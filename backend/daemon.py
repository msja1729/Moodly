"""The background daemon: polling, inference, actuation and telemetry.

    python -m backend.daemon            # live Spotify (credentials in .env)
    python -m backend.daemon --mock     # scripted session, sped up 30x; themes go to
                                        # sandbox files in data/mock_workspace/

A single scheduler loop wakes for whichever is due next (a Spotify poll, a
session reshaping cycle, or a display-temperature step), so the whole daemon
runs on one injectable clock (and a long session can be simulated in
milliseconds in tests). Changes to config.json arrive from a watchdog thread
and are handled immediately.

Audio modes (config "audio.mode"):
    "session" (default, Phase 2)  the playlist session: mood = average of the
                                  queue; songs are inserted/removed in a
                                  private session playlist (actuators/session.py)
    "queue"   (Phase 1)           mood = moving average of played songs; one
                                  bridging song at a time is appended to
                                  Spotify's queue (actuators/audio.py)

Sessions are started and ended from the sidebar (config "paused" = no session;
"session_seq" bumped = start a new one). With the recommender (live session
mode, config "recommender"):
    start / new target   the session is "preparing": nothing is added or removed
                         while background refreshes fill the song pool for the
                         goal; once it holds `ready_songs` useful songs (or after
                         `prepare_max_s`), the session starts adding from it
    during the session   a refresh every `refresh_s`, or sooner when the pool had
                         nothing near a mood step
    end of session       the pool (data/recommender.db) is cleared
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from backend.actuators import session as session_mod
from backend.actuators.audio import AudioActuator, AudioParams, CandidatePool, build_pool
from backend.actuators.session import PlaylistSession, SessionParams, build_session_table
from backend.actuators.display import DisplayActuator, DisplayParams, NullBackend, default_backend
from backend.actuators.workspace import WorkspaceActuator
from backend.clock import AcceleratedClock, RealClock
from backend.features import (CsvFeatureProvider, FeatureCache, FeatureChain, ReccoBeatsProvider,
                              SpotifyFeatureProvider)
from backend.feedback import FeedbackLog
from backend.inference import InferenceEngine, InferenceParams, PollResult, TrackChanged, TrackStarted
from backend.mood_space import MarkovParams, Point, distance
from backend.selection import SelectionParams
from backend.spotify.client import RateLimited
from recommender import Recommender, RecommenderParams
from shared.config_io import (DEFAULT_CONFIG_PATH, PROJECT_ROOT, atomic_write_json, ensure_app_home,
                              load_config, resolve_path, target_point)

log = logging.getLogger("daemon")

PLAYER_COMMAND = "player.cmd"  # {"cmd": "play" | "pause" | "next"}, written by the sidebar
PLAYLISTS_EVERY_S = 600.0      # re-read the user's playlists (for the "Songs from" picker)


class Daemon:
    def __init__(self, config_path: Path, cfg: dict[str, Any], client, features: FeatureChain,
                 feedback: FeedbackLog, pool: CandidatePool, clock, display_backend=None,
                 workspace: WorkspaceActuator | None = None, state_path: Path | None = None,
                 rng: np.random.Generator | None = None, session: PlaylistSession | None = None,
                 recommender=None):
        self.config_path = Path(config_path)
        self.cfg = cfg
        self.client = client
        self.clock = clock
        self.feedback = feedback
        self.rng = rng or np.random.default_rng()
        self.inference = InferenceEngine(client, features, clock, InferenceParams.from_config(cfg), self.rng)
        self.audio = AudioActuator(client, pool, feedback, clock, AudioParams.from_config(cfg),
                                   MarkovParams.from_config(cfg), self.rng)
        self.workspace = workspace if workspace is not None else WorkspaceActuator(cfg)
        self.display = DisplayActuator(display_backend or NullBackend(), DisplayParams.from_config(cfg))
        self.state_path = state_path
        self.target_mood = cfg["target_mood"]
        self.target = target_point(cfg)
        self.paused = bool(cfg.get("paused", False))
        self.last_poll: PollResult | None = None
        self.last_bridge: str | None = None
        # Phase 2: the playlist session (None in "queue" mode).
        self.session = session
        self._next_cycle = self.clock.now()
        self._cycle_blocked_until = 0.0
        self.on_not_premium = None  # live mode: forget the account Spotify says isn't Premium
        # Session steps run in worker threads; a target change and the timer must never overlap
        # (two overlapping starts once created two playlists that each looked "foreign").
        self._session_lock = asyncio.Lock()
        self._observer = None
        self._reload_handle: asyncio.TimerHandle | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: set[asyncio.Task] = set()
        # The recommender's song pool (live mode only).
        self.recommender = recommender
        self._catalog_table = session.table if session is not None else None  # fallback for the pool
        self._rec_thread: threading.Thread | None = None
        self._rec_ready = False
        self._next_rec = self.clock.now()
        self._rec_started_at = -1e18
        self._prepare_started: float | None = None
        self._session_seq = int(cfg.get("session_seq", 0))
        if recommender is not None and session is not None:
            session.on_missing_features = recommender.want_features
        if recommender is not None:
            recommender.set_source(cfg.get("song_source"))
        self.playlists: list[dict[str, Any]] = []  # the user's own playlists, for the sidebar's picker
        self.liked_count: int | None = None
        self._next_playlists = self.clock.now()
        self._playlists_task: asyncio.Task | None = None

    # ------------------------------------------------------------- target mood
    async def apply_target(self, reason: str) -> None:
        mood_cfg = self.cfg["moods"][self.target_mood]
        now = self.clock.now()
        mood = self.mood
        log.info("target mood: %s (%.2f, %.2f) [%s]", self.target_mood, *self.target, reason)
        if self.paused:
            self.display.set_target(self.display.params.max_kelvin)
            await self.end_session()
            return

        started = time.perf_counter()
        changed = await asyncio.to_thread(self.workspace.apply, mood_cfg)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if changed:
            self.feedback.log_intervention(now, "theme", ", ".join(changed), mood)
            if elapsed_ms > 500:
                log.warning("theme hot-swap took %.0f ms (budget 500 ms)", elapsed_ms)

        kelvin = float(mood_cfg.get("kelvin", 6500))
        if kelvin != self.display.target:
            self.display.set_target(kelvin)
            self.feedback.log_intervention(now, "display", f"target {self.display.target:.0f}K", mood)

        self._begin_preparing()
        await self._audio_step("target_change")

    @property
    def mood(self) -> Point | None:
        """Phase 2: the average of the queue while a session runs; otherwise the listening-history estimate."""
        if self.session is not None and self.session.active and self.session.mood is not None:
            return self.session.mood
        return self.inference.mood

    async def reload_config(self) -> None:
        try:
            cfg = await asyncio.to_thread(load_config, self.config_path)
        except Exception as e:
            log.warning("ignoring unreadable config.json: %s", e)
            return
        old = (self.target_mood, self.paused)
        old_seq = self._session_seq
        source_changed = self.recommender is not None and self.recommender.set_source(cfg.get("song_source"))
        self.cfg = cfg
        self.target_mood, self.target = cfg["target_mood"], target_point(cfg)
        self.paused = bool(cfg.get("paused", False))
        self._session_seq = int(cfg.get("session_seq", 0))
        if self._session_seq != old_seq and not self.paused:
            log.info("starting a new session")
            await self.end_session()
            await self.apply_target("new_session")
            self.write_state()
        elif (self.target_mood, self.paused) != old:
            await self.apply_target("config_change")
            self.write_state()
        elif source_changed:
            log.info("songs now come from: %s", self.recommender.source_label())
            if not self.paused:
                await self._clear_pool()  # the old pool's songs aren't from the new source
                self._begin_preparing()
            self.write_state()

    # ------------------------------------------------------------- playback
    async def handle_poll(self, result: PollResult) -> None:
        self.last_poll = result
        new_track = False
        for ev in result.events:
            if isinstance(ev, TrackChanged):
                source = self.session if self.session is not None else self.audio
                itype, direction = source.take_intervention(ev.track.id)
                self.feedback.log_event(self.clock.now(), self.mood, itype, ev.skipped,
                                        ev.track.id, direction)
                log.info("%s %s - %s (%s)", "skipped" if ev.skipped else "finished",
                         ev.track.artists, ev.track.name, itype)
            elif isinstance(ev, TrackStarted):
                new_track = True
                self.audio.note_started(ev.track.id)
                if self.session is not None:
                    self.session.note_started(ev.track.id, ev.track.name, ev.track.artists)
                mood = self.mood
                log.info("now playing %s - %s | mood %s | target %s",
                         ev.track.artists, ev.track.name,
                         f"({mood[0]:.2f}, {mood[1]:.2f})" if mood else "unknown", self.target_mood)
        if new_track and not self.paused:
            await self._audio_step("track_change")

    async def _audio_step(self, reason: str) -> None:
        if self.session is None:
            await self._inject(reason)
        else:
            await self._session_step(reason)

    async def _session_step(self, reason: str) -> None:
        """Start the playlist session if needed, then run one reshaping cycle (one at a time)."""
        async with self._session_lock:
            await self._session_step_locked(reason)

    async def _session_step_locked(self, reason: str) -> None:
        s = self.session
        now = self.clock.now()
        if self.paused or now < self._cycle_blocked_until:
            return
        if (reason == "track_change" and s.last_cycle_at is not None
                and now - s.last_cycle_at < s.params.min_cycle_gap_s):
            return
        try:
            if not s.active:
                # After the user took over (or a non-Premium answer), only a new target restarts it.
                if s.status in (session_mod.USER_TOOK_OVER, session_mod.NOT_PREMIUM) and reason != "target_change":
                    return
                if not await asyncio.to_thread(s.start):
                    if s.status == session_mod.NOT_PREMIUM and self.on_not_premium is not None:
                        self.on_not_premium()
                    return
                reason = "start"
            await asyncio.to_thread(s.cycle, self.target, reason)
        except RateLimited as e:
            self._cycle_blocked_until = self.clock.now() + e.retry_after
            log.warning("Spotify rate limit; session paused for %.0fs", e.retry_after)
        except Exception as e:
            log.warning("session cycle failed: %s", e)
        self._next_cycle = self.clock.now() + s.params.cycle_s
        if s.last_added:
            self.last_bridge = s.last_added[-1]

    async def end_session(self) -> None:
        """End the session (hand the next songs back, delete the playlist) and clear the song pool."""
        if self.session is not None:
            async with self._session_lock:
                if self.session.active:
                    await asyncio.to_thread(self.session.end, True)
        await self._clear_pool()

    async def _clear_pool(self) -> None:
        if self.recommender is None:
            return
        self.recommender.stop.set()  # a running refresh stops at its next step
        if self._rec_thread is not None:
            await asyncio.to_thread(self._rec_thread.join, 30)
        self.recommender.stop.clear()
        await asyncio.to_thread(self.recommender.clear)
        self._rec_ready = False
        self._prepare_started = None
        if self.session is not None:
            self.session.preparing = False
            self.session.needs_more_songs = False
            self.session.allow_repeats = self.session.source_dry = False
            self.session.use_tables(self._catalog_table)
        log.info("song pool cleared")

    async def _inject(self, reason: str) -> None:
        inj = await self.audio.maybe_inject(self.inference.mood, self.target, reason)
        if inj:
            self.last_bridge = f"{inj.candidate.artists} - {inj.candidate.name}"

    async def _refresh_playlists(self) -> None:
        """The user's own playlists and Liked Songs count, for the sidebar's "Songs from" picker."""
        try:
            rows = await asyncio.to_thread(self.client.my_playlists_raw)
            liked = await asyncio.to_thread(self.client.saved_tracks_count)
        except Exception as e:
            log.debug("could not list your playlists: %s", e)
            return
        playlists = [{"id": p["id"], "name": p.get("name") or "Untitled",
                      "count": int(((p.get("items") or p.get("tracks") or {}).get("total")) or 0)}
                     for p in rows if p.get("id") and p.get("name") != session_mod.SESSION_NAME]
        if (playlists, liked) != (self.playlists, self.liked_count):
            self.playlists, self.liked_count = playlists, liked
            self.write_state()

    # ------------------------------------------------------------- recommender
    def _begin_preparing(self) -> None:
        """New session or new target: stop adding until the pool has enough songs for the goal."""
        if self.recommender is None or self.session is None or self.paused:
            return
        self.session.preparing = True
        self.session.needs_more_songs = False
        self._prepare_started = self.clock.now()
        self.recommender.set_goal(self.target, self.session.mood)
        kept = self.recommender.prune()  # songs that would lead away from the new target leave the pool
        self._rec_ready = True
        self._start_rec_refresh()
        log.info("preparing songs for %s (%d useful songs in the pool already)", self.target_mood, kept)

    async def _check_ready(self) -> None:
        """End the preparing phase once the pool holds enough useful songs (or waiting took too long)."""
        s, rec = self.session, self.recommender
        if rec is None or s is None or not s.preparing or self.paused:
            return
        useful = rec.useful_count(s.mood, self.target)
        waited = self.clock.now() - (self._prepare_started if self._prepare_started is not None else self.clock.now())
        # A playlist / Liked Songs source is ready once it has been read: it won't grow by waiting.
        if useful >= rec.params.ready_songs or waited >= rec.params.prepare_max_s or (rec.custom and rec.loaded):
            s.preparing = False
            log.info("%d useful songs ready after %.0fs; the session starts adding songs", useful, waited)
            await self._session_step("ready")

    def _start_rec_refresh(self) -> bool:
        """Refresh the song pool in a background thread (a minute or two; playback goes on). True if started."""
        if self.recommender is None or (self._rec_thread is not None and self._rec_thread.is_alive()):
            return False
        self._rec_started_at = self.clock.now()
        if self.session is not None:
            self.recommender.set_goal(self.target, self.session.mood)

        def run():
            try:
                self.recommender.refresh()
                self._rec_ready = True
            except RateLimited:
                log.warning("recommender: Spotify rate limit; next refresh in %.0fs", self.recommender.params.refresh_s)
            except Exception as e:
                log.warning("recommender refresh failed: %s", e)

        self._rec_thread = threading.Thread(target=run, name="recommender", daemon=True)
        self._rec_thread.start()
        return True

    def _apply_rec(self) -> None:
        """Hand the latest pool to the session (the catalog only keeps the music going)."""
        if not self._rec_ready or self.session is None:
            return
        self._rec_ready = False
        pool = self.recommender.table()
        if self.recommender.custom:
            # Only the chosen playlist: any language, no top-up from elsewhere, repeats allowed.
            self.session.use_tables(pool, None, None)
            self.session.allow_repeats = True
            log.info("song pool: %d songs from %s", len(pool), self.recommender.source_label())
            return
        self.session.allow_repeats = self.session.source_dry = False
        self.session.use_tables(pool, self.recommender.languages, self._catalog_table)
        log.info("song pool: %d songs matching your taste (%s)", len(pool),
                 ", ".join(self.recommender.language_names()) or "languages not known yet")

    # ------------------------------------------------------------- state for the UI
    def state(self) -> dict[str, Any]:
        mood = self.mood
        pb = self.last_poll.playback if self.last_poll else None
        steps = None
        if mood is not None:
            steps = round(self.audio.chain_for(self.target).expected_steps_from(mood), 1)
        return {
            "updated": self.clock.now(),
            "mood": list(mood) if mood else None,
            "target_mood": self.target_mood,
            "target": list(self.target),
            "deviation": round(distance(mood, self.target), 3) if mood else None,
            "expected_steps": steps,
            "track": f"{pb.track.artists} - {pb.track.name}" if pb else None,
            "track_name": pb.track.name if pb else None,
            "track_artists": pb.track.artists if pb else None,
            "track_image": pb.track.image if pb else None,
            # The sidebar moves the progress bar itself from these (no need to write every second).
            "progress_ms": pb.progress_ms if pb else None,
            "duration_ms": pb.track.duration_ms if pb else None,
            "is_playing": pb.is_playing if pb else False,
            "observed_at": pb.observed_at if pb else None,
            "last_bridge": self.last_bridge,
            "kelvin": self.display.current,
            "paused": self.paused,
            "mode": "session" if self.session is not None else "queue",
            "session": None if self.session is None else {
                "status": self.session.status,
                "preparing": self.session.preparing,
                "queue_size": self.session.queue_size,
                "last_added": self.session.last_added,
                "last_removed": self.session.last_removed,
                "source_dry": self.session.source_dry,
            },
            "song_source": None if self.recommender is None else {
                **self.recommender.source, "label": self.recommender.source_label()},
            "playlists": self.playlists,
            "liked_count": self.liked_count,
            "recommender": None if self.recommender is None else {
                "status": self.recommender.status,
                "languages": self.recommender.language_names(),
                "pool": len(self.recommender.table()),
                "useful": (self.recommender.useful_count(self.session.mood, self.target)
                           if self.session is not None else 0),
                "ready_songs": self.recommender.params.ready_songs,
            },
        }

    # Fields that change on every poll without anything the user would see changing.
    _VOLATILE = ("updated", "progress_ms", "observed_at")

    def write_state(self, force: bool = False) -> None:
        """Write data/state.json for the sidebar: at once when something visible changed, else every 10 s."""
        if not self.state_path:
            return
        state = self.state()
        visible = {k: v for k, v in state.items() if k not in self._VOLATILE}
        now = self.clock.now()
        if not force and visible == getattr(self, "_last_state", None) and now - getattr(self, "_last_write", -1e18) < 10:
            return
        try:
            atomic_write_json(self.state_path, state)
            self._last_state, self._last_write = visible, now
        except OSError as e:
            log.debug("could not write state: %s", e)

    # ------------------------------------------------------------- scheduling
    async def run_for(self, seconds: float | None = None) -> None:
        """Main loop. Runs until cancelled, or for `seconds` of clock time."""
        end = None if seconds is None else self.clock.now() + seconds
        next_poll = self.clock.now()
        next_display = self.clock.now()
        while end is None or self.clock.now() < end:
            now = self.clock.now()
            if self.recommender is not None and not self.paused:
                s = self.session
                # Back-to-back refreshes while preparing; sooner than usual when a step found nothing.
                wants_more = s is not None and s.needs_more_songs and now - self._rec_started_at >= 120
                if (s is not None and s.preparing) or wants_more or now >= self._next_rec:
                    if self._start_rec_refresh():
                        self._next_rec = now + self.recommender.params.refresh_s
                        if s is not None:
                            s.needs_more_songs = False
                self._apply_rec()
                await self._check_ready()
            if self.recommender is not None and now >= self._next_playlists and (
                    self._playlists_task is None or self._playlists_task.done()):
                self._next_playlists = now + PLAYLISTS_EVERY_S
                self._playlists_task = asyncio.ensure_future(self._refresh_playlists())
            if now >= next_poll:
                result = await self.inference.poll()
                await self.handle_poll(result)
                self.write_state()
                next_poll = self.clock.now() + result.delay_s
            if self.session is not None and not self.paused and self.clock.now() >= self._next_cycle:
                await self._session_step("timer")
                self._next_cycle = max(self._next_cycle, self.clock.now() + 1.0)
                self.write_state()
            if now >= next_display:
                if await asyncio.to_thread(self.display.step) is not None:
                    self.write_state()
                next_display = self.clock.now() + self.display.params.step_interval_s
            due = [next_poll, next_display]
            if self.recommender is not None and not self.paused:
                due.append(self._next_rec)
                if self.session is not None and self.session.preparing:
                    due.append(self.clock.now() + 2.0)  # check readiness and the refresh thread often
            if self.session is not None and not self.paused:
                due.append(self._next_cycle)
            if end is not None:
                due.append(end)
            wake = min(due)
            await self.clock.sleep(max(0.0, wake - self.clock.now()))

    def start_watcher(self) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        self._loop = asyncio.get_running_loop()
        target = self.config_path.resolve()
        daemon = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                paths = [event.src_path, getattr(event, "dest_path", None)]
                if any(p and Path(p).resolve() == target for p in paths):
                    daemon._loop.call_soon_threadsafe(daemon._schedule_reload)

        self._observer = Observer()
        self._observer.schedule(Handler(), str(target.parent), recursive=False)
        if self.state_path is not None:
            # The sidebar's play/pause/next buttons write data/player.cmd next to state.json.
            command = self.player_command_path

            class PlayerHandler(FileSystemEventHandler):
                def on_any_event(self, event):
                    paths = [event.src_path, getattr(event, "dest_path", None)]
                    if any(p and Path(p).name == command.name for p in paths) and command.exists():
                        daemon._loop.call_soon_threadsafe(daemon._spawn_player_command)

            command.parent.mkdir(parents=True, exist_ok=True)
            command.unlink(missing_ok=True)  # a click from before this start is stale
            self._observer.schedule(PlayerHandler(), str(command.parent), recursive=False)
        self._observer.start()

    @property
    def player_command_path(self) -> Path:
        return self.state_path.parent / PLAYER_COMMAND

    def _spawn_player_command(self) -> None:
        task = asyncio.ensure_future(self.player_command())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def player_command(self) -> None:
        """Run the sidebar's play/pause/next on Spotify, then show the result at once."""
        path = self.player_command_path
        try:
            cmd = json.loads(path.read_text(encoding="utf-8")).get("cmd")
            path.unlink(missing_ok=True)
        except (OSError, ValueError):
            return  # gone already (handled), or still being written: the next event retries
        action = {"play": self.client.resume, "pause": self.client.pause, "next": self.client.next_track}.get(cmd)
        if action is None:
            return
        log.info("player: %s", cmd)
        try:
            await asyncio.to_thread(action)
        except Exception as e:
            log.warning("could not %s: %s", cmd, e)
        # Spotify takes a moment to report the change; look twice so the sidebar follows at once.
        for wait in (0.25, 0.6):
            await self.clock.sleep(wait)
            result = await self.inference.poll()
            await self.handle_poll(result)
            self.write_state(force=True)

    def _schedule_reload(self) -> None:
        # Editors and os.replace() fire several events per save; coalesce them.
        if self._reload_handle:
            self._reload_handle.cancel()
        self._reload_handle = self._loop.call_later(0.05, self._spawn_reload)

    def _spawn_reload(self) -> None:
        task = asyncio.ensure_future(self.reload_config())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def shutdown(self) -> None:
        if self.recommender is not None:
            self.recommender.stop.set()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None
        self.display.restore()
        self.feedback.close()


# ----------------------------------------------------------------- construction

def build_daemon(args: argparse.Namespace) -> Daemon:
    config_path = Path(args.config)
    cfg = load_config(config_path)
    paths = cfg.get("paths", {})
    state_path = resolve_path(paths.get("state", "data/state.json"))
    dataset = CsvFeatureProvider(resolve_path(paths.get("tracks_csv", "data/tracks.csv")))

    if args.mock:
        from backend.spotify.client import MockSpotifyClient, demo_catalog

        clock = AcceleratedClock(args.speed)
        catalog = demo_catalog()
        # Start the session somewhere loud and upbeat so there is something to correct.
        start = sorted(catalog, key=lambda t: -(catalog[t][1].energy + catalog[t][1].valence))[:4]
        client = MockSpotifyClient(catalog, clock, script=[(t, 1.0) for t in start], seed=args.seed)
        db_path = resolve_path("data/mock.db")
        chain = FeatureChain([SpotifyFeatureProvider(client)], FeatureCache(db_path))
        dataset = None
        from backend.catalog import CatalogTrack, TrackTable
        session_catalog = TrackTable(CatalogTrack(t.id, t.name, t.artists, f, t.duration_ms)
                                     for t, f in catalog.values())
    else:
        from backend.spotify.auth import AccountStore, linked_account
        from backend.spotify.client import LiveSpotifyClient

        store = AccountStore()
        account = linked_account(store)
        if account is None:
            raise SystemExit("No Spotify Premium account is signed in. Start the app "
                             "(python -m frontend.app) and choose or connect a Spotify account first.")
        log.info("Spotify account: %s (%s)", account.display_name,
                 "Premium" if account.is_premium else "Premium not confirmed yet")
        clock = RealClock()
        client = LiveSpotifyClient(clock, store)
        db_path = resolve_path(paths.get("db", "data/mood.db"))
        chain = FeatureChain([SpotifyFeatureProvider(client), ReccoBeatsProvider(), dataset],
                             FeatureCache(db_path))
        session_catalog = None

    pool = build_pool(client, chain, dataset, AudioParams.from_config(cfg).pool_size)
    feedback = FeedbackLog(db_path)
    session = None
    if cfg.get("audio", {}).get("mode", "session") == "session":
        if session_catalog is None:
            from backend.catalog import load_table

            session_catalog = load_table(resolve_path(paths.get("catalog_db", "data/catalog.db")))
            if not len(session_catalog):
                log.warning("the track catalog is empty; run `python -m backend.catalog import` "
                            "(songs will come only from your own library until then)")
        table = build_session_table(client, chain, session_catalog, AudioParams.from_config(cfg).pool_size)
        session_state = resolve_path(paths.get("session_state", "data/session.json")
                                     if not args.mock else "data/mock_session.json")
        session = PlaylistSession(client, table, chain, feedback, clock,
                                  SelectionParams.from_config(cfg), SessionParams.from_config(cfg),
                                  MarkovParams.from_config(cfg), np.random.default_rng(args.seed),
                                  state_file=session_state)
    recommender = None
    rec_params = RecommenderParams.from_config(cfg)
    if session is not None and not args.mock and rec_params.enabled:
        recommender = Recommender.build(client, [ReccoBeatsProvider(), dataset], chain.cache,
                                        resolve_path(paths.get("recommender_db", "data/recommender.db")),
                                        rec_params, feedback)
    backend = NullBackend() if args.no_display else default_backend()
    workspace = None
    if args.mock and not args.real_workspace:
        workspace = WorkspaceActuator(mock_workspace_config(cfg))
    daemon = Daemon(config_path, cfg, client, chain, feedback, pool, clock,
                    display_backend=backend, workspace=workspace, state_path=state_path,
                    rng=np.random.default_rng(args.seed), session=session, recommender=recommender)
    if not args.mock:
        def forget_account():
            log.error("Spotify says %s doesn't have Premium; this account was removed", account.display_name)
            store.remove(account.id)

        daemon.on_not_premium = forget_account
    return daemon


def mock_workspace_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Point theme edits at sandbox copies in data/mock_workspace/ instead of the real apps."""
    sandbox = resolve_path("data/mock_workspace")
    sandbox.mkdir(parents=True, exist_ok=True)
    seeds = {
        "vscode_settings": ("vscode_settings.json",
                            '{\n    // sandbox copy used by --mock\n    "editor.fontSize": 14,\n'
                            '    "workbench.colorTheme": "Default Dark Modern",\n}\n'),
        "terminal_settings": ("terminal_settings.json",
                              '{\n    // sandbox copy used by --mock\n    "profiles": {\n'
                              '        "defaults": {},\n        "list": []\n    }\n}\n'),
    }
    ws = dict(cfg.get("workspace", {}))
    for key, (name, seed) in seeds.items():
        path = sandbox / name
        if not path.exists():
            path.write_text(seed, encoding="utf-8")
        ws[key] = str(path)
    return {**cfg, "workspace": ws}


def parent_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def watch_for_stop(loop: asyncio.AbstractEventLoop, task: asyncio.Task, stop_file: Path | None,
                   parent_pid: int | None, interval_s: float = 0.5) -> threading.Thread:
    """Cancel `task` (a clean shutdown) when `stop_file` appears or the parent process exits.

    The UI starts the daemon with both, so quitting the UI, or the UI crashing,
    still ends the session and restores the display.
    """
    def run():
        while not task.done():
            if (stop_file is not None and stop_file.exists()) or (parent_pid and not parent_alive(parent_pid)):
                log.info("stop requested")
                loop.call_soon_threadsafe(task.cancel)
                return
            time.sleep(interval_s)

    thread = threading.Thread(target=run, name="stop-watch", daemon=True)
    thread.start()
    return thread


def acquire_single_instance(lock_path: Path):
    """Lock `lock_path` for this process's lifetime. Returns the open file, or None if another daemon holds it.

    Two live daemons would run two sessions on the same Spotify account.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


async def amain(args: argparse.Namespace) -> None:
    stop_file = Path(args.stop_file) if args.stop_file else None
    if stop_file is not None:
        stop_file.unlink(missing_ok=True)
    if stop_file is not None or args.parent_pid:
        watch_for_stop(asyncio.get_running_loop(), asyncio.current_task(), stop_file, args.parent_pid)
    daemon = await asyncio.to_thread(build_daemon, args)
    try:
        daemon.start_watcher()
        await daemon.apply_target("startup")
        await daemon.run_for(args.duration)
    finally:
        try:
            await daemon.end_session()  # hand the next songs back to the queue, delete the playlist
        finally:
            daemon.shutdown()
        log.info("stopped; display restored")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Ambient Mood Regulator daemon")
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    p.add_argument("--mock", action="store_true", help="use a scripted mock Spotify session")
    p.add_argument("--speed", type=float, default=30.0, help="mock mode: time acceleration")
    p.add_argument("--duration", type=float, default=None, help="stop after N seconds of (clock) time")
    p.add_argument("--no-display", action="store_true", help="never touch the screen gamma")
    p.add_argument("--real-workspace", action="store_true",
                   help="mock mode: edit the real VS Code/Terminal settings instead of sandbox copies")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--parent-pid", type=int, default=None, help="stop cleanly when this process exits")
    p.add_argument("--stop-file", default=None, help="stop cleanly when this file appears")
    p.add_argument("--log-file", default=None, help="log here instead of the console")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    log_target = {"filename": args.log_file, "encoding": "utf-8"} if args.log_file else {}
    if args.log_file:
        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S",
                        **log_target)
    for noisy in ("watchdog", "urllib3", "spotipy"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    ensure_app_home()
    lock = None
    if not args.mock:
        lock = acquire_single_instance(resolve_path("data/daemon.lock"))
        if lock is None:
            log.error("another Ambient Mood daemon is already running (the tray app starts one itself; "
                      "use `python -m frontend.app --no-daemon` to run the daemon separately)")
            raise SystemExit(1)
    try:
        asyncio.run(amain(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except SystemExit as e:
        log.error("%s", e)
        raise


if __name__ == "__main__":
    main()

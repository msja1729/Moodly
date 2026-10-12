"""The playlist session: Phase 2's audio actuator.

Spotify's Web API can't insert into or remove from the playback queue, so the
session plays a private playlist it owns, "Ambient Mood session", and edits
that instead:

    start   the song now playing + the user's upcoming queue are written to a
            new playlist, which is then played from the same song at the same
            millisecond (no audible gap).
    cycle   (regularly, and on song/target changes) read the playlist and
            where playback is; mood = average point of the window (now playing
            + upcoming); plan_cycle() picks songs to insert and songs to
            remove; apply the edits. Songs come from `table` (the recommender's
            pool: the user's taste and top-2 `languages`).
    preparing  (set by the daemon while the pool is being filled for a new
            session or target) cycles only measure the mood: nothing is added
            or removed, except to keep `prepare_min_upcoming` songs ahead so
            the music never stops. When the pool has nothing near a mood step
            (picks average more than `pool_max_jump` away), the cycle adds
            nothing and sets `needs_more_songs` so the pool grows; the static
            catalog (`fallback_table`) is used only to keep the music going. Songs that already played this session
            (e.g. from Spotify's own queue) are removed from the upcoming part,
            so nothing plays twice.
    end     (pause nudging, quit, log out) the next few songs are handed back
            to Spotify's queue so the music keeps going, then the playlist is
            deleted. If the user starts other music themselves, the session
            just ends and deletes the playlist.

The session never pauses playback. A user pause leaves it idle until they
press play again. Its playlist id is recorded in a small state file, so a
playlist left behind by a crash is deleted on the next start.
"""
from __future__ import annotations

import json
import logging
import math
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping

import numpy as np

from backend.catalog import CatalogTrack, TrackTable
from backend.features import FeatureChain, Features
from backend.mood_space import Direction, MarkovParams, MoodChain, Point, distance
from backend.selection import (CyclePlan, MusicalContext, SelectionParams, WindowItem, plan_cycle, queue_mood,
                               rank_candidates)
from backend.spotify.client import Item, PremiumRequired, RateLimited

log = logging.getLogger(__name__)

SESSION_NAME = "Ambient Mood session"
SESSION_DESCRIPTION = "Managed by Ambient Mood while a session runs; deleted when the session ends."

# Session states shown in the sidebar.
IDLE = "idle"                    # no session (paused nudging, or not started yet)
WAITING = "waiting"              # wants to start, but nothing is playing
MANAGING = "managing"
USER_PAUSED = "user_paused"      # the user paused Spotify; the session waits
USER_TOOK_OVER = "user_took_over"  # the user started other music; session ended
NOT_PREMIUM = "not_premium"


@dataclass(frozen=True)
class SessionParams:
    cycle_s: float = 90.0            # regular reshaping interval
    min_cycle_gap_s: float = 30.0    # song changes don't trigger cycles more often than this
    handback_count: int = 10         # songs moved to Spotify's queue when the session ends
    max_initial_items: int = 50
    takeover_grace_s: float = 20.0   # right after hand-over Spotify may still report the old source
    resync_after_edits: bool = True  # replay the playlist (same song, same moment) so Spotify sees the edits
    recent_exclusion: int = 50       # songs played recently are not picked again...
    replay_after: int = 10           # ...unless the catalog runs dry; then only the last 10 are excluded
    pool_max_jump: float = 0.3       # pool picks further than this (on average) from the waypoint: add nothing
    prepare_min_upcoming: int = 2    # while preparing, songs added only to keep this many ahead

    @classmethod
    def from_config(cls, cfg: Mapping) -> "SessionParams":
        s = cfg.get("session", {})
        return cls(**{k: type(getattr(cls, k))(v) for k, v in s.items() if k in cls.__dataclass_fields__})


def build_session_table(client, features: FeatureChain, catalog: TrackTable, limit: int = 200) -> TrackTable:
    """The catalog plus the user's own top/saved tracks (marked "library")."""
    try:
        tracks = client.library_tracks(limit)
        feats = features.get_many([t.id for t in tracks])
    except Exception as e:
        log.warning("could not load library tracks: %s", e)
        return catalog
    lib = [CatalogTrack(t.id, t.name, t.artists, feats[t.id], t.duration_ms, source="library")
           for t in tracks if t.id in feats]
    table = catalog.merged_with(lib)
    log.info("session catalog: %d tracks (%d from your library)", len(table), int(table.library.sum()))
    return table


def manual_prefix_length(spotify_queue: list[str], playlist_upcoming: list[str]) -> int:
    """How many leading items of Spotify's queue are the user's own "Add to queue" songs.

    Spotify's queue = [user-queued songs] + [the playlist's upcoming songs].
    The answer is the smallest offset after which the queue matches the
    playlist's upcoming songs over their whole overlap. Comparing the whole
    overlap (not just the first song) keeps this right when the user-queued
    songs are also in the playlist (they're copied in at hand-over).
    """
    if not playlist_upcoming:
        # Nothing to compare with (e.g. the playlist ran out). These are most likely Spotify's own
        # autoplay suggestions, not the user's songs: treat none as protected so the refill happens.
        return 0
    n = len(spotify_queue)
    # 1st choice: everything after the user's songs is the playlist (Spotify may cut the list short).
    for i in range(n):
        rest = spotify_queue[i:]
        if len(rest) <= len(playlist_upcoming) and rest == playlist_upcoming[:len(rest)]:
            return i
    # Otherwise Spotify appended its own autoplay songs after the playlist: the whole playlist must fit.
    m = len(playlist_upcoming)
    for i in range(n - m + 1):
        if spotify_queue[i:i + m] == playlist_upcoming:
            return i
    return n


class PlaylistSession:
    def __init__(self, client, table: TrackTable, features: FeatureChain | None, feedback, clock,
                 selection: SelectionParams = SelectionParams(), params: SessionParams = SessionParams(),
                 markov: MarkovParams = MarkovParams(), rng: np.random.Generator | None = None,
                 state_file: Path | None = None):
        self.client = client
        self.table = table
        self.fallback_table: TrackTable | None = None
        self.preparing = False         # pool being filled: measure only, don't add or remove
        self._window_points: list[tuple[str, Point | None]] = []  # last window seen: (uri, point)
        self.needs_more_songs = False  # the pool had nothing near the last mood step
        self.languages: frozenset[str] | None = None  # only songs in these languages from `table`
        self.on_missing_features: Callable[[list[str]], None] | None = None  # e.g. Recommender.want_features
        self.features = features
        self.feedback = feedback
        self.clock = clock
        self.selection = selection
        self.params = params
        self.markov = markov
        self.rng = rng or np.random.default_rng()
        self.state_file = state_file

        self.status = IDLE
        self.playlist_id: str | None = None
        self.playlist_uri: str | None = None
        self.mood: Point | None = None
        self.deviation: float | None = None
        self.queue_size = 0
        self.last_added: list[str] = []
        self.last_removed: list[str] = []
        self.last_cycle_at: float | None = None
        self.played_ids: set[str] = set()      # songs that started this session
        self.removed_ids: set[str] = set()     # never re-add a song we removed
        self.recent: deque[str] = deque(maxlen=params.recent_exclusion)
        self.pending: dict[str, Direction] = {}  # inserted song -> Markov direction (for skip feedback)
        self._cur_index = 0
        self._handover_at: float | None = None
        self._foreign_seen = 0          # consecutive cycles that saw another source playing
        self._known_uris: set[str] = set()  # playlist songs + Spotify-queue songs seen last cycle
        self._resync_pending = False
        self.resyncs = 0
        # What Spotify actually plays: its copy of the playlist from the last play_context()
        # (start or resync). Spotify doesn't see later edits until the next resync.
        self._snapshot: list[str] = []
        self._snap_index = 0

    # ------------------------------------------------------------- helpers
    @property
    def active(self) -> bool:
        return self.playlist_id is not None

    def use_tables(self, table: TrackTable, languages=None, fallback: TrackTable | None = None) -> None:
        """Pick from `table` (in `languages`), falling back to `fallback` (set by the daemon)."""
        self.table = table
        self.languages = frozenset(languages) if languages else None
        self.fallback_table = fallback

    def chain_for(self, target: Point) -> MoodChain:
        stats = self.feedback.skip_stats() if self.feedback else None
        return MoodChain(target, self.markov, stats)

    def _features_for(self, items: list[Item]) -> dict[str, Features]:
        out: dict[str, Features] = {}
        missing: list[str] = []
        for it in items:
            if it.kind != "track":
                continue
            t = self.table.get(it.id)
            if t is None and self.fallback_table is not None:
                t = self.fallback_table.get(it.id)
            if t is not None:
                out[it.id] = t.features
            else:
                missing.append(it.id)
        if missing and self.features is not None:
            try:
                out.update(self.features.get_many(missing))
            except Exception as e:
                log.warning("feature lookup failed: %s", e)
        still = [i for i in missing if i not in out]
        if still and self.on_missing_features is not None:
            self.on_missing_features(still)
        return out

    def _save_state(self) -> None:
        if self.state_file is None:
            return
        try:
            if self.playlist_id:
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                self.state_file.write_text(json.dumps({"playlist_id": self.playlist_id, "uri": self.playlist_uri}),
                                           encoding="utf-8")
            else:
                self.state_file.unlink(missing_ok=True)
        except OSError as e:
            log.debug("could not write session state: %s", e)

    def cleanup_leftover(self) -> None:
        """Delete a session playlist left behind by a crash."""
        if self.state_file is None or not self.state_file.exists() or self.active:
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.client.delete_playlist(data["playlist_id"], data["uri"])
            log.info("deleted a leftover session playlist from an earlier run")
        except Exception as e:
            log.warning("could not delete leftover session playlist: %s", e)
        self.state_file.unlink(missing_ok=True)

    # ------------------------------------------------------------- start
    def start(self) -> bool:
        """Hand playback over to a new session playlist. Returns True if a session is running."""
        if self.active:
            return True
        self.cleanup_leftover()
        try:
            ps = self.client.player_state()
            if ps is None or ps.item is None or not ps.is_playing:
                self.status = WAITING
                return False
            upcoming = self.client.queue()
            items: list[Item] = []
            seen: set[str] = set()
            for it in [ps.item] + upcoming:
                if it.uri not in seen:
                    seen.add(it.uri)
                    items.append(it)
            items = items[: self.params.max_initial_items]

            pid, uri = self.client.create_playlist(SESSION_NAME, SESSION_DESCRIPTION)
            self.playlist_id, self.playlist_uri = pid, uri
            self._save_state()
            self.client.playlist_add(pid, [it.uri for it in items])
            try:
                self.client.set_shuffle(False)
            except Exception as e:
                log.debug("could not turn shuffle off: %s", e)
            # Continue the same song at the same moment (allowing for the time spent setting up).
            progress = ps.progress_ms + int((self.clock.now() - ps.observed_at) * 1000)
            self.client.play_context(uri, 0, progress)
        except PremiumRequired:
            self.status = NOT_PREMIUM
            self._delete_playlist()
            return False
        except RateLimited:
            raise
        except Exception as e:
            log.warning("could not start the session: %s", e)
            self._delete_playlist()
            self.status = WAITING
            return False

        self._cur_index = 0
        self._handover_at = self.clock.now()
        self._foreign_seen = 0
        self._known_uris = {it.uri for it in items}
        self._snapshot, self._snap_index = [it.uri for it in items], 0
        self.played_ids = {ps.item.id}
        self.status = MANAGING
        log.info("session started: %d songs moved into '%s'", len(items), SESSION_NAME)
        if self.feedback:
            self.feedback.log_intervention(self.clock.now(), "session_start", f"{len(items)} songs", None)
        return True

    # ------------------------------------------------------------- cycle
    def _locate(self, entries: list[Item], now_uri: str) -> tuple[int, bool]:
        """Index of the song now playing in the playlist, and whether it is playing *from* the playlist.

        Searches forward from the last known position, so a song that appears
        twice is matched at the right place. If the song isn't in the
        playlist (it's from Spotify's own queue), the last position is kept.
        """
        start = min(self._cur_index, max(len(entries) - 1, 0))
        for i in list(range(start, len(entries))) + list(range(0, start)):
            if entries[i].uri == now_uri:
                return i, True
        return min(self._cur_index, len(entries) - 1), False

    def cycle(self, target: Point, reason: str = "timer") -> None:
        """One reshaping step. Safe to call at any time."""
        if not self.active:
            return
        ps = self.client.player_state()
        if ps is None or ps.item is None:
            self.status = USER_PAUSED  # stopped on the user's side (or no device)
            return
        if ps.context_uri != self.playlist_uri:
            # Spotify reports the new source with a delay after hand-over, and may report no
            # source at all while a song from its own queue plays. Only another source (or no
            # source with a song we don't know), seen on two cycles in a row after the grace
            # period, means the user started other music.
            settling = self._handover_at is not None and self.clock.now() - self._handover_at < self.params.takeover_grace_s
            if settling or (ps.context_uri is None and ps.item.uri in self._known_uris):
                return
            self._foreign_seen += 1
            if self._foreign_seen < 2:
                return
            log.info("you started other music in Spotify; ending the session")
            self.status = USER_TOOK_OVER
            self._delete_playlist()
            return
        self._foreign_seen = 0
        if not ps.is_playing:
            self.status = USER_PAUSED
            return
        self.status = MANAGING

        entries = self.client.playlist_items(self.playlist_id)
        cur, in_playlist = self._locate(entries, ps.item.uri)
        self._cur_index = cur
        in_snapshot = self._advance_snapshot(ps.item.uri)
        if not in_playlist and in_snapshot and entries and self.params.resync_after_edits:
            # Spotify is playing its old copy of the playlist: this song was removed since.
            # Move on to the current playlist's next song.
            nxt = min(cur + 1, len(entries) - 1)
            log.info("Spotify was playing a song already removed from the playlist; moving on")
            self.client.play_context(self.playlist_uri, nxt, 0)
            self._cur_index = nxt
            self._after_resync(entries, nxt)
            return
        upcoming_entries = entries[cur + 1:]

        # Songs in Spotify's own queue play before the playlist continues; they can't be removed.
        # Spotify's queue = the user's songs + the rest of the playlist, as Spotify has it: usually its
        # copy from the last resync (snapshot), but some clients follow edits live. Take whichever
        # explains the queue with the fewest user songs, so playlist songs are never mistaken for them.
        spotify_queue = self.client.queue()
        queue_uris = [it.uri for it in spotify_queue]
        n_manual = min(manual_prefix_length(queue_uris, self._snapshot[self._snap_index + 1:]),
                       manual_prefix_length(queue_uris, [e.uri for e in upcoming_entries]))
        manual = spotify_queue[:n_manual]
        self._known_uris = {e.uri for e in entries} | {it.uri for it in spotify_queue}

        feats = self._features_for([ps.item] + manual + upcoming_entries)
        now_item = WindowItem(ps.item.uri, ps.item.name, ps.item.artists, feats.get(ps.item.id),
                              ps.item.kind, removable=False)
        window = [now_item]
        window += [WindowItem(it.uri, it.name, it.artists, feats.get(it.id), it.kind, removable=False)
                   for it in manual]
        window += [WindowItem(it.uri, it.name, it.artists, feats.get(it.id), it.kind,
                              removable=it.kind == "track") for it in upcoming_entries]

        # Drop playlist copies of songs that already played this session or that will play from
        # Spotify's own queue anyway (copied in at hand-over), so nothing plays twice.
        protected_uris = {ps.item.uri}
        if n_manual == 0 and upcoming_entries:
            protected_uris.add(upcoming_entries[0].uri)  # the next song, already buffered by Spotify
        manual_uris = {it.uri for it in manual}
        duplicate_uris = {it.uri for it in upcoming_entries
                          if it.kind == "track" and (it.id in self.played_ids or it.uri in manual_uris)
                          and it.uri not in protected_uris}

        chain = self.chain_for(target)
        exclude = self.removed_ids | set(self.recent)
        retarget = reason in ("start", "target_change", "ready")
        if self.preparing:
            # The pool is being filled: only measure the mood (the daemon decides when it's ready).
            mood = queue_mood(window, self.selection.window_size)
            plan = CyclePlan(mood, distance(mood, target) if mood is not None else None)
        else:
            plan = plan_cycle(window, self.table, target, chain, self.rng, self.selection, exclude, retarget,
                              self.languages)
            if plan.waypoint is not None and self._poor(plan):
                # Small pools run out of unplayed songs near the target: allow older repeats.
                relaxed = self.removed_ids | set(list(self.recent)[-self.params.replay_after:])
                retry = plan_cycle(window, self.table, target, chain, self.rng, self.selection, relaxed, retarget,
                                   self.languages)
                if not self._poor(retry) or len(retry.insertions) > len(plan.insertions):
                    plan = retry
            if (self.fallback_table is not None and plan.waypoint is not None
                    and (not plan.insertions or self._poor(plan, self.params.pool_max_jump, average=True))):
                # Nothing in the taste pool near this mood step: add nothing now; the pool grows first.
                log.info("no song in the taste pool near this mood step; waiting for more songs")
                plan.insertions = []
                self.needs_more_songs = True
        self.mood, self.deviation = plan.mood, plan.deviation
        self._window_points = [(it.uri, it.point) for it in window]

        # Removals: by URI (Spotify removes every occurrence), never a protected song.
        remove_uris = [window[i].uri for i in plan.removals if window[i].uri not in protected_uris]
        remove_uris = list(dict.fromkeys(remove_uris + sorted(duplicate_uris)))
        if remove_uris:
            self.client.playlist_remove(self.playlist_id, remove_uris)
            entries = self.client.playlist_items(self.playlist_id)
            cur, _ = self._locate(entries, ps.item.uri) if in_playlist else (self._cur_index, False)
            self._cur_index = cur

        # Insertions: window position p (0 = now playing, then Spotify's queue, then the playlist)
        # -> playlist index. Positions inside Spotify's queue map to "right after the current song".
        insertions = plan.insertions
        upcoming_after = len(entries) - cur - 1 + n_manual
        keep_ahead = self.params.prepare_min_upcoming if self.preparing else self.selection.min_length
        if upcoming_after + len(insertions) < keep_ahead:
            # Keep the music going (pool first; the static catalog only if the pool can't).
            insertions = insertions + self._emergency_fill(window, target, exclude, insertions,
                                                           keep_ahead - upcoming_after - len(insertions))
        added: list[str] = []
        length = len(entries)
        for p, track in insertions:
            # p is None for "append at the end"
            pos = None if p is None else min(cur + 1 + max(0, p - 1 - n_manual), length)
            self.client.playlist_add(self.playlist_id, [track.uri], position=pos)
            length += 1
            added.append(f"{track.artists} - {track.name}")
            if plan.direction is not None:
                self.pending[track.id] = plan.direction
        removed_names = []
        for u in remove_uris:
            tid = u.rsplit(":", 1)[-1]
            self.removed_ids.add(tid)
            hit = next((w for w in window if w.uri == u), None)
            removed_names.append(f"{hit.artists} - {hit.name}" if hit else u)

        self.queue_size = upcoming_after + len(insertions)
        self.last_added, self.last_removed = added, removed_names
        self.last_cycle_at = self.clock.now()
        now = self.clock.now()
        if self.feedback:
            for name, (_, track) in zip(added, insertions):
                self.feedback.log_intervention(now, "playlist_add", f"{track.uri} dir={plan.direction}", self.mood)
            for u in remove_uris:
                self.feedback.log_intervention(now, "playlist_remove", u, self.mood)
        if (added or remove_uris or self._resync_pending) and self.params.resync_after_edits:
            if in_playlist:
                self._resync(ps, cur)
            else:
                self._resync_pending = True  # a song from Spotify's own queue is playing: don't cut it
        if added or removed_names:
            log.info("session cycle (%s): mood %s, deviation %.2f, +%d -%d",
                     reason, f"({self.mood[0]:.2f}, {self.mood[1]:.2f})" if self.mood else "unknown",
                     self.deviation or 0.0, len(added), len(removed_names))

    def _advance_snapshot(self, uri: str) -> bool:
        """Find the song now playing in Spotify's copy of the playlist; False if it isn't there
        (it's from Spotify's own queue)."""
        for i in range(self._snap_index, len(self._snapshot)):
            if self._snapshot[i] == uri:
                self._snap_index = i
                return True
        return False

    def _after_resync(self, entries: list[Item] | None, index: int) -> None:
        if entries is None:
            entries = self.client.playlist_items(self.playlist_id)
        self._snapshot, self._snap_index = [e.uri for e in entries], index
        self._handover_at = self.clock.now()  # Spotify may briefly report the source oddly again
        self._resync_pending = False
        self.resyncs += 1

    def _resync(self, ps, cur: int) -> None:
        """Make Spotify pick up the edits.

        Spotify keeps a snapshot of a playlist from when it started playing it,
        so API edits don't reach the queue. Playing the playlist again from the
        same song at the same moment reloads it; the listener hears at most a
        very short blip.
        """
        progress = ps.progress_ms + int((self.clock.now() - ps.observed_at) * 1000)
        self.client.play_context(self.playlist_uri, cur, progress)
        self._after_resync(None, cur)

    def _poor(self, plan, far: float | None = None, average: bool = False) -> bool:
        """No picks, or picks far from the waypoint (any of them, or on `average`): the table ran dry nearby."""
        if not plan.insertions:
            return True
        far = far or 2 * self.selection.search_radius
        dists = [math.dist(t.point, plan.waypoint) for _, t in plan.insertions]
        return (sum(dists) / len(dists) if average else max(dists)) > far

    def _emergency_fill(self, window: list[WindowItem], target: Point, exclude: set[str],
                        already: list, k: int) -> list[tuple[None, CatalogTrack]]:
        """Keep the music going: append songs near the target even if nothing better was found."""
        if k <= 0:
            return []
        taken = exclude | {t.id for _, t in already} | {w.track_id for w in window if w.track_id}
        relaxed = replace(self.selection, min_candidates=max(k, 20))
        context = MusicalContext.of(window)
        idx, _ = rank_candidates(self.table, target, target, target, context, taken, relaxed, settled=True,
                                 languages=self.languages)
        picks = [self.table.tracks[i] for i in idx[:k]]
        if len(picks) < k and self.fallback_table is not None:
            taken |= {t.id for t in picks}
            idx, _ = rank_candidates(self.fallback_table, target, target, target, context, taken, relaxed,
                                     settled=True)
            picks += [self.fallback_table.tracks[i] for i in idx[:k - len(picks)]]
        return [(None, t) for t in picks]

    # ------------------------------------------------------------- song events (from polling)
    def note_started(self, track_id: str) -> None:
        if self.active:
            self.played_ids.add(track_id)
            self._advance_mood(f"spotify:track:{track_id}")
        self.recent.append(track_id)

    def _advance_mood(self, uri: str) -> None:
        """A new song started: drop the songs before it from the last window and re-average at once,
        so the sidebar's mood moves with the song instead of waiting for the next cycle."""
        uris = [u for u, _ in self._window_points]
        if uri not in uris:
            return
        self._window_points = self._window_points[uris.index(uri):]
        pts = [p for _, p in self._window_points[: self.selection.window_size] if p is not None]
        if pts:
            self.mood = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))

    def take_intervention(self, ended_track_id: str) -> tuple[str, Direction | None]:
        direction = self.pending.pop(ended_track_id, None)
        return ("playlist_add", direction) if direction is not None else ("none", None)

    # ------------------------------------------------------------- end
    def end(self, handback: bool = True) -> None:
        """End the session: hand the next songs to Spotify's queue, then delete the playlist."""
        if not self.active:
            return
        if handback:
            try:
                ps = self.client.player_state()
                if ps is not None and ps.context_uri == self.playlist_uri:
                    entries = self.client.playlist_items(self.playlist_id)
                    cur, _ = self._locate(entries, ps.item.uri if ps.item else "")
                    for it in entries[cur + 1: cur + 1 + self.params.handback_count]:
                        self.client.add_to_queue(it.uri)
            except Exception as e:
                log.warning("could not hand the upcoming songs back to your queue: %s", e)
        self._delete_playlist()
        self.status = IDLE
        log.info("session ended")

    def _delete_playlist(self) -> None:
        if self.playlist_id:
            try:
                self.client.delete_playlist(self.playlist_id, self.playlist_uri)
            except Exception as e:
                log.warning("could not delete the session playlist: %s", e)
                return  # keep the state file so the next start retries
            if self.feedback:
                self.feedback.log_intervention(self.clock.now(), "session_end", self.playlist_uri or "", self.mood)
        self.playlist_id = self.playlist_uri = None
        self.pending.clear()
        self._save_state()

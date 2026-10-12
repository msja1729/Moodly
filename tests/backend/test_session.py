"""Phase 2 playlist session against the simulated Spotify: hand-over, reshaping, user actions, ending."""
import asyncio
import json
import math

import numpy as np
import pytest

from backend.actuators import session as session_mod
from backend.actuators.audio import build_pool
from backend.actuators.display import NullBackend
from backend.actuators.session import PlaylistSession, SessionParams, manual_prefix_length
from backend.catalog import CatalogTrack, TrackTable
from backend.daemon import Daemon
from backend.features import FeatureChain, SpotifyFeatureProvider
from backend.feedback import FeedbackLog
from backend.mood_space import distance
from backend.selection import SelectionParams
from backend.spotify.client import MockSpotifyClient, PremiumRequired, demo_catalog
from shared.config_io import load_config

FOCUSED = (0.5, 0.45)


def table_of(catalog):
    return TrackTable(CatalogTrack(t.id, t.name, t.artists, f, t.duration_ms) for t, f in catalog.values())


def loud(catalog, n):
    return sorted(catalog, key=lambda t: -(catalog[t][1].energy + catalog[t][1].valence))[:n]


@pytest.fixture
def big_catalog():
    return demo_catalog(n_side=40)


def make_session(client, catalog, clock, tmp_path=None, **params):
    return PlaylistSession(client, table_of(catalog), None, FeedbackLog(":memory:"), clock,
                           SelectionParams(), SessionParams(**params), rng=np.random.default_rng(0),
                           state_file=(tmp_path / "session.json") if tmp_path else None)


def playing_client(catalog, clock, queued=4, seed=1):
    """The user is playing a loud song and has `queued` more loud songs in Spotify's queue."""
    songs = loud(catalog, 1 + queued)
    client = MockSpotifyClient(catalog, clock, script=[(songs[0], 1.0)], seed=seed, playlist_skip_p=0.0)
    for t in songs[1:]:
        client.add_to_queue(f"spotify:track:{t}")
    return client, songs


def upcoming(client):
    return client.queue_snapshot()


class TestManualPrefix:
    @pytest.mark.parametrize("queue, playlist, n", [
        (["p1", "p2"], ["p1", "p2", "p3"], 0),
        (["m1", "p1", "p2"], ["p1", "p2"], 1),
        (["m1", "m2", "m1", "m2", "c1"], ["m1", "m2", "c1"], 2),  # user songs also copied into the playlist
        (["m1", "m2", "m1", "m2"], ["m1", "m2"], 2),  # right after hand-over: playlist = copies only
        (["p1", "p2", "auto1"], ["p1", "p2"], 0),     # Spotify autoplay after the playlist
        (["auto1", "auto2"], [], 0),  # playlist ran out: Spotify autoplay, not the user's songs
        ([], ["p1"], 0),
    ])
    def test_cases(self, queue, playlist, n):
        assert manual_prefix_length(queue, playlist) == n


class TestStart:
    def test_hand_over_keeps_the_song_and_position(self, catalog, clock, tmp_path):
        client, songs = playing_client(catalog, clock)
        clock.advance(42)
        s = make_session(client, catalog, clock, tmp_path)
        assert s.start()
        assert s.status == session_mod.MANAGING
        entries = [it.id for it in client.playlist_items(s.playlist_id)]
        assert entries == songs  # the current song first, then the user's queue
        uri, offset, position_ms = client.context_starts[-1]
        assert (uri, offset) == (s.playlist_uri, 0) and position_ms == 42_000
        assert client.player_state().item.id == songs[0]
        assert client.shuffle is False
        assert json.loads((tmp_path / "session.json").read_text())["playlist_id"] == s.playlist_id

    def test_waits_when_nothing_is_playing(self, catalog, clock):
        client, _ = playing_client(catalog, clock)
        client.user_pause()
        s = make_session(client, catalog, clock)
        assert not s.start() and s.status == session_mod.WAITING and not client.playlists

    def test_not_premium(self, catalog, clock):
        client, _ = playing_client(catalog, clock)

        def refuse(*_a, **_k):
            raise PremiumRequired("PREMIUM_REQUIRED")

        client.play_context = refuse
        s = make_session(client, catalog, clock)
        assert not s.start()
        assert s.status == session_mod.NOT_PREMIUM and not s.active and client.deleted_playlists

    def test_leftover_playlist_from_a_crash_is_deleted(self, catalog, clock, tmp_path):
        client, _ = playing_client(catalog, clock)
        pid, uri = client.create_playlist("Ambient Mood session")
        (tmp_path / "session.json").write_text(json.dumps({"playlist_id": pid, "uri": uri}))
        make_session(client, catalog, clock, tmp_path).start()
        assert pid in client.deleted_playlists


class TestCycle:
    def test_first_cycle_refills_and_removes_user_queue_copies(self, big_catalog, clock):
        client, songs = playing_client(big_catalog, clock, queued=3)
        s = make_session(client, big_catalog, clock)
        s.start()
        s.cycle(FOCUSED)
        ups = upcoming(client)
        # The user's queued songs play once (from Spotify's queue), not again from the playlist.
        assert ups[:3] == songs[1:] and len(ups) == len(set(ups))
        assert len(ups) >= SelectionParams().min_length
        assert s.mood is not None and s.deviation == pytest.approx(distance(s.mood, FOCUSED))

    def test_added_songs_lead_toward_the_target(self, big_catalog, clock):
        client, _ = playing_client(big_catalog, clock, queued=0)
        s = make_session(client, big_catalog, clock)
        s.start()
        s.cycle(FOCUSED)
        mood = s.mood
        for tid in upcoming(client):
            assert distance(big_catalog[tid][1].point, FOCUSED) < distance(mood, FOCUSED) + 1e-9

    def test_far_songs_are_removed_and_never_come_back(self, big_catalog, clock):
        client, _ = playing_client(big_catalog, clock, queued=0)
        s = make_session(client, big_catalog, clock, recent_exclusion=0)
        s.start()
        s.cycle(FOCUSED)
        far = min(big_catalog, key=lambda t: big_catalog[t][1].energy + big_catalog[t][1].valence)  # quiet & sad
        client.playlist_add(s.playlist_id, [f"spotify:track:{far}"])  # far from the loud mood AND from Focused
        s.cycle(FOCUSED, "test")
        assert far not in upcoming(client) and far in s.removed_ids
        for _ in range(5):
            s.cycle(FOCUSED, "test")
        assert far not in upcoming(client)

    def test_now_playing_and_next_are_never_touched(self, big_catalog, clock):
        client, _ = playing_client(big_catalog, clock, queued=0)
        s = make_session(client, big_catalog, clock)
        s.start()
        s.cycle(FOCUSED)
        now, nxt = client.player_state().item.id, upcoming(client)[0]
        for _ in range(4):
            s.cycle(FOCUSED, "test")
        assert client.player_state().item.id == now and upcoming(client)[0] == nxt

    def test_user_pause_leaves_the_session_idle(self, big_catalog, clock):
        client, _ = playing_client(big_catalog, clock)
        s = make_session(client, big_catalog, clock)
        s.start()
        s.cycle(FOCUSED)
        before = list(client.playlists[s.playlist_id])
        client.user_pause()
        s.cycle(FOCUSED)
        assert s.status == session_mod.USER_PAUSED and client.playlists[s.playlist_id] == before
        client.user_resume()
        s.cycle(FOCUSED)
        assert s.status == session_mod.MANAGING

    def test_user_starting_other_music_ends_the_session(self, big_catalog, clock):
        client, _ = playing_client(big_catalog, clock)
        s = make_session(client, big_catalog, clock)
        s.start()
        client.user_switch_context(["demo0000", "demo0001"])
        queued_before = list(client.queued_log)
        s.cycle(FOCUSED)
        assert s.active  # inside the grace period right after hand-over: not judged yet
        clock.advance(SessionParams().takeover_grace_s)
        s.cycle(FOCUSED)
        assert s.active  # seen once: could still be a glitch
        s.cycle(FOCUSED)
        assert s.status == session_mod.USER_TOOK_OVER and not s.active
        assert client.deleted_playlists and client.queued_log == queued_before  # nothing handed back
        assert client.player_state().item.id == "demo0000"  # the user's music keeps playing


class TestEnd:
    def test_hand_back_then_delete(self, big_catalog, clock, tmp_path):
        client, _ = playing_client(big_catalog, clock, queued=0)
        s = make_session(client, big_catalog, clock, tmp_path, handback_count=5)
        s.start()
        s.cycle(FOCUSED)
        nxt = upcoming(client)[:5]
        playing, queued_before = client.player_state().item.id, list(client.queued_log)
        s.end()
        assert not s.active and s.status == session_mod.IDLE and client.deleted_playlists
        # Off the session playlist: the same song goes on, then the next 5 as a plain list; nothing queued.
        assert client.context is None and client.played_lists[-1] == [playing] + nxt
        assert client.player_state().item.id == playing and client.queued_log == queued_before
        assert not (tmp_path / "session.json").exists()
        clock.advance(3600)
        assert client.player_state() is not None  # still playing after the playlist is gone


class TestDaemonSessionMode:
    def make_daemon(self, tmp_config, client, catalog, clock, **session_cfg):
        cfg = load_config(tmp_config)
        cfg["session"].update(session_cfg)
        chain = FeatureChain([SpotifyFeatureProvider(client)])
        feedback = FeedbackLog(":memory:")
        s = PlaylistSession(client, table_of(catalog), chain, feedback, clock, SelectionParams.from_config(cfg),
                            SessionParams.from_config(cfg), rng=np.random.default_rng(1))
        return Daemon(tmp_config, cfg, client, chain, feedback, build_pool(client, chain, None), clock,
                      display_backend=NullBackend(), state_path=tmp_config.parent / "state.json",
                      rng=np.random.default_rng(0), session=s), s

    @pytest.mark.parametrize("mode", ["path", "random"])
    def test_long_session_converges_and_music_never_stops(self, tmp_config, big_catalog, clock, mode):
        client, _ = playing_client(big_catalog, clock, queued=5, seed=5)
        client.playlist_skip_p = 0.1
        daemon, s = self.make_daemon(tmp_config, client, big_catalog, clock, insert_mode=mode)
        devs = []

        async def run():
            await daemon.apply_target("startup")
            for _ in range(24):  # 4 hours in 10-minute steps
                await daemon.run_for(600)
                devs.append(s.deviation)
                assert not client.stopped
                assert len(client.queue_snapshot()) >= SelectionParams().min_length - 1

        asyncio.run(run())
        assert devs[0] > 0.4
        if mode == "path":
            assert devs[-1] < 0.08                   # settled near the target
            assert devs[12] < devs[0] * 0.6          # well on the way after 2 hours
        else:
            # Random positions: new songs are heard sooner, but the original songs stay in the
            # queue (and in the average) longer, so the mood moves more slowly.
            assert devs[-1] < devs[0] * 0.6
        state = json.loads((tmp_config.parent / "state.json").read_text())
        assert state["mode"] == "session" and state["session"]["status"] == "managing"
        assert state["mood"] == pytest.approx(list(s.mood))  # the sidebar shows the queue average

    def test_pause_nudging_ends_the_session(self, tmp_config, big_catalog, clock):
        client, _ = playing_client(big_catalog, clock)
        daemon, s = self.make_daemon(tmp_config, client, big_catalog, clock)

        async def run():
            await daemon.apply_target("startup")
            await daemon.run_for(300)
            assert s.active
            daemon.paused = True
            await daemon.apply_target("config_change")

        asyncio.run(run())
        assert not s.active and client.deleted_playlists

    def test_playlist_inserts_feed_the_markov_skip_learning(self, tmp_config, big_catalog, clock):
        client, _ = playing_client(big_catalog, clock, seed=2)
        client.playlist_skip_p = 0.5
        daemon, s = self.make_daemon(tmp_config, client, big_catalog, clock)

        async def run():
            await daemon.apply_target("startup")
            await daemon.run_for(3600)

        asyncio.run(run())
        stats = daemon.feedback.skip_stats()
        assert stats and sum(c + k for c, k in stats.values()) > 0


def test_daemon_forgets_an_account_spotify_says_is_not_premium(tmp_config, big_catalog, clock):
    client, _ = playing_client(big_catalog, clock)

    def refuse(*_a, **_k):
        raise PremiumRequired("PREMIUM_REQUIRED")

    client.play_context = refuse
    daemon, s = TestDaemonSessionMode().make_daemon(tmp_config, client, big_catalog, clock)
    forgotten = []
    daemon.on_not_premium = lambda: forgotten.append(True)
    asyncio.run(daemon.apply_target("startup"))
    assert s.status == session_mod.NOT_PREMIUM and forgotten == [True]
    state = daemon.state()
    assert state["session"]["status"] == "not_premium"


class TestLiveRunFixes:
    """Problems seen on the first live runs (2026-10-08)."""

    def test_stale_source_after_hand_over_is_not_a_takeover(self, big_catalog, clock):
        client, _ = playing_client(big_catalog, clock)
        client.context_report_lag_s = 3.0  # Spotify still reports the old source for a few seconds
        s = make_session(client, big_catalog, clock)
        assert s.start()
        s.cycle(FOCUSED, "start")
        assert s.active and s.status == session_mod.MANAGING

    def test_overlapping_steps_create_only_one_playlist(self, tmp_config, big_catalog, clock):
        client, _ = playing_client(big_catalog, clock)
        daemon, s = TestDaemonSessionMode().make_daemon(tmp_config, client, big_catalog, clock)

        async def run():
            await asyncio.gather(daemon._session_step("target_change"), daemon._session_step("timer"),
                                 daemon._session_step("track_change"))

        asyncio.run(run())
        assert len(client.playlists) == 1 and s.active

    def test_new_target_keeps_songs_near_the_mood(self, big_catalog, clock):
        # Only songs far from both the mood and the target are removed, also right after a new target.
        client, _ = playing_client(big_catalog, clock, queued=0)
        s = make_session(client, big_catalog, clock)
        s.start()
        loud_songs = loud(big_catalog, 17)[1:]  # 16 loud, happy songs: that's the mood now
        client.playlist_add(s.playlist_id, [f"spotify:track:{t}" for t in loud_songs])
        wind_down = (0.4, 0.15)
        s.cycle(wind_down, "target_change")
        assert s.last_removed == []  # far from Wind Down, but they are what the user is listening to
        s.cycle(wind_down, "timer")
        assert s.last_removed == []

    def test_new_target_removes_songs_far_from_mood_and_target(self, big_catalog, clock):
        client, _ = playing_client(big_catalog, clock, queued=0)
        s = make_session(client, big_catalog, clock)
        s.start()
        calm = sorted(big_catalog, key=lambda t: math.dist(big_catalog[t][1].point, (0.5, 0.45)))[:13]
        sad = sorted(big_catalog, key=lambda t: math.dist(big_catalog[t][1].point, (0.05, 0.05)))[:4]
        client.playlist_add(s.playlist_id, [f"spotify:track:{t}" for t in calm + sad])
        s.cycle((0.9, 0.9), "target_change")
        removed = {r.split(" - ")[-1] for r in s.last_removed}
        sad_names = {big_catalog[t][0].name for t in sad}
        assert removed and removed <= sad_names  # only the far-off sad songs, capped
        assert len(s.last_removed) <= SelectionParams().max_removals_on_retarget

    def test_random_insert_positions_are_in_the_middle(self):
        from backend.selection import WindowItem, random_positions
        from backend.catalog import CatalogTrack
        from backend.features import Features

        items = [WindowItem(f"spotify:track:s{i}") for i in range(12)]
        picks = [CatalogTrack(f"p{i}", "P", f"A{i}", Features(0.5, 0.5, 0.5)) for i in range(200)]
        out = random_positions(items, picks[:1] * 1, SelectionParams(), np.random.default_rng(0))
        positions = [random_positions(items, [p], SelectionParams(), np.random.default_rng(i))[0][0]
                     for i, p in enumerate(picks)]
        assert min(positions) == 2 and max(positions) == len(items) - 1  # never 0/1, never appended last
        assert len(set(positions)) > 5 and out[0][0] >= 2


def test_one_song_start_with_spotify_autoplay_queue_gets_filled(big_catalog, clock):
    """Live run 3: the user's queue was empty, Spotify's queue held 20 autoplay songs."""
    client, songs = playing_client(big_catalog, clock, queued=0)
    s = make_session(client, big_catalog, clock)
    s.start()
    autoplay = [f"spotify:track:{t}" for t in loud(big_catalog, 40)[20:40]]
    real_queue = client.queue
    client.queue = lambda: [client._item(u) for u in autoplay]  # Spotify shows only autoplay songs
    s.cycle(FOCUSED, "start")
    client.queue = real_queue
    playlist = client.playlists[s.playlist_id]
    assert len(playlist) - 1 >= SelectionParams().min_length  # the playlist itself was filled


class TestSpotifyFollowsEdits:
    """Live run 4: the playlist was edited but Spotify's queue didn't change."""

    def test_queue_reflects_edits_after_resync(self, big_catalog, clock):
        client, _ = playing_client(big_catalog, clock, queued=0)
        client.follows_playlist_edits = False  # like the real Spotify app
        s = make_session(client, big_catalog, clock)
        s.start()
        playing = client.player_state().item.id
        progress = client.player_state().progress_ms
        s.cycle(FOCUSED, "start")
        playlist_next = [uri.rsplit(":", 1)[-1] for _, uri in client.playlists[s.playlist_id]][1:]
        assert client.queue_snapshot() == playlist_next  # the queue now shows the edited playlist
        assert s.resyncs == 1
        state = client.player_state()
        assert state.item.id == playing and state.progress_ms >= progress  # same song, no restart

    def test_without_resync_the_queue_is_stale(self, big_catalog, clock):
        client, _ = playing_client(big_catalog, clock, queued=0)
        client.follows_playlist_edits = False
        s = make_session(client, big_catalog, clock, resync_after_edits=False)
        s.start()
        s.cycle(FOCUSED, "start")
        assert client.queue_snapshot() == []  # what the user saw: edits never reached the queue

    def test_no_resync_when_nothing_changed(self, big_catalog, clock):
        client, _ = playing_client(big_catalog, clock, queued=0)
        client.follows_playlist_edits = False
        s = make_session(client, big_catalog, clock)
        s.start()
        s.cycle(FOCUSED, "start")
        before = len(client.context_starts)
        s.cycle(FOCUSED, "timer")  # queue full: may change nothing
        if not s.last_added and not s.last_removed:
            assert len(client.context_starts) == before

    def test_long_session_with_snapshot_playback(self, tmp_config, big_catalog, clock):
        client, _ = playing_client(big_catalog, clock, queued=3, seed=5)
        client.follows_playlist_edits = False
        daemon, s = TestDaemonSessionMode().make_daemon(tmp_config, client, big_catalog, clock)

        async def run():
            await daemon.apply_target("startup")
            for _ in range(12):
                await daemon.run_for(600)
                assert not client.stopped
                assert len(client.queue_snapshot()) >= SelectionParams().min_length - 1

        asyncio.run(run())
        assert s.active and s.resyncs > 0


@pytest.mark.parametrize("follows", [False, True])
def test_user_queued_songs_play_in_full_and_music_never_stops(tmp_config, big_catalog, clock, follows):
    """Live run 4 follow-up: stale snapshots must not cut the user's songs or empty the playlist."""
    client, songs = playing_client(big_catalog, clock, queued=4, seed=3)
    client.follows_playlist_edits = follows
    client.playlist_skip_p = client.queue_skip_p = 0.0  # the simulated user skips nothing
    daemon, s = TestDaemonSessionMode().make_daemon(tmp_config, client, big_catalog, clock)

    async def run():
        await daemon.apply_target("startup")
        for _ in range(12):  # 2 hours
            await daemon.run_for(600)
            assert not client.stopped and s.active

    asyncio.run(run())
    played = [(t, skipped) for t, skipped in client.plays]
    for t in songs[1:]:  # the user's queued songs: each played once, to the end
        assert [sk for pt, sk in played if pt == t] == [False]
    assert len(client.playlists[s.playlist_id]) > 10


def test_mood_moves_as_soon_as_the_next_song_starts(big_catalog, clock):
    client, songs = playing_client(big_catalog, clock, queued=4)
    s = make_session(client, big_catalog, clock)
    s.start()
    s.cycle(FOCUSED)
    before = s.mood
    nxt = upcoming(client)[0].rsplit(":", 1)[-1]
    t = big_catalog[nxt][0]
    s.note_started(nxt, t.name, t.artists)  # no cycle: the mood is re-averaged from the window that's left
    assert s.mood != before
    # The song playing + the user's own songs ahead (Moodly's picks only count once they play).
    pts = [p for i, (_, p, ours) in enumerate(s._window_points) if p and (i == 0 or not ours)]
    assert s.mood == pytest.approx((sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)))


def test_mood_trace_records_cycles_songs_and_end_only_when_enabled(big_catalog, clock, tmp_path, monkeypatch):
    client, songs = playing_client(big_catalog, clock, queued=4)
    assert make_session(client, big_catalog, clock).trace is None  # off by default
    path = tmp_path / "trace.jsonl"
    monkeypatch.setenv("AMBIENT_MOOD_TRACE", str(path))
    s = make_session(client, big_catalog, clock)
    s.start()
    s.cycle(FOCUSED, "start")
    n_cycles = len(path.read_text(encoding="utf-8").splitlines())
    s.trace.cycle(reason="timer", target=FOCUSED, plan=type("P", (), {"mood": s.mood, "waypoint": None, "deviation": 0,
                  "direction": None, "settled": False})(), window=[], added=[], removed=[], preparing=False,
                  needs_more_songs=False, queue_size=0)
    s.trace.cycle(reason="timer", target=FOCUSED, plan=type("P", (), {"mood": s.mood, "waypoint": None, "deviation": 0,
                  "direction": None, "settled": False})(), window=[], added=[], removed=[], preparing=False,
                  needs_more_songs=False, queue_size=0)  # nothing changed: not written again
    assert len(path.read_text(encoding="utf-8").splitlines()) == n_cycles + 1
    nxt = upcoming(client)[0].rsplit(":", 1)[-1]
    s.note_started(nxt, big_catalog[nxt][0].name, big_catalog[nxt][0].artists)
    s.end()
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [e["kind"] for e in events] == ["cycle", "cycle", "song", "end"]
    c = events[0]
    assert c["reason"] == "start" and c["target"] == list(FOCUSED) and len(c["mood"]) == 2
    assert c["waypoint"] is not None and c["added"]
    assert all(len(a["point"]) == 2 and a["name"] for a in c["added"])
    assert len(c["window"]) >= 5 and all(w["point"] for w in c["window"])
    assert events[2]["mood"] == pytest.approx(list(s.mood), abs=1e-4)


def test_sidebar_player_buttons_pause_resume_and_skip(tmp_config, big_catalog, clock):
    client, songs = playing_client(big_catalog, clock, queued=3)
    d, _ = TestDaemonSessionMode().make_daemon(tmp_config, client, big_catalog, clock)
    cmd = d.player_command_path

    def press(name):
        cmd.write_text(json.dumps({"cmd": name}), encoding="utf-8")
        asyncio.run(d.player_command())
        assert not cmd.exists()  # handled once
        return json.loads(d.state_path.read_text(encoding="utf-8"))

    first = press("pause")
    assert first["is_playing"] is False and first["track_name"]
    assert press("play")["is_playing"] is True
    after = press("next")
    assert after["track_name"] != first["track_name"] and client.plays[-1] == (songs[0], True)  # a skip


def test_cover_url_picks_the_300px_album_image():
    from backend.spotify.client import _track_from_item
    item = {"type": "track", "id": "x", "name": "n", "artists": [{"name": "a"}], "duration_ms": 1000,
            "album": {"images": [{"url": "big", "width": 640}, {"url": "mid", "width": 300},
                                 {"url": "small", "width": 64}]}}
    t = _track_from_item(item)
    assert t.image == "mid" and t == _track_from_item({**item, "album": {}})  # the image isn't part of identity


def _queue_with(client, transform):
    """Make Spotify's queue report its songs through `transform` (e.g. relinked ids, a reshuffle)."""
    real = client.queue

    def queue():
        return transform(real())
    client.queue = queue


def test_relinked_ids_in_spotify_queue_remove_nothing(big_catalog, clock):
    # Spotify (e.g. in India) reports the same song under another id; it must not look like the user's queue.
    from backend.spotify.client import Item
    client, _ = playing_client(big_catalog, clock, queued=0)
    s = make_session(client, big_catalog, clock)
    s.start()
    near = sorted(big_catalog, key=lambda t: math.dist(big_catalog[t][1].point, (0.9, 0.9)))[1:15]
    client.playlist_add(s.playlist_id, [f"spotify:track:{t}" for t in near])
    s.cycle((0.9, 0.9), "timer")
    _queue_with(client, lambda items: [Item(it.uri + "x", it.name, it.artists, it.duration_ms) for it in items])
    for _ in range(3):
        clock.advance(30)
        s.cycle((0.9, 0.9), "timer")
        assert not s.last_removed


def test_misread_queue_never_removes_playlist_songs(big_catalog, clock):
    client, _ = playing_client(big_catalog, clock, queued=0)
    s = make_session(client, big_catalog, clock)
    s.start()
    near = sorted(big_catalog, key=lambda t: math.dist(big_catalog[t][1].point, (0.9, 0.9)))[1:15]
    client.playlist_add(s.playlist_id, [f"spotify:track:{t}" for t in near])
    s.cycle((0.9, 0.9), "timer")
    _queue_with(client, lambda items: list(reversed(items)))  # nothing lines up with the playlist
    clock.advance(30)
    s.cycle((0.9, 0.9), "timer")
    assert not s.last_removed


def test_mood_is_what_the_user_listens_to_not_moodlys_picks(big_catalog, clock):
    client, songs = playing_client(big_catalog, clock, queued=4)
    s = make_session(client, big_catalog, clock)
    s.start()
    s.cycle((0.1, 0.1), "ready")  # Moodly adds sad, calm songs
    assert s.last_added
    own = [big_catalog[t][1].point for t in songs]  # the song playing + the user's 4 queued songs
    assert s.mood == pytest.approx((sum(p[0] for p in own) / 5, sum(p[1] for p in own) / 5))


def test_edits_never_cut_the_song_playing_when_spotify_counts_a_hidden_entry(big_catalog, clock):
    # Spotify can count a playlist entry its API doesn't list. Restarting "at position N" then lands on the
    # next song a second after it started; restarting at the song itself keeps it playing.
    client, songs = playing_client(big_catalog, clock, queued=4)
    s = make_session(client, big_catalog, clock)
    s.start()
    hidden = next(t for t in big_catalog if t not in songs)
    client.playlists[s.playlist_id].insert(0, [client._new_uid(), f"spotify:track:{hidden}"])
    client.unlisted = {f"spotify:track:{hidden}"}
    playing = client.player_state().item.uri
    clock.advance(5)
    s.cycle((0.1, 0.1), "ready")  # adds songs, then makes Spotify reload the playlist
    assert s.last_added and client.player_state().item.uri == playing
    assert client.player_state().progress_ms >= 5000  # same moment, not restarted


def test_a_new_session_forgets_the_last_one(big_catalog, clock):
    client, songs = playing_client(big_catalog, clock, queued=4)
    s = make_session(client, big_catalog, clock)
    s.start()
    s.cycle((0.1, 0.1), "ready")
    t = big_catalog[songs[1]][0]
    s.note_started(songs[1], t.name, t.artists)
    assert s.added_keys and len(s.played_keys) == 2
    s.end()
    assert s.start()
    now = client.player_state().item
    assert s.played_keys == {now.key} and not s.added_keys and not s.removed_ids and not s.pending


def test_a_slow_session_step_never_holds_up_the_sidebar(tmp_config, big_catalog, clock):
    # Live, steps run in the background: Spotify keeps being polled and a new song is shown at once,
    # even while a step is still adding songs.
    import threading
    client, songs = playing_client(big_catalog, clock, queued=4)
    d, s = TestDaemonSessionMode().make_daemon(tmp_config, client, big_catalog, clock)
    d.background_steps = True
    release = threading.Event()
    real_cycle = s.cycle

    def slow_cycle(*args, **kwargs):
        release.wait(5)
        return real_cycle(*args, **kwargs)

    async def run():
        await d.apply_target("startup")
        s.start()
        s.cycle = slow_cycle
        polls_before = client.calls
        await d.run_for(30)  # a song change and timer steps happen; every step is stuck
        assert client.calls - polls_before > 20  # polling went on regardless
        state = json.loads(d.state_path.read_text(encoding="utf-8"))
        assert state["track_name"] == client.player_state().item.name  # the song playing now is shown
        release.set()
        await asyncio.gather(*list(d._tasks))

    asyncio.run(run())
    d.shutdown()



def test_playlist_session_starts_fresh_without_the_users_queue(tmp_config, big_catalog, clock):
    from shared.config_io import set_song_source
    client, songs = playing_client(big_catalog, clock, queued=4)
    d, s = TestDaemonSessionMode().make_daemon(tmp_config, client, big_catalog, clock)
    liked = [t for t in big_catalog if t not in songs][:30]

    class Rec:  # just enough of the recommender: a Liked Songs source
        custom, source, params = True, {"kind": "liked"}, None

        def source_seed(self, k):
            return [(f"spotify:track:{t}", big_catalog[t][0].name, big_catalog[t][0].artists,
                     big_catalog[t][0].duration_ms) for t in liked[:k]]

    d.recommender = Rec()

    async def run():
        await d._prepare_start(s)
        assert s.start()
        entries = [it.id for it in client.playlist_items(s.playlist_id)]
        assert entries == [songs[0]] + liked[:2]  # the song playing + 2 from Liked Songs; not the user's queue

    asyncio.run(run())

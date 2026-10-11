"""The recommender's pool inside Moodly: language filter, taste in the score, waiting for the pool,
and the session lifecycle (start / target change / new session / end + clear)."""
import asyncio
import math
import threading

import numpy as np
import pytest

from backend.actuators.audio import build_pool
from backend.actuators.display import NullBackend
from backend.actuators.session import PlaylistSession, SessionParams
from backend.catalog import CatalogTrack, TrackTable
from backend.daemon import Daemon
from backend.features import FeatureChain, Features, SpotifyFeatureProvider
from backend.feedback import FeedbackLog
from backend.mood_space import distance
from backend.selection import MusicalContext, SelectionParams, rank_candidates
from backend.spotify.client import MockSpotifyClient, demo_catalog
from recommender import RecommenderParams
from shared.config_io import load_config, set_paused, set_target_mood, start_new_session

FOCUSED = (0.5, 0.45)


def song(i, v, e, language="", taste=0.0):
    return CatalogTrack(f"s{i}", f"Song {i}", f"Artist {i}", Features(v, e, 0.3), language=language, taste=taste)


def test_language_filter_and_taste_in_the_score():
    table = TrackTable([song(1, 0.5, 0.5, "te", 0.0), song(2, 0.5, 0.5, "te", 1.0),
                        song(3, 0.5, 0.5, "hi", 1.0), song(4, 0.5, 0.5, "en", 1.0)])
    ctx = MusicalContext(None, None, None, None, None, None)
    idx, _ = rank_candidates(table, (0.5, 0.5), (0.9, 0.9), (0.2, 0.2), ctx, set(), SelectionParams(),
                             languages=frozenset({"te", "hi"}))
    ids = [table.tracks[i].id for i in idx]
    assert set(ids) == {"s1", "s2", "s3"}       # English filtered out
    assert ids.index("s2") < ids.index("s1")    # same mood fit: the better taste match first


@pytest.fixture
def catalog():
    return demo_catalog(n_side=40)


def table_of(catalog, language="", taste=0.0, keep=lambda f: True):
    return TrackTable(CatalogTrack(t.id, t.name, t.artists, f, t.duration_ms, language=language, taste=taste)
                      for t, f in catalog.values() if keep(f))


def playing(catalog, clock, queued=0):
    """A loud song playing, with `queued` more loud songs in the user's own Spotify queue."""
    loud = sorted(catalog, key=lambda t: -(catalog[t][1].energy + catalog[t][1].valence))[:1 + queued]
    client = MockSpotifyClient(catalog, clock, script=[(loud[0], 1.0)], seed=1, playlist_skip_p=0.0)
    for t in loud[1:]:
        client.add_to_queue(f"spotify:track:{t}")
    return client


def session(client, catalog, clock):
    return PlaylistSession(client, table_of(catalog), None, FeedbackLog(":memory:"), clock, SelectionParams(),
                           SessionParams(), rng=np.random.default_rng(0))


def added_ids(s, table):
    names = {f"{t.artists} - {t.name}": t.id for t in table.tracks}
    return {names[n] for n in s.last_added if n in names}


def near_target(catalog):
    return table_of(catalog, "te", 1.0, keep=lambda f: distance(f.point, FOCUSED) < 0.35)


def test_session_picks_from_the_pool(catalog, clock):
    s = session(playing(catalog, clock), catalog, clock)
    pool = near_target(catalog)
    s.use_tables(pool, ["te"], table_of(catalog))
    s.start()
    s.cycle(FOCUSED)
    assert s.last_added and len(added_ids(s, pool)) == len(s.last_added)  # every added song is from the pool


def test_nothing_near_the_step_means_wait_not_catalog(catalog, clock):
    # 9 songs in the user's own queue: the music is safe, so nothing needs adding to keep it going.
    s = session(playing(catalog, clock, queued=9), catalog, clock)
    quiet_only = table_of(catalog, "te", 1.0, keep=lambda f: f.energy < 0.08 and f.valence < 0.08)
    s.use_tables(quiet_only, ["te"], table_of(catalog))
    s.start()
    s.cycle(FOCUSED, "timer")
    assert s.last_added == []
    assert s.needs_more_songs  # the daemon grows the pool before adding


def test_preparing_measures_but_adds_nothing(catalog, clock):
    s = session(playing(catalog, clock, queued=9), catalog, clock)
    s.use_tables(near_target(catalog), ["te"], table_of(catalog))
    s.preparing = True
    s.start()
    s.cycle(FOCUSED, "start")
    assert s.last_added == [] and s.mood is not None


def test_preparing_still_keeps_the_music_going(catalog, clock):
    client = playing(catalog, clock, queued=0)  # only the current song: the playlist would run out
    s = session(client, catalog, clock)
    pool = near_target(catalog)
    s.use_tables(pool, ["te"], table_of(catalog))
    s.preparing = True
    s.start()
    s.cycle(FOCUSED, "start")
    assert len(s.last_added) == SessionParams().prepare_min_upcoming
    assert len(added_ids(s, pool)) == len(s.last_added)  # from the pool first


class FakeRecommender:
    """The Recommender interface the daemon uses, with an instant refresh."""

    def __init__(self, pool):
        self.full = pool
        self.pool = TrackTable([])
        self.params = RecommenderParams(ready_songs=5, prepare_max_s=600, refresh_s=600)
        self.languages = ["te", "hi"]
        self.status = "ready"
        self.stop = threading.Event()
        self.refreshes = 0
        self.cleared = 0
        self.goals = []
        self.target = self.mood = None

    def refresh(self):
        self.refreshes += 1
        self.pool = TrackTable([t for t in self.full.tracks if self.is_useful(t.point)])
        return self.pool

    def table(self):
        return self.pool

    def set_goal(self, target, mood):
        self.target, self.mood = target, mood
        self.goals.append(tuple(target))

    def is_useful(self, point, mood=None, target=None):
        target, mood = target or self.target, mood or self.mood
        return target is None or mood is None or math.dist(point, target) < max(math.dist(mood, target), 0.15)

    def useful_count(self, mood=None, target=None):
        return sum(self.is_useful(t.point, mood, target) for t in self.pool.tracks)

    def prune(self, mood=None, target=None):
        self.pool = TrackTable([t for t in self.pool.tracks if self.is_useful(t.point, mood, target)])
        return len(self.pool)

    def clear(self):
        self.cleared += 1
        self.pool = TrackTable([])

    def language_names(self):
        return ["Telugu", "Hindi"]

    def want_features(self, ids):
        pass


def make_daemon(tmp_config, catalog, clock):
    client = playing(catalog, clock)
    set_paused(True, tmp_config)  # as at launch: no session until "Start session"
    cfg = load_config(tmp_config)
    chain = FeatureChain([SpotifyFeatureProvider(client)])
    s = session(client, catalog, clock)
    rec = FakeRecommender(table_of(catalog, "te", 1.0))
    daemon = Daemon(tmp_config, cfg, client, chain, FeedbackLog(":memory:"), build_pool(client, chain, None), clock,
                    display_backend=NullBackend(), state_path=tmp_config.parent / "state.json",
                    rng=np.random.default_rng(0), session=s, recommender=rec)
    return daemon, s, rec, client


async def settle(daemon):
    """Let the background refresh finish and the daemon notice it."""
    if daemon._rec_thread is not None:
        await asyncio.to_thread(daemon._rec_thread.join, 5)
    await daemon.run_for(1)


def test_session_lifecycle(tmp_config, catalog, clock):
    daemon, s, rec, client = make_daemon(tmp_config, catalog, clock)

    async def run():
        await daemon.apply_target("startup")
        await daemon.run_for(5)
        assert not s.active and rec.refreshes == 0  # nothing happens before "Start session"

        # Start session: the pool is prepared first; nothing is added meanwhile (except to keep music going).
        set_paused(False, tmp_config)
        await daemon.reload_config()
        assert s.active and s.preparing and rec.goals[-1] == FOCUSED
        added_while_preparing = list(s.last_added)
        assert len(added_while_preparing) <= SessionParams().prepare_min_upcoming
        state = daemon.state()
        assert state["session"]["preparing"] and state["recommender"]["ready_songs"] == 5
        await settle(daemon)
        assert not s.preparing and s.table is rec.pool  # ready: the session picks from the pool
        assert s.last_added and len(added_ids(s, rec.pool)) == len(s.last_added)

        # A new target: prepare again for it.
        set_target_mood("wind_down", tmp_config)
        await daemon.reload_config()
        assert s.preparing and rec.goals[-1] == daemon.target
        await settle(daemon)
        assert not s.preparing

        # New session: the current one ends (pool cleared), a new one starts and prepares.
        cleared = rec.cleared
        start_new_session(tmp_config)
        await daemon.reload_config()
        assert rec.cleared == cleared + 1
        assert client.deleted_playlists and s.active and s.preparing

        # End session: playlist deleted, pool cleared, no more refreshes.
        set_paused(True, tmp_config)
        await daemon.reload_config()
        assert not s.active and rec.cleared == cleared + 2 and not s.preparing
        refreshes = rec.refreshes
        await daemon.run_for(1200)
        assert rec.refreshes == refreshes

    asyncio.run(run())
    daemon.shutdown()


def test_preparing_ends_after_the_time_limit(tmp_config, catalog, clock):
    daemon, s, rec, _ = make_daemon(tmp_config, catalog, clock)
    rec.full = TrackTable([])  # the pool never fills

    async def run():
        set_paused(False, tmp_config)
        await daemon.reload_config()
        await settle(daemon)
        assert s.preparing
        await daemon.run_for(rec.params.prepare_max_s + 5)
        assert not s.preparing

    asyncio.run(run())
    daemon.shutdown()

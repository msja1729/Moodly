"""The recommender's pool inside Moodly: language filter, taste in the score, catalog fallback, daemon wiring."""
import asyncio
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
from shared.config_io import load_config

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


def playing(catalog, clock):
    loud = sorted(catalog, key=lambda t: -(catalog[t][1].energy + catalog[t][1].valence))[:1]
    return MockSpotifyClient(catalog, clock, script=[(loud[0], 1.0)], seed=1, playlist_skip_p=0.0)


def session(client, catalog, clock):
    return PlaylistSession(client, table_of(catalog), None, FeedbackLog(":memory:"), clock, SelectionParams(),
                           SessionParams(), rng=np.random.default_rng(0))


def added_ids(s, table):
    names = {f"{t.artists} - {t.name}": t.id for t in table.tracks}
    return {names[n] for n in s.last_added if n in names}


def test_session_picks_from_the_pool(catalog, clock):
    client = playing(catalog, clock)
    s = session(client, catalog, clock)
    near = table_of(catalog, "te", 1.0, keep=lambda f: distance(f.point, FOCUSED) < 0.35)
    s.use_tables(near, ["te"], table_of(catalog))
    s.start()
    s.cycle(FOCUSED)
    assert s.last_added and added_ids(s, near) == {n for n in added_ids(s, near)}
    assert len(added_ids(s, near)) == len(s.last_added)  # every added song came from the pool


def test_session_falls_back_to_the_catalog(catalog, clock):
    client = playing(catalog, clock)
    s = session(client, catalog, clock)
    # The pool only has very quiet songs: nowhere near the next step from a loud start.
    quiet_only = table_of(catalog, "te", 1.0, keep=lambda f: f.energy < 0.08 and f.valence < 0.08)
    full = table_of(catalog)
    s.use_tables(quiet_only, ["te"], full)
    s.start()
    s.cycle(FOCUSED)
    from_catalog = added_ids(s, full) - {t.id for t in quiet_only.tracks}
    assert from_catalog  # the step toward the target came from the catalog


class FakeRecommender:
    def __init__(self, pool):
        self.pool = pool
        self.params = RecommenderParams(min_pool=5, refresh_s=600)
        self.languages = ["te", "hi"]
        self.status = "ready"
        self.stop = threading.Event()
        self.refreshed = threading.Event()
        self.wanted = []

    def refresh(self):
        self.refreshed.set()
        return self.pool

    def table(self):
        return self.pool if self.refreshed.is_set() else TrackTable([])

    def language_names(self):
        return ["Telugu", "Hindi"]

    def want_features(self, ids):
        self.wanted += ids


def test_daemon_refreshes_in_the_background_and_hands_the_pool_over(tmp_config, catalog, clock):
    client = playing(catalog, clock)
    cfg = load_config(tmp_config)
    chain = FeatureChain([SpotifyFeatureProvider(client)])
    s = session(client, catalog, clock)
    catalog_table = s.table
    pool = table_of(catalog, "te", 1.0, keep=lambda f: distance(f.point, FOCUSED) < 0.35)
    rec = FakeRecommender(pool)
    daemon = Daemon(tmp_config, cfg, client, chain, FeedbackLog(":memory:"), build_pool(client, chain, None), clock,
                    display_backend=NullBackend(), state_path=tmp_config.parent / "state.json",
                    rng=np.random.default_rng(0), session=s, recommender=rec)
    assert s.on_missing_features == rec.want_features

    async def run():
        await daemon.run_for(1)            # starts the refresh thread
        assert rec.refreshed.wait(5)
        daemon._rec_thread.join(5)
        await daemon.run_for(1)            # hands the finished pool to the session

    asyncio.run(run())
    assert s.table is pool and s.languages == frozenset({"te", "hi"}) and s.fallback_table is catalog_table
    assert daemon.state()["recommender"] == {"status": "ready", "languages": ["Telugu", "Hindi"], "pool": len(pool)}
    daemon.shutdown()
    assert rec.stop.is_set()


def test_a_small_pool_keeps_the_catalog_in_charge(tmp_config, catalog, clock):
    client = playing(catalog, clock)
    cfg = load_config(tmp_config)
    chain = FeatureChain([SpotifyFeatureProvider(client)])
    s = session(client, catalog, clock)
    catalog_table = s.table
    rec = FakeRecommender(TrackTable([song(1, 0.5, 0.5, "te", 1.0)]))  # below min_pool
    daemon = Daemon(tmp_config, cfg, client, chain, FeedbackLog(":memory:"), build_pool(client, chain, None), clock,
                    display_backend=NullBackend(), rng=np.random.default_rng(0), session=s, recommender=rec)
    rec.refresh()
    daemon._rec_ready = True
    daemon._apply_rec()
    assert s.table is catalog_table and s.languages is None

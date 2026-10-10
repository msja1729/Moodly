"""Config I/O, feature providers, inference, telemetry and the daemon loop."""
import asyncio
import json
import time

import numpy as np
import pytest

from backend.actuators.audio import CandidatePool, Candidate, build_pool
from backend.actuators.display import NullBackend
from backend.daemon import Daemon
from backend.features import (CsvFeatureProvider, FeatureCache, FeatureChain, Features, FeaturesUnavailable,
                              ReccoBeatsProvider, SpotifyFeatureProvider)
from backend.feedback import FeedbackLog
from backend.inference import InferenceEngine, InferenceParams, MoodEstimator, TrackChanged, TrackStarted
from backend.mood_space import distance
from backend.spotify.client import MockSpotifyClient, Playback, RateLimited
from shared.config_io import ConfigError, load_config, set_paused, set_target_mood

from conftest import feats, make_track


# =========================================================== config.json

class TestConfigIO:
    def test_project_config_is_valid(self):
        cfg = load_config()
        assert set(cfg["moods"]) == {"focused", "calm", "energized", "wind_down"}

    def test_set_target_mood_writes_name_and_coordinates(self, tmp_config):
        before = json.loads(tmp_config.read_text(encoding="utf-8"))
        started = time.perf_counter()
        set_target_mood("wind_down", tmp_config)
        assert time.perf_counter() - started < 0.1
        after = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert after["target_mood"] == "wind_down"
        assert after["target"] == {"valence": 0.4, "energy": 0.15}
        # Nothing else changed.
        for k in before:
            if k not in ("target_mood", "target"):
                assert after[k] == before[k]

    def test_unknown_mood_rejected_and_file_untouched(self, tmp_config):
        before = tmp_config.read_text(encoding="utf-8")
        with pytest.raises(ConfigError):
            set_target_mood("ecstatic", tmp_config)
        assert tmp_config.read_text(encoding="utf-8") == before

    def test_pause_flag(self, tmp_config):
        assert set_paused(True, tmp_config)["paused"] is True
        assert load_config(tmp_config)["paused"] is True

    @pytest.mark.parametrize("mutate", [
        lambda c: c.update(target_mood="nope"),
        lambda c: c["moods"]["calm"].update(valence=1.5),
        lambda c: c.update(moods={}),
    ])
    def test_validation(self, tmp_config, mutate):
        cfg = json.loads(tmp_config.read_text(encoding="utf-8"))
        mutate(cfg)
        tmp_config.write_text(json.dumps(cfg), encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(tmp_config)

    def test_no_temp_files_left_behind(self, tmp_config):
        for mood in ("calm", "energized", "focused"):
            set_target_mood(mood, tmp_config)
        assert [p.name for p in tmp_config.parent.iterdir() if p.suffix == ".tmp"] == []


# =========================================================== feature providers

class FakeProvider:
    def __init__(self, name, data=None, exc=None):
        self.name, self.data, self.exc, self.calls = name, data or {}, exc, []

    def fetch(self, ids):
        self.calls.append(list(ids))
        if self.exc:
            raise self.exc
        return {i: self.data[i] for i in ids if i in self.data}


class TestFeatureChain:
    def test_falls_through_providers_and_caches(self, tmp_path):
        spotify = FakeProvider("spotify", exc=FeaturesUnavailable("403"))
        recco = FakeProvider("reccobeats", {"a": feats(0.1, 0.2)})
        csv_ = FakeProvider("csv", {"b": feats(0.3, 0.4), "a": feats(0.9, 0.9)})
        chain = FeatureChain([spotify, recco, csv_], FeatureCache(tmp_path / "c.db"))

        got = chain.get_many(["a", "b", "c"])
        assert got == {"a": feats(0.1, 0.2), "b": feats(0.3, 0.4)}
        assert csv_.calls == [["b", "c"]]  # only what was still missing
        assert "spotify" in chain.disabled

        # Second lookup: cache hits, disabled provider not retried.
        chain.get_many(["a", "b"])
        assert len(spotify.calls) == 1 and len(recco.calls) == 1

    def test_network_error_tries_next_provider(self):
        chain = FeatureChain([FakeProvider("reccobeats", exc=ConnectionError()),
                              FakeProvider("csv", {"a": feats(0.5, 0.5)})])
        assert chain.get("a") == feats(0.5, 0.5)
        assert chain.disabled == set()  # transient errors don't disable

    def test_csv_provider(self, tmp_path):
        path = tmp_path / "tracks.csv"
        path.write_text("track_id,track_name,artists,duration_ms,valence,energy,acousticness\n"
                        "abc,Song,Band,200000,0.7,0.6,0.1\n"
                        "bad,Broken,Band,1,x,0.1,0.1\n", encoding="utf-8")
        p = CsvFeatureProvider(path)
        assert p.fetch(["abc", "bad", "zzz"]) == {"abc": Features(0.7, 0.6, 0.1)}
        assert p.rows()["abc"].duration_ms == 200000

    def test_reccobeats_maps_href_to_spotify_id(self):
        class Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"content": [{"id": "uuid-1", "href": "https://open.spotify.com/track/sp1",
                                     "valence": 0.9, "energy": 0.6, "acousticness": 0.5}]}

        class Session:
            def get(self, url, params, timeout):
                assert params == {"ids": "sp1,sp2"}
                return Resp()

        assert ReccoBeatsProvider(Session()).fetch(["sp1", "sp2"]) == {"sp1": Features(0.9, 0.6, 0.5)}

    def test_spotify_provider_uses_client(self, mock_client, catalog):
        tid = next(iter(catalog))
        assert SpotifyFeatureProvider(mock_client).fetch([tid]) == {tid: catalog[tid][1]}
        mock_client.features_available = False
        with pytest.raises(FeaturesUnavailable):
            SpotifyFeatureProvider(mock_client).fetch([tid])


# =========================================================== inference

class TestMoodEstimator:
    def test_first_track_sets_mood_then_ema(self):
        est = MoodEstimator(alpha=0.5)
        assert est.push(feats(0.2, 0.2)) == (0.2, 0.2)
        assert est.push(feats(0.6, 1.0)) == pytest.approx((0.4, 0.6))

    def test_skip_reduces_contribution(self):
        est = MoodEstimator(alpha=0.5, skip_weight=0.2)
        est.push(feats(0.0, 0.0))
        est.push(feats(1.0, 1.0))
        assert est.mark_last_skipped() == pytest.approx((0.1, 0.1))


def pb(track, progress_s, at, playing=True):
    return Playback(track, int(progress_s * 1000), playing, at)


class TestSkipDetection:
    def engine(self, clock):
        return InferenceEngine(None, FeatureChain([]), clock, InferenceParams(), np.random.default_rng(0))

    def test_completed_track(self, clock):
        eng = self.engine(clock)
        a, b = make_track("a", 200), make_track("b", 180)
        eng.observe(pb(a, 150, 1000), feats(0.5, 0.5))
        # 55 s later: b has been playing for 5 s, so a stopped at 150 + 50 = 200 s.
        events = eng.observe(pb(b, 5, 1055), feats(0.6, 0.6))
        changed = [e for e in events if isinstance(e, TrackChanged)][0]
        assert not changed.skipped and changed.listened_ms == 200_000
        assert isinstance(events[-1], TrackStarted)

    def test_skipped_track(self, clock):
        eng = self.engine(clock)
        a, b = make_track("a", 200), make_track("b", 180)
        eng.observe(pb(a, 30, 1000), feats(0.5, 0.5))
        events = eng.observe(pb(b, 40, 1045), feats(0.9, 0.9))
        changed = events[0]
        assert changed.skipped and changed.listened_ms == 35_000

    def test_skip_lowers_weight_of_skipped_track(self, clock):
        eng = self.engine(clock)
        eng.observe(pb(make_track("a"), 0, 0), feats(0.2, 0.2))
        eng.observe(pb(make_track("b"), 0, 50), feats(1.0, 1.0))
        full = eng.mood
        eng.observe(pb(make_track("c"), 1, 60), feats(0.2, 0.2))  # b skipped after ~9 s
        # b's influence was reduced before c was folded in.
        assert eng.estimator._before_last[0] < full[0]

    def test_nothing_playing_and_same_track(self, clock):
        eng = self.engine(clock)
        assert eng.observe(None, None) == []
        a = make_track("a")
        assert len(eng.observe(pb(a, 0, 0), feats(0.5, 0.5))) == 1
        assert eng.observe(pb(a, 30, 30), None) == []

    def test_poll_delay_stays_in_range_and_targets_track_end(self, clock):
        eng = self.engine(clock)
        a = make_track("a", 200)
        for progress in (0, 100, 150, 170, 190, 199):
            d = eng.next_delay(pb(a, progress, 0))
            assert 30 <= d <= 60
        assert eng.next_delay(None) == 60

    def test_rate_limit_backs_off(self, clock):
        class Limited:
            def current_playback(self):
                raise RateLimited(120)

        eng = InferenceEngine(Limited(), FeatureChain([]), clock)
        result = asyncio.run(eng.poll())
        assert result.error == "rate_limited" and result.delay_s == 120
        result = asyncio.run(eng.poll())
        assert result.delay_s >= 120


# =========================================================== telemetry

class TestFeedback:
    def test_event_row_on_song_change(self):
        fb = FeedbackLog(":memory:")
        fb.log_event(1234.5, (0.4, 0.7), "audio_bridge", True, "t1", (-1, 0))
        fb.log_event(1300.0, (0.42, 0.66), "none", False, "t2")
        rows = fb.events()
        assert rows[0] == (1234.5, 0.4, 0.7, "audio_bridge", 1, "t1", "-1,0")
        assert rows[1][3:5] == ("none", 0)
        assert fb.skip_stats() == {(-1, 0): (0, 1)}
        assert fb.skip_rate_by_intervention() == {"audio_bridge": (1, 1.0), "none": (1, 0.0)}

    def test_csv_export(self, tmp_path):
        fb = FeedbackLog(":memory:")
        fb.log_event(1.0, (0.1, 0.2), "none", False, "t")
        fb.export_csv(tmp_path / "events.csv")
        lines = (tmp_path / "events.csv").read_text(encoding="utf-8").splitlines()
        assert lines[0].startswith("ts,valence,energy,intervention_type,skipped")
        assert len(lines) == 2


# =========================================================== daemon

def make_daemon(tmp_config, client, clock, catalog):
    cfg = load_config(tmp_config)
    chain = FeatureChain([SpotifyFeatureProvider(client)])
    pool = build_pool(client, chain, None)
    return Daemon(tmp_config, cfg, client, chain, FeedbackLog(":memory:"), pool, clock,
                  display_backend=NullBackend(), state_path=tmp_config.parent / "state.json",
                  rng=np.random.default_rng(0))


class TestDaemonSession:
    def test_ten_minute_mocked_session(self, tmp_config, catalog, clock):
        # Start far from "focused" (loud, happy tracks), with API faults sprinkled in.
        loud = sorted(catalog, key=lambda t: -(catalog[t][1].energy + catalog[t][1].valence))[:3]
        client = MockSpotifyClient(catalog, clock, script=[(t, 1.0) for t in loud] + [(loud[0], 0.2)],
                                   seed=2, rate_limit_every=7, error_every=11, gap_after=4, gap_s=70)
        daemon = make_daemon(tmp_config, client, clock, catalog)

        async def session():
            await daemon.apply_target("startup")
            await daemon.run_for(600)

        asyncio.run(session())

        assert clock.now() - 1_700_000_000.0 >= 600
        assert client.calls >= 10  # polled throughout, despite 429s and errors
        events = daemon.feedback.events()
        assert len(events) >= 2
        for ts, v, e, itype, skipped, track_id, _ in events:  # [timestamp, mood, intervention, skipped]
            assert isinstance(ts, float) and 0 <= v <= 1 and 0 <= e <= 1
            assert itype in ("none", "audio_bridge") and skipped in (0, 1)
        # The actuator pushed bridging tracks into the (mock) queue...
        assert client.queued_log
        # ...and the display ramp moved toward the target temperature.
        assert daemon.display.current < 6500
        state = json.loads((tmp_config.parent / "state.json").read_text(encoding="utf-8"))
        assert state["target_mood"] == "focused" and state["mood"] is not None

    def test_mood_converges_toward_target(self, tmp_config, catalog, clock):
        loud = sorted(catalog, key=lambda t: -(catalog[t][1].energy + catalog[t][1].valence))[:3]
        client = MockSpotifyClient(catalog, clock, script=[(t, 1.0) for t in loud], seed=5, queue_skip_p=0.0)
        daemon = make_daemon(tmp_config, client, clock, catalog)
        start_gap = []

        async def session():
            await daemon.apply_target("startup")
            await daemon.run_for(3 * 230)  # past the scripted tracks
            start_gap.append(distance(daemon.inference.mood, daemon.target))
            await daemon.run_for(90 * 60)

        asyncio.run(session())
        end_gap = distance(daemon.inference.mood, daemon.target)
        assert start_gap[0] > 0.3
        assert end_gap < 0.2 + 0.05

    def test_injected_tracks_logged_as_interventions(self, tmp_config, catalog, clock):
        client = MockSpotifyClient(catalog, clock, script=[("demo0143", 1.0)], seed=3, queue_skip_p=1.0)
        daemon = make_daemon(tmp_config, client, clock, catalog)

        async def session():
            await daemon.apply_target("startup")
            await daemon.run_for(30 * 60)

        asyncio.run(session())
        bridged = [r for r in daemon.feedback.events() if r[3] == "audio_bridge"]
        assert bridged and all(r[4] == 1 for r in bridged)  # every bridge was skipped
        assert sum(s for _, s in daemon.feedback.skip_stats().values()) == len(bridged)

    def test_config_change_detected_by_watcher(self, tmp_config, catalog, clock):
        client = MockSpotifyClient(catalog, clock, seed=4)
        daemon = make_daemon(tmp_config, client, clock, catalog)

        async def scenario():
            daemon.start_watcher()
            try:
                await asyncio.sleep(0.2)
                await asyncio.to_thread(set_target_mood, "wind_down", tmp_config)
                for _ in range(100):
                    # The display target is set last in apply_target: wait for it, not just the name.
                    if daemon.target_mood == "wind_down" and daemon.display.target == 3500:
                        break
                    await asyncio.sleep(0.02)
            finally:
                daemon.shutdown()

        asyncio.run(scenario())
        assert daemon.target_mood == "wind_down"
        assert daemon.target == (0.4, 0.15)
        assert daemon.display.target == 3500

"""Markov chain math, audio bridging, JSONC workspace edits, display temperature."""
import asyncio
import json
import math
import time

import numpy as np
import pytest

from backend.actuators.audio import AudioActuator, AudioParams, Candidate, CandidatePool
from backend.actuators.display import DisplayActuator, DisplayParams, NullBackend, build_ramp, kelvin_to_rgb
from backend.actuators.workspace import (JsoncError, JsoncPathError, WorkspaceActuator, parse_jsonc,
                                         set_jsonc_key, update_jsonc_file)
from backend.feedback import FeedbackLog
from backend.mood_space import (DIRECTIONS, MarkovParams, MoodChain, build_transition_matrix, cell_center,
                                cell_index, distance, index_cell, point_to_cell)
from shared.config_io import load_config

from conftest import feats

N = 10
PARAMS = MarkovParams(grid_size=N)
FOCUSED = (0.5, 0.45)


# =========================================================== Markov chain

class TestGrid:
    @pytest.mark.parametrize("point,cell", [((0.0, 0.0), (0, 0)), ((1.0, 1.0), (9, 9)),
                                            ((0.55, 0.05), (5, 0)), ((-0.2, 1.4), (0, 9))])
    def test_point_to_cell_clamps_edges(self, point, cell):
        assert point_to_cell(point, N) == cell

    def test_cell_center_round_trip(self):
        for i in range(N * N):
            c = index_cell(i, N)
            assert cell_index(c, N) == i
            assert point_to_cell(cell_center(c, N), N) == c


class TestTransitionMatrix:
    @pytest.fixture
    def P(self):
        return build_transition_matrix(FOCUSED, PARAMS)

    def test_rows_are_probability_distributions(self, P):
        assert P.shape == (N * N, N * N)
        assert np.all(P >= 0)
        np.testing.assert_allclose(P.sum(axis=1), 1.0)

    def test_only_adjacent_moves(self, P):
        for i, j in zip(*np.nonzero(P)):
            ci, cj = index_cell(i, N), index_cell(j, N)
            assert max(abs(ci[0] - cj[0]), abs(ci[1] - cj[1])) <= 1

    def test_every_move_gets_closer_to_target(self, P):
        t = point_to_cell(FOCUSED, N)
        for i, j in zip(*np.nonzero(P)):
            ci, cj = index_cell(i, N), index_cell(j, N)
            if ci == t:
                continue
            dj = 0.0 if cj == t else distance(cell_center(cj, N), FOCUSED)
            assert dj < distance(cell_center(ci, N), FOCUSED)

    def test_target_cell_is_absorbing(self, P):
        t = cell_index(point_to_cell(FOCUSED, N), N)
        assert P[t, t] == 1.0

    def test_prefers_the_direct_route(self, P):
        # From the top-right corner the diagonal step toward the target dominates.
        i = cell_index((9, 9), N)
        best = index_cell(int(np.argmax(P[i])), N)
        assert best == (8, 8)

    def test_probability_mass_drifts_toward_target(self, P):
        dist = np.zeros(N * N)
        dist[cell_index((9, 9), N)] = 1.0
        centers = np.array([distance(cell_center(index_cell(i, N), N), FOCUSED) for i in range(N * N)])
        expected = [float(dist @ centers)]
        for _ in range(12):
            dist = dist @ P
            expected.append(float(dist @ centers))
        assert all(b < a or math.isclose(a, b, abs_tol=1e-12) for a, b in zip(expected, expected[1:]))
        assert dist[cell_index(point_to_cell(FOCUSED, N), N)] > 0.99

    def test_beta_strengthens_pull(self):
        i = cell_index((9, 9), N)
        weak = build_transition_matrix(FOCUSED, MarkovParams(N, beta=0.5))
        strong = build_transition_matrix(FOCUSED, MarkovParams(N, beta=12.0))
        diag = cell_index((8, 8), N)
        assert strong[i, diag] > weak[i, diag]

    def test_skip_stats_reduce_probability_of_that_direction(self):
        i = cell_index((9, 9), N)
        diag = cell_index((8, 8), N)
        base = build_transition_matrix(FOCUSED, PARAMS)
        skipped = build_transition_matrix(FOCUSED, PARAMS, {(-1, -1): (0, 10)})
        favoured = build_transition_matrix(FOCUSED, PARAMS, {(-1, -1): (10, 0)})
        assert skipped[i, diag] < base[i, diag] < favoured[i, diag]
        np.testing.assert_allclose(skipped.sum(axis=1), 1.0)


class TestMoodChain:
    def test_expected_steps(self):
        chain = MoodChain(FOCUSED, PARAMS)
        steps = chain.expected_steps_to_target()
        t = cell_index(chain.target_cell, N)
        assert steps[t] == 0
        assert np.all(np.isfinite(steps))
        # Corner (9, 9) is 4 cells away diagonally from (5, 4): needs at least 5 steps (Chebyshev).
        corner = chain.expected_steps_from((0.95, 0.95))
        assert 5 <= corner <= 10

    def test_sampled_walk_reaches_target(self):
        chain = MoodChain(FOCUSED, PARAMS)
        rng = np.random.default_rng(0)
        for start in [(0.95, 0.95), (0.05, 0.05), (0.95, 0.05), (0.05, 0.95)]:
            point = start
            for step in range(2 * N):
                point, direction = chain.next_waypoint(point, rng)
                assert direction in DIRECTIONS or direction == (0, 0)
                if point == chain.target:
                    break
            assert point == chain.target, f"walk from {start} did not converge"

    def test_waypoint_moves_one_step(self):
        chain = MoodChain(FOCUSED, PARAMS)
        rng = np.random.default_rng(3)
        for _ in range(50):
            wp, d = chain.next_waypoint((0.85, 0.85), rng)
            assert max(abs(d[0]), abs(d[1])) == 1
            assert distance(wp, FOCUSED) < distance((0.85, 0.85), FOCUSED)


# =========================================================== audio actuator

def grid_pool() -> CandidatePool:
    """One candidate at every grid-cell centre."""
    cands = []
    for i in range(N):
        for j in range(N):
            v, e = (i + 0.5) / N, (j + 0.5) / N
            cands.append(Candidate(f"t{i}{j}", f"Song {i}{j}", "A", feats(v, e, 1 - e), "library"))
    return CandidatePool(cands)


class RecordingClient:
    def __init__(self):
        self.queued = []

    def add_to_queue(self, uri):
        self.queued.append(uri)


def make_actuator(clock, client=None, feedback=None, **params):
    return AudioActuator(client or RecordingClient(), grid_pool(), feedback, clock,
                         AudioParams(**params), PARAMS, np.random.default_rng(0))


class TestAudioActuator:
    def test_bridge_queued_when_deviation_exceeds_threshold(self, clock):
        act = make_actuator(clock)
        mood = (0.85, 0.85)
        inj = asyncio.run(act.maybe_inject(mood, FOCUSED))
        assert inj is not None
        assert act.client.queued == [inj.candidate.uri]
        p = inj.candidate.features.point
        # Bridging track lies between current and target: closer to the target
        # than we are, and closer to us than the target is.
        assert distance(p, FOCUSED) < distance(mood, FOCUSED)
        assert distance(mood, p) < distance(mood, FOCUSED)
        # It is the track nearest the Markov waypoint, one grid step away.
        assert distance(p, inj.waypoint) < 1e-9
        assert max(abs(inj.direction[0]), abs(inj.direction[1])) == 1

    def test_no_bridge_within_threshold(self, clock):
        act = make_actuator(clock)
        mood = (FOCUSED[0] + 0.12, FOCUSED[1] + 0.12)  # 0.17 away
        assert asyncio.run(act.maybe_inject(mood, FOCUSED)) is None
        assert act.client.queued == []

    def test_no_bridge_without_mood_or_when_disabled(self, clock):
        assert asyncio.run(make_actuator(clock).maybe_inject(None, FOCUSED)) is None
        assert asyncio.run(make_actuator(clock, enabled=False).maybe_inject((0.9, 0.9), FOCUSED)) is None

    def test_pacing_one_in_flight_and_cooldown(self, clock):
        act = make_actuator(clock, cooldown_s=90)
        first = asyncio.run(act.maybe_inject((0.9, 0.9), FOCUSED))
        assert first
        # Not played yet -> nothing else is queued.
        assert asyncio.run(act.maybe_inject((0.9, 0.9), FOCUSED)) is None
        act.note_started(first.candidate.track_id)
        clock.advance(30)
        assert asyncio.run(act.maybe_inject((0.9, 0.9), FOCUSED)) is None  # cooldown
        # A new target bypasses the cooldown.
        assert asyncio.run(act.maybe_inject((0.9, 0.9), (0.4, 0.15), reason="target_change"))

    def test_take_intervention_marks_bridged_tracks(self, clock):
        act = make_actuator(clock)
        inj = asyncio.run(act.maybe_inject((0.9, 0.9), FOCUSED))
        assert act.take_intervention("someone-else") == ("none", None)
        assert act.take_intervention(inj.candidate.track_id) == ("audio_bridge", inj.direction)

    def test_recently_played_tracks_are_not_requeued(self, clock):
        act = make_actuator(clock)
        planned = act.plan((0.85, 0.85), FOCUSED)[0]
        act.note_started(planned.track_id)
        for _ in range(20):
            assert act.plan((0.85, 0.85), FOCUSED)[0].track_id != planned.track_id

    def test_skip_feedback_changes_chain(self, clock):
        fb = FeedbackLog(":memory:")
        for _ in range(10):
            fb.log_event(0, (0.9, 0.9), "audio_bridge", True, "x", (-1, -1))
        act = make_actuator(clock, feedback=fb)
        skipped = act.chain_for(FOCUSED).transition_probs((0.95, 0.95))[(8, 8)]
        fresh = MoodChain(FOCUSED, PARAMS).transition_probs((0.95, 0.95))[(8, 8)]
        assert skipped < fresh

    def test_add_to_queue_failure_is_not_fatal(self, clock):
        class Broken:
            def add_to_queue(self, uri):
                raise ConnectionError("down")

        act = make_actuator(clock, client=Broken())
        assert asyncio.run(act.maybe_inject((0.9, 0.9), FOCUSED)) is None
        assert act.pending == {}


# =========================================================== workspace (JSONC)

VSCODE_SETTINGS = """{
    // Editor look & feel
    "editor.fontSize": 14,
    "workbench.colorTheme": "Default Dark Modern", // set by hand
    "files.associations": {
        "*.jsonc": "jsonc",
    },
    /* proxy, keep the // inside the string */
    "http.proxy": "http://user:pw@proxy//x",
    "editor.rulers": [80, 120,],
    "escaped": "quote \\" and // not a comment",
}
"""


class TestJsonc:
    def test_parse_handles_comments_and_trailing_commas(self):
        data = parse_jsonc(VSCODE_SETTINGS)
        assert data["workbench.colorTheme"] == "Default Dark Modern"
        assert data["http.proxy"] == "http://user:pw@proxy//x"
        assert data["editor.rulers"] == [80, 120]
        assert data["escaped"] == 'quote " and // not a comment'

    def test_replace_changes_only_the_value(self):
        new = set_jsonc_key(VSCODE_SETTINGS, ["workbench.colorTheme"], "Solarized Dark")
        assert new == VSCODE_SETTINGS.replace('"Default Dark Modern"', '"Solarized Dark"')

    def test_insert_missing_key_keeps_comments(self):
        src = '{\n    "a": 1, // first\n    "b": [1, 2] // last\n}\n'
        new = set_jsonc_key(src, ["workbench.colorTheme"], "Monokai")
        assert "// first" in new and "// last" in new
        assert parse_jsonc(new) == {"a": 1, "b": [1, 2], "workbench.colorTheme": "Monokai"}
        assert '    "workbench.colorTheme": "Monokai"' in new

    def test_insert_into_empty_object_and_nested_path(self):
        assert parse_jsonc(set_jsonc_key("{}", ["x"], 1)) == {"x": 1}
        src = '{\n  "profiles": {\n    "list": []\n  }\n}'
        new = set_jsonc_key(src, ["profiles", "defaults", "colorScheme"], "Campbell")
        assert parse_jsonc(new) == {"profiles": {"list": [], "defaults": {"colorScheme": "Campbell"}}}

    def test_path_through_non_object_is_rejected(self):
        with pytest.raises(JsoncPathError):
            set_jsonc_key('{"profiles": []}', ["profiles", "defaults", "colorScheme"], "x")

    @pytest.mark.parametrize("bad", ['{"a": 1', '{"a" 1}', '{"a": 1} x', '{"a": "unterminated}', '/* open'])
    def test_invalid_documents_raise(self, bad):
        with pytest.raises(JsoncError):
            parse_jsonc(bad)


class TestWorkspaceFiles:
    def test_mock_vscode_settings_updated_without_corruption(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(VSCODE_SETTINGS, encoding="utf-8")
        assert update_jsonc_file(path, ["workbench.colorTheme"], "Solarized Dark", backup=True)
        text = path.read_text(encoding="utf-8")
        assert text == VSCODE_SETTINGS.replace('"Default Dark Modern"', '"Solarized Dark"')
        before, after = parse_jsonc(VSCODE_SETTINGS), parse_jsonc(text)
        assert after.pop("workbench.colorTheme") == "Solarized Dark"
        before.pop("workbench.colorTheme")
        assert after == before
        assert (tmp_path / "settings.json.bak").read_text(encoding="utf-8") == VSCODE_SETTINGS

    def test_crlf_and_bom_preserved(self, tmp_path):
        path = tmp_path / "settings.json"
        src = VSCODE_SETTINGS.replace("\n", "\r\n")
        path.write_bytes(b"\xef\xbb\xbf" + src.encode())
        update_jsonc_file(path, ["new.key"], True)
        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig")
        assert "\n" not in text.replace("\r\n", "")
        assert parse_jsonc(text)["new.key"] is True

    def test_same_value_is_a_noop(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(VSCODE_SETTINGS, encoding="utf-8")
        assert not update_jsonc_file(path, ["workbench.colorTheme"], "Default Dark Modern")
        assert not (tmp_path / "settings.json.bak").exists()

    def test_broken_file_left_untouched(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text('{"workbench.colorTheme": "x",, }', encoding="utf-8")
        with pytest.raises(JsoncError):
            update_jsonc_file(path, ["workbench.colorTheme"], "y")
        assert path.read_text(encoding="utf-8") == '{"workbench.colorTheme": "x",, }'

    def test_actuator_applies_vscode_and_terminal_fast(self, tmp_config):
        cfg = load_config(tmp_config)
        vs = tmp_config.parent / "vscode_settings.json"
        wt = tmp_config.parent / "terminal_settings.json"
        vs.write_text(VSCODE_SETTINGS, encoding="utf-8")
        wt.write_text('{\n    // Windows Terminal\n    "profiles": {\n        "defaults": {},\n'
                      '        "list": [{"name": "PowerShell"}]\n    },\n    "schemes": []\n}\n', encoding="utf-8")
        act = WorkspaceActuator(cfg)
        started = time.perf_counter()
        changed = act.apply(cfg["moods"]["wind_down"])
        assert (time.perf_counter() - started) < 0.5
        assert changed == ["vscode=Solarized Dark", "terminal=Solarized Dark"]
        assert parse_jsonc(vs.read_text(encoding="utf-8"))["workbench.colorTheme"] == "Solarized Dark"
        wt_data = parse_jsonc(wt.read_text(encoding="utf-8"))
        assert wt_data["profiles"]["defaults"]["colorScheme"] == "Solarized Dark"
        assert "// Windows Terminal" in wt.read_text(encoding="utf-8")
        # Switching again keeps working and is idempotent.
        assert act.apply(cfg["moods"]["calm"]) == ["vscode=Quiet Light", "terminal=One Half Light"]
        assert act.apply(cfg["moods"]["calm"]) == []

    def test_missing_files_are_skipped(self, tmp_config):
        cfg = load_config(tmp_config)
        assert WorkspaceActuator(cfg).apply(cfg["moods"]["calm"]) == []


# =========================================================== display

class TestDisplay:
    def test_6500k_is_neutral(self):
        assert kelvin_to_rgb(6500) == pytest.approx((1.0, 1.0, 1.0))
        ramp = build_ramp(6500)
        assert ramp[0][255] == ramp[2][255] == 65535 and ramp[1][128] == 128 * 257

    def test_warmer_means_less_blue(self):
        blues = [kelvin_to_rgb(k)[2] for k in (6500, 5500, 4500, 3500)]
        assert blues == sorted(blues, reverse=True)
        r, g, b = kelvin_to_rgb(3500)
        assert r == pytest.approx(1.0) and r > g > b > 0

    def test_ramps_gradually_to_target(self):
        backend = NullBackend()
        act = DisplayActuator(backend, DisplayParams(step_kelvin=100, min_kelvin=3400))
        act.set_target(3500)
        steps = []
        while (k := act.step()) is not None:
            steps.append(k)
        assert steps[0] == 6400 and steps[-1] == 3500 and len(steps) == 30
        assert all(a - b == 100 for a, b in zip(steps, steps[1:]))

    def test_target_clamped_to_range(self):
        act = DisplayActuator(NullBackend(), DisplayParams(min_kelvin=3400, max_kelvin=6500))
        act.set_target(1000)
        assert act.target == 3400
        act.set_target(9000)
        assert act.target == 6500

    def test_refused_ramp_raises_floor(self):
        class Picky(NullBackend):
            def apply(self, kelvin):
                return kelvin >= 5000

        act = DisplayActuator(Picky(), DisplayParams(step_kelvin=500, min_kelvin=3000))
        act.set_target(3000)
        results = [act.step() for _ in range(6)]
        assert act.current == 5000 and act.floor == 5000
        assert results[-1] is None

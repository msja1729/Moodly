"""Phase 2 selection: queue mood, musical distances, ranking, MMR, insertion positions, removals."""
import numpy as np
import pytest

from backend.catalog import CatalogTrack, TrackTable
from backend.features import Features
from backend.mood_space import MarkovParams, MoodChain, distance
from backend.selection import (MusicalContext, SelectionParams, WindowItem, camelot, choose_removals,
                               key_distance, mmr_select, plan_cycle, plan_insertions, queue_mood,
                               rank_candidates, tempo_distance, transition_cost)

TARGET = (0.5, 0.45)
P = SelectionParams()


def feats(v, e, tempo=120.0, key=0, mode=1, loud=-8.0, dance=0.6, ac=0.3):
    return Features(v, e, ac, tempo, key, mode, loud, dance)


def item(v, e, uid, removable=True, **kw):
    return WindowItem(f"spotify:track:{uid}", f"Song {uid}", f"Artist {uid}", feats(v, e, **kw), removable=removable)


def grid_table(n=30, artists=20):
    tracks = []
    k = 0
    for i in range(n):
        for j in range(n):
            v, e = (i + 0.5) / n, (j + 0.5) / n
            tracks.append(CatalogTrack(f"g{k}", f"Grid {k}", f"Artist {k % artists}",
                                       feats(v, e, tempo=80 + 80 * e, key=k % 12, mode=k % 2), 200_000))
            k += 1
    return TrackTable(tracks)


# --------------------------------------------------------------------------- mood

class TestQueueMood:
    def test_plain_average_of_the_window(self):
        items = [item(0.2, 0.4, "a"), item(0.6, 0.8, "b"), item(1.0, 0.0, "c")]
        assert queue_mood(items) == pytest.approx((0.6, 0.4))

    def test_podcasts_and_unknown_songs_are_left_out(self):
        items = [item(0.2, 0.2, "a"),
                 WindowItem("spotify:episode:x", kind="episode", removable=False),
                 WindowItem("spotify:track:nofeatures"),
                 item(0.4, 0.6, "b")]
        assert queue_mood(items) == pytest.approx((0.3, 0.4))

    def test_window_size_limits_the_average(self):
        items = [item(0.0, 0.0, "a"), item(1.0, 1.0, "b"), item(1.0, 1.0, "c")]
        assert queue_mood(items, window_size=1) == (0.0, 0.0)

    def test_nothing_known(self):
        assert queue_mood([WindowItem("spotify:episode:x", kind="episode")]) is None


# --------------------------------------------------------------------------- distances

class TestMusicalDistances:
    def test_tempo_half_and_double_time_match(self):
        d = tempo_distance([120, 60, 240, 90, 180], 120)
        assert d[0] == 0 and d[1] == pytest.approx(0) and d[2] == pytest.approx(0)
        assert 0 < d[3] < 1 and 0 < d[4] < 1

    def test_tempo_unknown_is_neutral(self):
        assert tempo_distance([np.nan], 120)[0] == 0.5
        assert tempo_distance([100], None)[0] == 0.5

    @pytest.mark.parametrize("key, mode, code", [(0, 1, (8, 1)), (9, 0, (8, 0)), (7, 1, (9, 1)), (4, 1, (12, 1)),
                                                 (11, 0, (10, 0)), (2, 0, (7, 0))])
    def test_camelot_codes(self, key, mode, code):
        num, letter = camelot([key], [mode])
        assert (int(num[0]), int(letter[0])) == code

    def test_key_distance_on_the_wheel(self):
        # C major (8B) vs: itself, A minor (8A, relative), G major (9B, neighbour), F# major (2B, opposite)
        d = key_distance([0, 9, 7, 6], [1, 0, 1, 1], 0, 1)
        assert d[0] == 0
        assert d[1] == pytest.approx(1 / 7) and d[2] == pytest.approx(1 / 7)
        assert d[3] == pytest.approx(6 / 7)
        assert key_distance([-1], [1], 0, 1)[0] == 0.5

    def test_transition_cost_prefers_smooth_changes(self):
        a = feats(0.5, 0.5, tempo=120, key=0, mode=1, loud=-8)
        smooth = feats(0.5, 0.52, tempo=122, key=7, mode=1, loud=-8.5)
        jarring = feats(0.5, 0.95, tempo=172, key=6, mode=1, loud=-2)
        assert transition_cost(a, smooth, P) < transition_cost(a, jarring, P)


# --------------------------------------------------------------------------- ranking and variety

class TestRanking:
    def test_candidates_are_near_the_waypoint_and_closer_to_the_target(self):
        table = grid_table()
        mood, waypoint = (0.85, 0.85), (0.75, 0.75)
        ctx = MusicalContext(140, 0, 1, -6, 0.6, 0.3)
        idx, scores = rank_candidates(table, waypoint, mood, TARGET, ctx, set(), P)
        assert len(idx) > 0 and np.all(np.diff(scores) >= 0)
        for i in idx[:10]:
            p = tuple(table.va[i])
            assert distance(p, TARGET) < distance(mood, TARGET)
            assert distance(p, waypoint) <= 0.2

    def test_musical_fit_breaks_ties(self):
        same_spot = [CatalogTrack("fit", "Fit", "X", feats(0.6, 0.6, tempo=120, key=0, mode=1, loud=-8)),
                     CatalogTrack("misfit", "Misfit", "Y", feats(0.6, 0.6, tempo=171, key=6, mode=1, loud=-1))]
        table = TrackTable(same_spot)
        ctx = MusicalContext(120, 0, 1, -8, 0.6, 0.3)
        idx, _ = rank_candidates(table, (0.6, 0.6), (0.9, 0.9), TARGET, ctx, set(), P)
        assert [table.tracks[i].id for i in idx] == ["fit", "misfit"]

    def test_excluded_and_explicit_tracks(self):
        tracks = [CatalogTrack("a", "A", "X", feats(0.6, 0.6)), CatalogTrack("b", "B", "Y", feats(0.6, 0.6), explicit=True)]
        table = TrackTable(tracks)
        ctx = MusicalContext.of([])
        idx, _ = rank_candidates(table, (0.6, 0.6), (0.9, 0.9), TARGET, ctx, {"a"}, P)
        assert [table.tracks[i].id for i in idx] == ["b"]
        no_explicit = SelectionParams(allow_explicit=False)
        idx, _ = rank_candidates(table, (0.6, 0.6), (0.9, 0.9), TARGET, ctx, {"a"}, no_explicit)
        assert len(idx) == 0

    def test_library_songs_get_a_small_bonus(self):
        tracks = [CatalogTrack("d", "D", "X", feats(0.6, 0.6)), CatalogTrack("l", "L", "Y", feats(0.6, 0.6), source="library")]
        table = TrackTable(tracks)
        idx, _ = rank_candidates(table, (0.6, 0.6), (0.9, 0.9), TARGET, MusicalContext.of([]), set(), P)
        assert table.tracks[idx[0]].id == "l"

    def test_mmr_avoids_repeating_an_artist(self):
        tracks = [CatalogTrack(f"s{i}", f"S{i}", "Same Artist", feats(0.6, 0.6 + i * 0.001)) for i in range(5)]
        tracks += [CatalogTrack("o", "O", "Other Artist", feats(0.62, 0.6))]
        table = TrackTable(tracks)
        idx = np.arange(len(tracks))
        scores = np.array([0.0, 0.001, 0.002, 0.003, 0.004, 0.02])
        picks = mmr_select(table, idx, scores, 2)
        assert {p.artists for p in picks} == {"Same Artist", "Other Artist"}

    def test_mmr_respects_artists_to_avoid(self):
        tracks = [CatalogTrack("a", "A", "Near Artist", feats(0.6, 0.6)), CatalogTrack("b", "B", "Fresh", feats(0.6, 0.6))]
        table = TrackTable(tracks)
        picks = mmr_select(table, np.arange(2), np.array([0.0, 0.05]), 1, avoid_artists={"near artist"})
        assert picks[0].id == "b"


# --------------------------------------------------------------------------- positions and removals

class TestInsertions:
    def test_never_before_position_two(self):
        items = [item(0.9, 0.9, "now"), item(0.9, 0.9, "next")]
        pick = CatalogTrack("p", "P", "Z", feats(0.8, 0.8))
        [(pos, _)] = plan_insertions(items, [pick], (0.9, 0.9), TARGET, P)
        assert pos == 2

    def test_songs_play_in_mood_to_target_order(self):
        items = [item(0.9, 0.9, "now"), item(0.9, 0.9, "next"), item(0.88, 0.88, "a"), item(0.6, 0.55, "b")]
        near_mood = CatalogTrack("m", "M", "Z1", feats(0.8, 0.8))
        near_target = CatalogTrack("t", "T", "Z2", feats(0.55, 0.5))
        plan = plan_insertions(items, [near_target, near_mood], (0.9, 0.9), TARGET, P)
        seq = [it.uri for it in items]
        for pos, track in plan:
            seq.insert(pos, track.uri)
        # The near-mood pick goes after "a" (less far along) and before "b"; the near-target pick goes last.
        assert seq == ["spotify:track:now", "spotify:track:next", "spotify:track:a", "spotify:track:m",
                       "spotify:track:b", "spotify:track:t"]

    def test_cheapest_gap_among_equals(self):
        # Same point on the path everywhere; only the music differs. The fast pick fits right after the
        # fast "next" song, not between (or after) the two slow, quiet songs in another key.
        items = [item(0.9, 0.9, "now"), item(0.7, 0.7, "next", tempo=150, key=0, loud=-6),
                 item(0.7, 0.7, "slow1", tempo=70, key=6, loud=-20),
                 item(0.7, 0.7, "slow2", tempo=70, key=6, loud=-20)]
        pick = CatalogTrack("p", "P", "Z", feats(0.7, 0.7, tempo=150, key=0, loud=-6))
        [(pos, _)] = plan_insertions(items, [pick], (0.9, 0.9), TARGET, P)
        assert pos == 2

    def test_chosen_gap_has_the_lowest_insertion_cost(self):
        rng = np.random.default_rng(3)
        items = [item(0.9, 0.9, "now")] + [
            item(0.7, 0.7, f"s{i}", tempo=float(rng.uniform(70, 170)), key=int(rng.integers(12)),
                 loud=float(rng.uniform(-20, -4))) for i in range(8)]
        pick = CatalogTrack("p", "P", "Z", feats(0.7, 0.7, tempo=128, key=2, loud=-7))
        [(pos, _)] = plan_insertions(items, [pick], (0.9, 0.9), TARGET, P)

        def cost(at):
            a = items[at - 1].features
            b = items[at].features if at < len(items) else None
            c = transition_cost(a, pick.features, P)
            return c + (transition_cost(pick.features, b, P) - transition_cost(a, b, P) if b else 0)

        assert cost(pos) == pytest.approx(min(cost(at) for at in range(2, len(items) + 1)))


class TestRemovals:
    def test_only_songs_far_from_both_mood_and_target(self):
        mood = (0.8, 0.8)
        items = ([item(0.8, 0.8, "now", removable=False), item(0.8, 0.8, "next")]
                 + [item(0.8, 0.8, f"keep{i}") for i in range(8)]
                 + [item(0.05, 0.05, "far"),            # far from both -> removed
                    item(0.5, 0.45, "attarget"),        # at the target -> kept
                    item(0.85, 0.85, "atmood")])        # at the mood -> kept
        assert [items[i].track_id for i in choose_removals(items, mood, TARGET, P)] == ["far"]

    def test_protected_and_non_removable_items_are_kept(self):
        mood = (0.8, 0.8)
        items = [item(0.0, 0.0, "now"), item(0.0, 0.0, "next"), item(0.0, 0.0, "userqueue", removable=False),
                 WindowItem("spotify:episode:pod", kind="episode", removable=False)]
        items += [item(0.8, 0.8, f"k{i}") for i in range(10)]
        assert choose_removals(items, mood, TARGET, P) == []

    def test_caps_and_minimum_length(self):
        mood = (0.8, 0.8)
        items = [item(0.8, 0.8, "now"), item(0.8, 0.8, "next")] + [item(0.0, 0.05 * i, f"far{i}") for i in range(10)]
        removed = choose_removals(items, mood, TARGET, P)
        assert len(removed) == P.max_removals_per_cycle
        short = items[:9]  # 8 upcoming = min_length: nothing may go
        assert choose_removals(short, mood, TARGET, P) == []


class TestPlanCycle:
    def chain(self):
        return MoodChain(TARGET, MarkovParams())

    def test_refills_a_short_queue(self):
        items = [item(0.9, 0.9, "now"), item(0.9, 0.9, "next")]
        plan = plan_cycle(items, grid_table(), TARGET, self.chain(), np.random.default_rng(0), P, set())
        assert len(plan.insertions) == P.target_length - 1
        assert plan.mood == pytest.approx((0.9, 0.9))

    def test_off_target_adds_a_few_toward_the_target(self):
        items = [item(0.9, 0.9, f"s{i}") for i in range(12)]
        plan = plan_cycle(items, grid_table(), TARGET, self.chain(), np.random.default_rng(0), P, set())
        assert 1 <= len(plan.insertions) <= P.max_adds_per_cycle
        for _, t in plan.insertions:
            assert distance(t.point, TARGET) < distance(plan.mood, TARGET)

    def test_full_window_only_replaces_removed_songs(self):
        items = [item(0.9, 0.9, f"s{i}") for i in range(P.window_size)]
        plan = plan_cycle(items, grid_table(), TARGET, self.chain(), np.random.default_rng(0), P, set())
        assert plan.insertions == [] and plan.removals == []

    def test_settled_only_keeps_the_queue_full(self):
        items = [item(0.5, 0.45, f"s{i}") for i in range(10)]
        plan = plan_cycle(items, grid_table(), TARGET, self.chain(), np.random.default_rng(0), P, set())
        assert plan.settled and plan.removals == []
        assert len(plan.insertions) == P.target_length - 9
        for _, t in plan.insertions:
            assert distance(t.point, TARGET) < 0.15

    def test_window_songs_are_not_picked_again(self):
        table = grid_table()
        items = [WindowItem(t.uri, t.name, t.artists, t.features) for t in table.tracks[:5]]
        plan = plan_cycle(items, table, TARGET, self.chain(), np.random.default_rng(0), P, set())
        assert not {t.id for _, t in plan.insertions} & {it.track_id for it in items}


def test_listening_mood_counts_the_song_playing_and_the_users_songs_once():
    from backend.selection import listening_mood
    now = WindowItem("spotify:track:a", "A", "x", feats(0.2, 0.2), ours=True)   # Moodly's, but playing: counts
    mine = WindowItem("spotify:track:b", "B", "x", feats(0.4, 0.4))
    again = WindowItem("spotify:track:b2", "B", "x", feats(0.4, 0.4))          # same song, relinked id
    pick = WindowItem("spotify:track:c", "C", "x", feats(0.9, 0.9), ours=True)  # not played yet: doesn't
    assert listening_mood([now, mine, again, pick]) == pytest.approx((0.3, 0.3))
    assert queue_mood([now, mine, again, pick]) == pytest.approx((0.475, 0.475))  # the queue's heading

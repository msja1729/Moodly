"""Choosing which songs to add to the session playlist, where, and which to remove.

Pure functions (no Spotify calls), so every rule here is unit-tested.

The listening window is the song now playing plus the upcoming songs:

    mood m       = mean (valence, arousal) of the song playing + the user's
                   own songs ahead; Moodly's picks count once they play    (listening_mood)
    heading h    = mean of the whole window, Moodly's picks included       (queue_mood)
    waypoint w   = one Markov-chain step from h toward the target t        (mood_space)
    candidates   = songs near w, closer to t than m                       (rank_candidates)
                   (from the recommender's pool: the user's taste and top-2
                   languages; or the static catalog as a fallback)
                   ranked by distance to w + musical fit with the window:
                   tempo (half/double time aware), Camelot key, loudness,
                   danceability/acousticness, - taste - freshness bonus
                   (- familiarity/popularity bonus)
    picks        = a varied subset of the best (Maximal Marginal Relevance) (mmr_select)
    positions    = "random" (default): random positions in the middle of the
                   upcoming songs, so new songs are heard soon             (random_positions)
                   "path": in m -> t order, at the gap with the cheapest
                   insertion cost T(a,c) + T(c,b) - T(a,b)                 (plan_insertions)
                   never before position 2 in either mode
    removals     = upcoming songs far from BOTH m and t, capped (a larger
                   cap right after a target change); the only removal rule  (choose_removals)
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Mapping, Sequence

import numpy as np

from backend.catalog import CatalogTrack, TrackTable
from backend.features import Features
from backend.mood_space import Direction, MoodChain, Point, distance


@dataclass(frozen=True)
class SelectionParams:
    window_size: int = 20             # songs (now playing + upcoming) that define the mood
    target_length: int = 15           # upcoming songs the session tries to keep
    min_length: int = 8               # never fewer upcoming songs than this
    protect_head: int = 2             # never insert before / remove at positions 0 (now) and 1 (next)
    max_adds_per_cycle: int = 3
    max_removals_per_cycle: int = 2
    settle_radius: float = 0.05       # within this of the target: maintenance only
    search_radius: float = 0.08
    min_candidates: int = 200
    shortlist: int = 50               # best-scored candidates that MMR chooses from
    mmr_lambda: float = 0.7
    remove_radius_mood: float = 0.35
    remove_radius_target: float = 0.35
    w_tempo: float = 0.15
    w_key: float = 0.05
    w_loudness: float = 0.05
    w_feel: float = 0.05
    w_energy_jump: float = 0.3        # transition cost only: arousal jump between neighbours
    w_familiar: float = 0.02
    w_popularity: float = 0.01
    w_taste: float = 0.12             # recommender taste score (0..1): favourite artists > related artists
    w_fresh: float = 0.03             # recommender freshness (0..1): released in the last year
    w_trajectory: float = 0.5         # insertion ("path" mode): keep the m -> t order
    allow_explicit: bool = True
    insert_mode: str = "random"       # "random": random positions in the middle; "path": m -> t order + cheapest gap
    max_removals_on_retarget: int = 5  # a new target clears up to this many songs far from it

    @classmethod
    def from_config(cls, cfg: Mapping) -> "SelectionParams":
        s = cfg.get("session", {})
        out = {}
        for k, v in s.items():
            if k in cls.__dataclass_fields__:
                out[k] = type(getattr(cls, k))(v)
        return cls(**out)


@dataclass(frozen=True)
class WindowItem:
    """One entry of the listening window (a playlist row or a song in Spotify's own queue)."""
    uri: str
    name: str = "?"
    artists: str = ""
    features: Features | None = None
    kind: str = "track"          # "track" or "episode"
    removable: bool = True       # False: now playing, next, episodes, the user's own Spotify queue
    ours: bool = False           # added by Moodly (not counted in the mood until it plays)

    @property
    def track_id(self) -> str | None:
        return self.uri.rsplit(":", 1)[-1] if self.uri.startswith("spotify:track:") else None

    @property
    def point(self) -> Point | None:
        return self.features.point if self.features is not None else None

    @property
    def key(self) -> str:
        return f"{self.name}|{self.artists}".casefold().strip()


# --------------------------------------------------------------------------- mood

def queue_mood(items: Sequence[WindowItem], window_size: int | None = None) -> Point | None:
    """Plain average (valence, arousal) of the window's songs with a known point: where the whole queue,
    Moodly's picks included, is heading. Used to place the next Markov step."""
    pts = [it.point for it in (items[:window_size] if window_size else items) if it.point is not None]
    if not pts:
        return None
    return (float(np.mean([p[0] for p in pts])), float(np.mean([p[1] for p in pts])))


def listening_mood(items: Sequence[WindowItem], window_size: int | None = None) -> Point | None:
    """The mood: what the user is actually listening to. The song playing (whoever picked it) plus the
    user's own songs ahead: their queue, or the playlist they were playing. Moodly's picks still ahead
    don't count until they play. Each song once (Spotify can list a song twice)."""
    seen: set[str] = set()
    pts = []
    for i, it in enumerate(items):
        if (i > 0 and it.ours) or it.key in seen:
            continue
        seen.add(it.key)
        if it.point is not None:
            pts.append(it.point)
        if window_size and len(seen) >= window_size:
            break
    if not pts:
        return None
    return (float(np.mean([p[0] for p in pts])), float(np.mean([p[1] for p in pts])))


# --------------------------------------------------------------------------- musical distances

def tempo_distance(tempo, ref) -> np.ndarray:
    """|log2| tempo ratio, treating half and double time as the same pulse. 0 = same, capped at 1.

    Unknown tempos give a neutral 0.5.
    """
    t = np.asarray(tempo, dtype=float)
    if ref is None or not np.isfinite(ref) or ref <= 0:
        return np.full(t.shape, 0.5)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.log2(t / ref)
    d = np.minimum(np.abs(r), np.minimum(np.abs(r + 1), np.abs(r - 1)))
    return np.where(np.isfinite(d) & (t > 0), np.clip(d, 0, 1), 0.5)


def camelot(key, mode) -> tuple[np.ndarray, np.ndarray]:
    """Camelot wheel position (1..12) and letter (0 = A/minor, 1 = B/major) for pitch class + mode."""
    k = np.asarray(key, dtype=int)
    m = np.asarray(mode, dtype=int)
    num = np.where(m == 1, (7 * k + 8) % 12, (7 * k + 5) % 12)
    return np.where(num == 0, 12, num), (m == 1).astype(int)


def key_distance(key, mode, ref_key, ref_mode) -> np.ndarray:
    """Harmonic distance on the Camelot wheel, in [0, 1].

    Same key 0; relative major/minor or a neighbouring number 1/7; further
    steps add up. Unknown keys give a neutral 0.5.
    """
    k = np.asarray(key, dtype=int)
    m = np.asarray(mode, dtype=int)
    if ref_key is None or ref_mode is None or ref_key < 0 or ref_mode < 0:
        return np.full(k.shape, 0.5)
    num, letter = camelot(k, m)
    rnum, rletter = camelot(ref_key, ref_mode)
    dn = np.abs(num - rnum)
    dn = np.minimum(dn, 12 - dn)
    steps = np.where(letter == rletter, dn, np.where(dn == 0, 1, dn + 1))
    return np.where((k >= 0) & (m >= 0), np.clip(steps / 7.0, 0, 1), 0.5)


def _diff(a, b, scale: float):
    if a is None or b is None or (isinstance(a, float) and math.isnan(a)) or (isinstance(b, float) and math.isnan(b)):
        return 0.5
    return min(abs(a - b) / scale, 1.0)


def transition_cost(a: Features | None, b: Features | None, p: SelectionParams) -> float:
    """How jarring it is to go from song a to song b. Unknown songs cost a neutral amount."""
    if a is None or b is None:
        return 0.5 * (p.w_tempo + p.w_key + p.w_loudness + p.w_feel + p.w_energy_jump)
    tempo = float(tempo_distance([b.tempo if b.tempo is not None else np.nan], a.tempo)[0])
    key = float(key_distance([-1 if b.key is None else b.key], [-1 if b.mode is None else b.mode],
                             a.key, a.mode)[0])
    feel = (_diff(a.danceability, b.danceability, 1.0) + _diff(a.acousticness, b.acousticness, 1.0)) / 2
    return (p.w_tempo * tempo + p.w_key * key + p.w_loudness * _diff(a.loudness, b.loudness, 20.0)
            + p.w_feel * feel + p.w_energy_jump * abs(a.energy - b.energy))


# --------------------------------------------------------------------------- picking

@dataclass(frozen=True)
class MusicalContext:
    """Typical musical character of the window, which new songs should fit."""
    tempo: float | None
    key: int | None
    mode: int | None
    loudness: float | None
    danceability: float | None
    acousticness: float | None

    @classmethod
    def of(cls, items: Sequence[WindowItem]) -> "MusicalContext":
        feats = [it.features for it in items if it.features is not None]

        def med(attr):
            vals = [getattr(f, attr) for f in feats if getattr(f, attr) is not None]
            return float(np.median(vals)) if vals else None

        keys = Counter((f.key, f.mode) for f in feats if f.key is not None and f.key >= 0 and f.mode is not None)
        key, mode = keys.most_common(1)[0][0] if keys else (None, None)
        return cls(med("tempo"), key, mode, med("loudness"), med("danceability"), med("acousticness"))


def rank_candidates(table: TrackTable, waypoint: Point, mood: Point, target: Point,
                    context: MusicalContext, exclude_ids: set[str], params: SelectionParams,
                    settled: bool = False, languages: frozenset[str] | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Shortlist of (indices, scores) into `table`, best (lowest score) first.

    `languages`: only songs in these languages (the user's top 2), if given.
    """
    if not len(table):
        return np.array([], dtype=int), np.array([])
    mask = np.ones(len(table), dtype=bool)
    if not settled:
        # Only songs strictly closer to the target than the current mood can pull the average toward it.
        mask &= table.distances(target) < distance(mood, target)
    if exclude_ids:
        excluded = [table.index[i] for i in exclude_ids if i in table.index]
        mask[excluded] = False
    if not params.allow_explicit:
        mask &= ~table.explicit
    if languages:
        mask &= np.isin(table.language, list(languages))
    idx = table.radius_search(waypoint, params.search_radius, mask, params.min_candidates)
    if not len(idx):
        return idx, np.array([])

    d_w = np.hypot(table.va[idx, 0] - waypoint[0], table.va[idx, 1] - waypoint[1])
    d_tempo = tempo_distance(table.tempo[idx], context.tempo)
    d_key = key_distance(table.key[idx], table.mode[idx], context.key, context.mode)
    d_loud = (np.clip(np.abs(table.loudness[idx] - context.loudness) / 20.0, 0, 1)
              if context.loudness is not None else np.full(len(idx), 0.5))
    d_loud = np.where(np.isfinite(d_loud), d_loud, 0.5)
    feel_parts = []
    for col, ref in ((table.danceability, context.danceability), (table.acousticness, context.acousticness)):
        part = np.abs(col[idx] - ref) if ref is not None else np.full(len(idx), 0.5)
        feel_parts.append(np.where(np.isfinite(part), part, 0.5))
    d_feel = (feel_parts[0] + feel_parts[1]) / 2

    score = (d_w + params.w_tempo * d_tempo + params.w_key * d_key + params.w_loudness * d_loud
             + params.w_feel * d_feel - params.w_familiar * table.library[idx]
             - params.w_popularity * table.popularity[idx]
             - params.w_taste * table.taste[idx] - params.w_fresh * table.fresh[idx])
    order = np.argsort(score, kind="stable")[: params.shortlist]
    return idx[order], score[order]


def main_artist(artists: str) -> str:
    return artists.split(",")[0].strip().lower()


def mmr_select(table: TrackTable, idx: np.ndarray, scores: np.ndarray, k: int, lam: float = 0.7,
               avoid_artists: set[str] | None = None) -> list[CatalogTrack]:
    """Maximal Marginal Relevance: good songs that are not near-duplicates of each other.

    relevance = 1 for the best score .. 0 for the worst in the shortlist;
    similarity = closeness in (valence, arousal, danceability, acousticness, tempo).
    Artists are a hard rule: a song by an artist already picked (or in
    `avoid_artists`) is only taken when no other artist is left.
    """
    if k <= 0 or not len(idx):
        return []
    span = float(scores.max() - scores.min()) or 1.0
    relevance = 1.0 - (scores - scores.min()) / span
    tempo_norm = np.nan_to_num(table.tempo[idx] / 200.0, nan=0.6)
    vecs = np.column_stack([table.va[idx], np.nan_to_num(table.danceability[idx], nan=0.5),
                            table.acousticness[idx], tempo_norm])
    artists = [main_artist(table.tracks[i].artists) for i in idx]
    avoid = {a.lower() for a in (avoid_artists or set())}

    chosen: list[int] = []
    for _ in range(min(k, len(idx))):
        used = avoid | {artists[c] for c in chosen}
        pool = [j for j in range(len(idx)) if j not in chosen and artists[j] not in used]
        if not pool:  # every remaining song is by an artist already used
            pool = [j for j in range(len(idx)) if j not in chosen]
        best, best_val = None, -np.inf
        for j in pool:
            sim = max((1.0 - min(float(np.linalg.norm(vecs[j] - vecs[c])) / 0.5, 1.0) for c in chosen), default=0.0)
            val = lam * relevance[j] - (1 - lam) * sim
            if val > best_val:
                best, best_val = j, val
        chosen.append(best)
    return [table.tracks[idx[j]] for j in chosen]


# --------------------------------------------------------------------------- positions and removals

def _projection(p: Point | None, mood: Point, target: Point) -> float | None:
    if p is None:
        return None
    dv, de = target[0] - mood[0], target[1] - mood[1]
    n2 = dv * dv + de * de
    if n2 < 1e-12:
        return 0.0
    return ((p[0] - mood[0]) * dv + (p[1] - mood[1]) * de) / n2


def plan_insertions(items: Sequence[WindowItem], picks: Sequence[CatalogTrack], mood: Point, target: Point,
                    params: SelectionParams) -> list[tuple[int, CatalogTrack]]:
    """Where to insert each pick: [(position, track)], to be applied in this order.

    Each position refers to the list as it is after the previous insertions
    (position 0 = now playing). Picks are placed in order of their progress
    from m toward t; each goes at the gap with the lowest
    T(a, c) + T(c, b) - T(a, b) + w_trajectory * |progress mismatch with the gap|.
    """
    seq: list[WindowItem] = list(items)
    ordered = sorted(picks, key=lambda c: _projection(c.point, mood, target) or 0.0)
    lower = min(params.protect_head, len(seq))
    out: list[tuple[int, CatalogTrack]] = []
    for c in ordered:
        pc = _projection(c.point, mood, target)
        # Strict path order: never before an upcoming song that is less far along m -> t,
        # so the songs nearest the current mood play (and leave the window) first.
        start = lower
        if pc is not None:
            for i in range(len(seq) - 1, lower - 1, -1):
                pi = _projection(seq[i].point, mood, target)
                if pi is not None and pi < pc - 1e-9:
                    start = max(lower, i + 1)
                    break
        best_pos, best_cost = None, math.inf
        for pos in range(start, len(seq) + 1):
            a = seq[pos - 1].features if pos > 0 else None
            b = seq[pos].features if pos < len(seq) else None
            cost = transition_cost(a, c.features, params)
            if pos < len(seq):
                cost += transition_cost(c.features, b, params) - transition_cost(a, b, params)
            neighbours = [x for x in (_projection(seq[pos - 1].point, mood, target) if pos > 0 else None,
                                      _projection(seq[pos].point, mood, target) if pos < len(seq) else None)
                          if x is not None]
            if neighbours and pc is not None:
                cost += params.w_trajectory * abs(pc - float(np.mean(neighbours)))
            if cost < best_cost - 1e-12:
                best_pos, best_cost = pos, cost
        seq.insert(best_pos, WindowItem(c.uri, c.name, c.artists, c.features))
        out.append((best_pos, c))
        lower = best_pos + 1  # keep the picks in m -> t order
    return out


def random_positions(items: Sequence[WindowItem], picks: Sequence[CatalogTrack], params: SelectionParams,
                     rng: np.random.Generator) -> list[tuple[int, CatalogTrack]]:
    """Random positions in the middle of the upcoming songs: [(position, track)], applied in order.

    Never before position `protect_head` (now playing / next), and not
    after the last song while there is a middle to choose from.
    """
    seq_len = len(items)
    out: list[tuple[int, CatalogTrack]] = []
    for c in picks:
        lo = min(params.protect_head, seq_len)
        hi = max(lo, seq_len - 1)  # last gap *before* the final song
        pos = int(rng.integers(lo, hi + 1))
        out.append((pos, c))
        seq_len += 1
    return out


def choose_removals(items: Sequence[WindowItem], mood: Point, target: Point, params: SelectionParams,
                    cap: int | None = None) -> list[int]:
    """Indices (into `items`) of upcoming songs far from both the mood and the target. The only rule
    for removing a song (also right after a new target, with a larger `cap`).

    Never touches protected positions or non-removable items; at most
    max_removals_per_cycle (or `cap`); never leaves fewer than min_length upcoming.
    """
    upcoming = len(items) - 1
    budget = min(params.max_removals_per_cycle if cap is None else cap, max(0, upcoming - params.min_length))
    if budget <= 0:
        return []
    far: list[tuple[float, int]] = []
    for i, it in enumerate(items):
        if i < params.protect_head or not it.removable or it.point is None:
            continue
        d_m, d_t = distance(it.point, mood), distance(it.point, target)
        if d_m > params.remove_radius_mood and d_t > params.remove_radius_target:
            far.append((min(d_m, d_t), i))
    far.sort(reverse=True)  # the farthest first
    return sorted(i for _, i in far[:budget])


# --------------------------------------------------------------------------- one cycle

@dataclass
class CyclePlan:
    mood: Point | None                 # what the user is listening to (listening_mood)
    deviation: float | None
    waypoint: Point | None = None
    ahead: Point | None = None         # where the whole queue, Moodly's picks included, is heading
    direction: Direction | None = None
    settled: bool = False
    removals: list[int] = field(default_factory=list)                 # indices into the window
    insertions: list[tuple[int, CatalogTrack]] = field(default_factory=list)  # positions after removals


def plan_cycle(items: Sequence[WindowItem], table: TrackTable, target: Point, chain: MoodChain,
               rng: np.random.Generator, params: SelectionParams, exclude_ids: set[str],
               retarget: bool = False, languages: frozenset[str] | None = None) -> CyclePlan:
    """Decide one reshaping step for the window (now playing first).

    `retarget`: the user just picked a new target, so songs far from it are cleared out.
    """
    ahead = queue_mood(items, params.window_size)
    mood = listening_mood(items, params.window_size) or ahead
    if mood is None:
        return CyclePlan(None, None)
    dev = distance(mood, target)
    settled = dev <= params.settle_radius
    plan = CyclePlan(mood, dev, settled=settled, ahead=ahead)

    # Only songs far from both the mood and the target leave the queue (a few more right after a new target).
    if retarget:
        plan.removals = choose_removals(items, mood, target, params, cap=params.max_removals_on_retarget)
    elif not settled:
        plan.removals = choose_removals(items, mood, target, params)
    kept = [it for i, it in enumerate(items) if i not in set(plan.removals)]

    upcoming = len(kept) - 1
    need = params.target_length - upcoming
    if upcoming < params.min_length:
        k = need                                     # refill: music must not run out
    elif settled:
        k = max(0, need)                             # at the target: only keep the queue full
    elif upcoming >= params.window_size - 1:
        k = min(len(plan.removals), max(params.max_adds_per_cycle, len(plan.removals)))  # only replace
    else:
        k = min(max(need, 1), max(params.max_adds_per_cycle, len(plan.removals)))
    if k <= 0:
        return plan

    if settled:
        plan.waypoint, plan.direction = target, (0, 0)
    else:
        # Step on from where the queue is heading, so each new pick goes one step further.
        plan.waypoint, plan.direction = chain.next_waypoint(ahead or mood, rng)
    context = MusicalContext.of(kept)
    window_ids = {it.track_id for it in items if it.track_id}
    idx, scores = rank_candidates(table, plan.waypoint, mood, target, context, exclude_ids | window_ids,
                                  params, settled, languages)
    # Don't repeat the artists of the songs right around the insertion area.
    window_artists = {main_artist(it.artists) for it in kept[1: params.protect_head + 4] if it.artists}
    picks = mmr_select(table, idx, scores, k, params.mmr_lambda, window_artists)
    if params.insert_mode == "path":
        plan.insertions = plan_insertions(kept, picks, mood, target, params)
    else:
        plan.insertions = random_positions(kept, picks, params, rng)
    return plan

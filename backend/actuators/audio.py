"""Audio actuator: queue bridging tracks that walk the mood toward the target.

When the estimated mood is more than `deviation_threshold` from the target,
the Markov chain (backend.mood_space) samples the next waypoint one grid step
closer to the target, and the candidate track nearest that waypoint is pushed
to the Spotify queue. Only candidates strictly closer to the target than the
current mood are eligible, so a bridging track always lies between the two.

Pacing: one bridging track in flight at a time, and at most one per
`cooldown_s` on ordinary track changes (a new target bypasses the cooldown).
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from typing import Mapping

import numpy as np

from backend.features import CsvFeatureProvider, FeatureChain, Features
from backend.feedback import FeedbackLog
from backend.mood_space import Direction, MarkovParams, MoodChain, Point, distance

log = logging.getLogger(__name__)

IN_FLIGHT_TIMEOUT_S = 600


@dataclass(frozen=True)
class AudioParams:
    enabled: bool = True
    deviation_threshold: float = 0.2
    cooldown_s: float = 90.0
    recent_exclusion: int = 50
    acousticness_weight: float = 0.05
    dataset_penalty: float = 0.03
    pool_size: int = 200

    @classmethod
    def from_config(cls, cfg: Mapping) -> "AudioParams":
        a = cfg.get("audio", {})
        return cls(**{k: type(getattr(cls, k))(v) for k, v in a.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class Candidate:
    track_id: str
    name: str
    artists: str
    features: Features
    source: str  # "library" (the user's own tracks) or "dataset"

    @property
    def uri(self) -> str:
        return f"spotify:track:{self.track_id}"


class CandidatePool:
    def __init__(self, candidates: list[Candidate]):
        self.candidates = candidates
        self._pts = np.array([[c.features.valence, c.features.energy] for c in candidates]).reshape(-1, 2)
        self._ac = np.array([c.features.acousticness for c in candidates])
        self._lib = np.array([c.source == "library" for c in candidates], dtype=bool)

    def __len__(self) -> int:
        return len(self.candidates)

    def nearest(self, waypoint: Point, current: Point, target: Point, exclude: set[str] = frozenset(),
                acousticness_weight: float = 0.05, dataset_penalty: float = 0.03) -> Candidate | None:
        if not self.candidates:
            return None
        d_target = np.hypot(self._pts[:, 0] - target[0], self._pts[:, 1] - target[1])
        eligible = d_target < distance(current, target)
        if exclude:
            eligible &= np.array([c.track_id not in exclude for c in self.candidates])
        if not eligible.any():
            return None
        acoustic_target = 1.0 - waypoint[1]  # calmer waypoints prefer acoustic tracks
        score = (
            np.hypot(self._pts[:, 0] - waypoint[0], self._pts[:, 1] - waypoint[1])
            + acousticness_weight * np.abs(self._ac - acoustic_target)
            + dataset_penalty * ~self._lib
        )
        score[~eligible] = np.inf
        return self.candidates[int(np.argmin(score))]


def build_pool(client, chain: FeatureChain, dataset: CsvFeatureProvider | None, limit: int = 200) -> CandidatePool:
    """The user's top/saved tracks (familiar first), plus the local dataset if present."""
    candidates: list[Candidate] = []
    try:
        tracks = client.library_tracks(limit)
        feats = chain.get_many([t.id for t in tracks])
        candidates += [Candidate(t.id, t.name, t.artists, feats[t.id], "library") for t in tracks if t.id in feats]
    except Exception as e:
        log.warning("could not load library tracks: %s", e)
    if dataset is not None:
        seen = {c.track_id for c in candidates}
        candidates += [Candidate(r.id, r.name, r.artists, r.features, "dataset")
                       for r in dataset.rows().values() if r.id not in seen]
    log.info("candidate pool: %d tracks (%d from library)",
             len(candidates), sum(c.source == "library" for c in candidates))
    return CandidatePool(candidates)


@dataclass
class Injection:
    candidate: Candidate
    waypoint: Point
    direction: Direction
    deviation: float
    queued_at: float
    started: bool = False


class AudioActuator:
    def __init__(self, client, pool: CandidatePool, feedback: FeedbackLog | None, clock,
                 params: AudioParams = AudioParams(), markov: MarkovParams = MarkovParams(),
                 rng: np.random.Generator | None = None):
        self.client = client
        self.pool = pool
        self.feedback = feedback
        self.clock = clock
        self.params = params
        self.markov = markov
        self.rng = rng or np.random.default_rng()
        self.pending: dict[str, Injection] = {}
        self.recent: deque[str] = deque(maxlen=params.recent_exclusion)
        self.last_injection_at: float | None = None

    def chain_for(self, target: Point) -> MoodChain:
        stats = self.feedback.skip_stats() if self.feedback else None
        return MoodChain(target, self.markov, stats)

    def note_started(self, track_id: str) -> None:
        self.recent.append(track_id)
        if track_id in self.pending:
            self.pending[track_id].started = True

    def take_intervention(self, ended_track_id: str) -> tuple[str, Direction | None]:
        """Called when a track ends: was it one of ours?"""
        inj = self.pending.pop(ended_track_id, None)
        return ("audio_bridge", inj.direction) if inj else ("none", None)

    def _in_flight(self) -> bool:
        now = self.clock.now()
        for tid, inj in list(self.pending.items()):
            if now - inj.queued_at > IN_FLIGHT_TIMEOUT_S and not inj.started:
                del self.pending[tid]  # user cleared the queue or switched context
        return any(not inj.started for inj in self.pending.values())

    def plan(self, mood: Point, target: Point) -> tuple[Candidate, Point, Direction] | None:
        """Pick the next bridging track without queueing it."""
        waypoint, direction = self.chain_for(target).next_waypoint(mood, self.rng)
        cand = self.pool.nearest(waypoint, mood, target, set(self.recent) | set(self.pending),
                                 self.params.acousticness_weight, self.params.dataset_penalty)
        return (cand, waypoint, direction) if cand else None

    async def maybe_inject(self, mood: Point | None, target: Point, reason: str = "track_change") -> Injection | None:
        if not self.params.enabled or mood is None:
            return None
        deviation = distance(mood, target)
        if deviation <= self.params.deviation_threshold:
            return None
        if self._in_flight():
            return None
        now = self.clock.now()
        if (reason != "target_change" and self.last_injection_at is not None
                and now - self.last_injection_at < self.params.cooldown_s):
            return None

        planned = self.plan(mood, target)
        if planned is None:
            log.info("no bridging candidate between %s and %s", mood, target)
            return None
        cand, waypoint, direction = planned
        try:
            await asyncio.to_thread(self.client.add_to_queue, cand.uri)
        except Exception as e:
            log.warning("add_to_queue failed: %s", e)
            return None

        inj = Injection(cand, waypoint, direction, deviation, now)
        self.pending[cand.track_id] = inj
        self.last_injection_at = now
        log.info("queued bridge %s - %s (v=%.2f e=%.2f) toward waypoint (%.2f, %.2f), deviation %.2f",
                 cand.artists, cand.name, cand.features.valence, cand.features.energy,
                 waypoint[0], waypoint[1], deviation)
        if self.feedback:
            self.feedback.log_intervention(now, "audio_bridge", f"{cand.uri} dir={direction}", mood)
        return inj

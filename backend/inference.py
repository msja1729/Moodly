"""Mood inference from Spotify playback.

Each poll reads the currently playing track. When the track changes we
 * estimate how far the previous track got and call it skipped if it played
   less than `skip_threshold` of its duration, and
 * fold the new track's (valence, energy) into an exponential moving average.

Skip estimate: the new track started roughly `progress_ms` before this
observation, which is also when the previous track stopped. The previous
track's progress then = its last observed progress + the time between that
observation and the change (clamped to the window between the two polls).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Mapping

import numpy as np

from backend.features import FeatureChain, Features
from backend.mood_space import Point
from backend.spotify.client import Playback, RateLimited, Track

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InferenceParams:
    ema_alpha: float = 0.3
    skip_weight: float = 0.3
    skip_threshold: float = 0.8
    poll_min_s: float = 0.75    # while music plays: the sidebar shows a skip or a new song within this
    poll_max_s: float = 2.0     # paused / nothing playing
    max_backoff_s: float = 5.0      # after errors (e.g. Spotify timing out); a 429 still waits as told

    @classmethod
    def from_config(cls, cfg: Mapping) -> "InferenceParams":
        i = cfg.get("inference", {})
        return cls(**{k: float(v) for k, v in i.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class TrackStarted:
    track: Track
    features: Features | None


@dataclass(frozen=True)
class TrackChanged:
    track: Track  # the track that just ended
    features: Features | None
    skipped: bool
    listened_ms: int


@dataclass
class PollResult:
    events: list = field(default_factory=list)
    delay_s: float = 30.0
    playback: Playback | None = None
    error: str | None = None


class MoodEstimator:
    """EMA over track features; a skip shrinks that track's contribution."""

    def __init__(self, alpha: float = 0.3, skip_weight: float = 0.3):
        self.alpha = alpha
        self.skip_weight = skip_weight
        self.mood: Point | None = None
        self._before_last: Point | None = None
        self._last: Features | None = None

    def push(self, f: Features) -> Point:
        self._before_last, self._last = self.mood, f
        self.mood = self._blend(self.mood, f, self.alpha)
        return self.mood

    def mark_last_skipped(self) -> Point | None:
        """Redo the most recent update with a reduced weight."""
        if self._last is not None and self._before_last is not None:
            self.mood = self._blend(self._before_last, self._last, self.alpha * self.skip_weight)
        self._last = None
        return self.mood

    @staticmethod
    def _blend(base: Point | None, f: Features, a: float) -> Point:
        if base is None:
            return f.point
        return (base[0] + a * (f.valence - base[0]), base[1] + a * (f.energy - base[1]))


class InferenceEngine:
    def __init__(self, client, features: FeatureChain, clock, params: InferenceParams = InferenceParams(),
                 rng: np.random.Generator | None = None):
        self.client = client
        self.features = features
        self.clock = clock
        self.params = params
        self.rng = rng or np.random.default_rng()
        self.estimator = MoodEstimator(params.ema_alpha, params.skip_weight)
        self.last: Playback | None = None
        self.last_features: Features | None = None
        self.consecutive_errors = 0

    @property
    def mood(self) -> Point | None:
        return self.estimator.mood

    async def poll(self) -> PollResult:
        try:
            pb = await asyncio.to_thread(self.client.current_playback)
            feats = None
            if pb is not None and (self.last is None or pb.track.id != self.last.track.id):
                feats = await asyncio.to_thread(self.features.get, pb.track.id)
        except RateLimited as e:
            self.consecutive_errors += 1
            delay = max(e.retry_after, self._backoff())
            log.warning("Spotify rate limit; next poll in %.0fs", delay)
            return PollResult(delay_s=delay, error="rate_limited")
        except Exception as e:
            self.consecutive_errors += 1
            delay = self._backoff()
            log.warning("poll failed (%s); next poll in %.0fs", e, delay)
            return PollResult(delay_s=delay, error=type(e).__name__)
        self.consecutive_errors = 0
        events = self.observe(pb, feats)
        return PollResult(events=events, delay_s=self.next_delay(pb), playback=pb)

    def observe(self, pb: Playback | None, feats: Features | None) -> list:
        """Update state from one observation. `feats` is only used for a new track."""
        if pb is None:
            return []  # nothing playing; the previous track is judged when music resumes
        if self.last is not None and pb.track.id == self.last.track.id:
            self.last = pb
            return []

        events: list = []
        if self.last is not None:
            listened = self._estimate_listened_ms(self.last, pb)
            skipped = listened < self.params.skip_threshold * self.last.track.duration_ms
            if skipped:
                self.estimator.mark_last_skipped()
            events.append(TrackChanged(self.last.track, self.last_features, skipped, listened))

        if feats is not None:
            self.estimator.push(feats)
        else:
            log.info("no audio features for %s - %s", pb.track.artists, pb.track.name)
        events.append(TrackStarted(pb.track, feats))
        self.last, self.last_features = pb, feats
        return events

    @staticmethod
    def _estimate_listened_ms(prev: Playback, now: Playback) -> int:
        change_at = now.observed_at - now.progress_ms / 1000
        change_at = min(max(change_at, prev.observed_at), now.observed_at)
        played = prev.progress_ms + ((change_at - prev.observed_at) * 1000 if prev.is_playing else 0)
        return int(min(played, prev.track.duration_ms))

    def next_delay(self, pb: Playback | None) -> float:
        """Poll every `poll_min_s` while playing (a skip shows within seconds), and right after the
        song is due to end, so the sidebar switches to the next song on the spot."""
        lo, hi = self.params.poll_min_s, self.params.poll_max_s
        if pb is None or not pb.is_playing:
            return hi
        remaining = (pb.track.duration_ms - pb.progress_ms) / 1000
        return float(max(0.5, min(lo, remaining + 0.4)))

    def _backoff(self) -> float:
        return min(self.params.poll_min_s * 2 ** (self.consecutive_errors - 1), self.params.max_backoff_s)

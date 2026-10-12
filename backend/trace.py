"""Mood trace: a developer log of every session step, for plotting the mood path.

Off unless the environment variable AMBIENT_MOOD_TRACE names a file. Each line
is one JSON event:

    cycle   one session cycle: target, mood (queue average), waypoint (the
            Markov step it aimed for), every song in the window with its point,
            the songs added (with their points and why) and removed
    song    a new song started: the mood re-averaged at once
    end     the session ended

tools/mood_trace_view.py serves a live plot of the file.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

ENV = "AMBIENT_MOOD_TRACE"


def _pt(p):
    return None if p is None else [round(float(p[0]), 4), round(float(p[1]), 4)]


class MoodTrace:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last: tuple | None = None

    @classmethod
    def from_env(cls) -> "MoodTrace | None":
        path = os.environ.get(ENV)
        return cls(Path(path)) if path else None

    def write(self, kind: str, **data) -> None:
        data = {"t": round(time.time(), 3), "kind": kind, **data}
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        except OSError as e:
            log.debug("could not write the mood trace: %s", e)

    def cycle(self, *, reason: str, target, plan, window, added, removed, preparing: bool,
              needs_more_songs: bool, queue_size: int) -> None:
        """Record a cycle; a cycle that changed nothing (same mood, waypoint, target, no edits) is skipped."""
        key = (_pt(target) and tuple(_pt(target)), _pt(plan.mood) and tuple(_pt(plan.mood)),
               _pt(plan.waypoint) and tuple(_pt(plan.waypoint)), preparing, tuple(w.uri for w in window))
        if key == self._last and not added and not removed and reason == "timer":
            return
        self._last = key
        self.write(
            "cycle", reason=reason, target=_pt(target), mood=_pt(plan.mood),
            deviation=None if plan.deviation is None else round(float(plan.deviation), 4),
            waypoint=_pt(plan.waypoint),
            direction=None if plan.direction is None else str(plan.direction),
            settled=bool(plan.settled), preparing=preparing, needs_more_songs=needs_more_songs,
            queue_size=queue_size,
            window=[{"name": w.name, "artists": w.artists, "point": _pt(w.point),
                     "removable": w.removable} for w in window],
            added=added, removed=removed)

    def song(self, name: str, point, mood, target) -> None:
        self.write("song", name=name, point=_pt(point), mood=_pt(mood), target=_pt(target))
        self._last = None

"""Telemetry: one row per song change, plus a log of actuator interventions.

events        - [timestamp, current_mood_vector, intervention_type, skipped] per
                song change. intervention_type is "audio_bridge" when the track
                was one the audio actuator queued, else "none". `direction` is
                the grid step that bridging track was chosen for, which feeds
                the Markov chain's skip weights.
interventions - theme/display/audio actions as they happen, for evaluation.
"""
from __future__ import annotations

import csv
import sqlite3
import threading
from pathlib import Path

from backend.mood_space import Direction, Point


class FeedbackLog:
    def __init__(self, db_path: Path | str):
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                valence REAL,
                energy REAL,
                intervention_type TEXT NOT NULL,
                skipped INTEGER NOT NULL,
                track_id TEXT,
                direction TEXT
            );
            CREATE TABLE IF NOT EXISTS interventions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                intervention_type TEXT NOT NULL,
                detail TEXT,
                valence REAL,
                energy REAL
            );
            """
        )
        self._db.commit()

    def log_event(self, ts: float, mood: Point | None, intervention_type: str, skipped: bool,
                  track_id: str | None = None, direction: Direction | None = None) -> None:
        v, e = mood if mood else (None, None)
        d = f"{direction[0]},{direction[1]}" if direction else None
        with self._lock:
            self._db.execute(
                "INSERT INTO events (ts, valence, energy, intervention_type, skipped, track_id, direction)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, v, e, intervention_type, int(skipped), track_id, d),
            )
            self._db.commit()

    def log_intervention(self, ts: float, intervention_type: str, detail: str, mood: Point | None) -> None:
        v, e = mood if mood else (None, None)
        with self._lock:
            self._db.execute(
                "INSERT INTO interventions (ts, intervention_type, detail, valence, energy) VALUES (?, ?, ?, ?, ?)",
                (ts, intervention_type, detail, v, e),
            )
            self._db.commit()

    def events(self) -> list[tuple]:
        with self._lock:
            return self._db.execute(
                "SELECT ts, valence, energy, intervention_type, skipped, track_id, direction FROM events ORDER BY id"
            ).fetchall()

    def skip_stats(self) -> dict[Direction, tuple[int, int]]:
        """(completions, skips) of songs the app added (Phase 1 bridges, Phase 2 playlist inserts) per grid direction."""
        with self._lock:
            rows = self._db.execute(
                "SELECT direction, SUM(1 - skipped), SUM(skipped) FROM events"
                " WHERE intervention_type IN ('audio_bridge', 'playlist_add') AND direction IS NOT NULL"
                " GROUP BY direction"
            ).fetchall()
        out: dict[Direction, tuple[int, int]] = {}
        for d, completions, skips in rows:
            dv, de = (int(x) for x in d.split(","))
            out[(dv, de)] = (int(completions), int(skips))
        return out

    def skip_rate_by_intervention(self) -> dict[str, tuple[int, float]]:
        """{intervention_type: (songs, skip rate)} - did bridging tracks get skipped more?"""
        with self._lock:
            rows = self._db.execute(
                "SELECT intervention_type, COUNT(*), AVG(skipped) FROM events GROUP BY intervention_type"
            ).fetchall()
        return {t: (int(n), float(rate)) for t, n, rate in rows}

    def export_csv(self, path: Path | str) -> None:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ts", "valence", "energy", "intervention_type", "skipped", "track_id", "direction"])
            w.writerows(self.events())

    def close(self) -> None:
        self._db.close()

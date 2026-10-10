"""Audio-feature lookup: Spotify -> ReccoBeats -> local CSV, with a SQLite cache.

Spotify removed GET /audio-features for apps registered after Nov 2024, so the
chain tries each provider in order for whatever IDs are still missing. A
provider that reports it is unavailable (e.g. HTTP 403) is switched off for
the rest of the session. Every hit is cached so a track is only looked up once.
"""
from __future__ import annotations

import csv
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

import requests

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Features:
    """Spotify-style audio features. `energy` is the arousal axis of the mood space.

    The musical descriptors after acousticness are optional: providers that
    don't have them leave them as None.
    """
    valence: float
    energy: float
    acousticness: float
    tempo: float | None = None         # BPM
    key: int | None = None             # pitch class 0=C ... 11=B, -1 unknown
    mode: int | None = None            # 1 major, 0 minor
    loudness: float | None = None      # dB, typically -60..0
    danceability: float | None = None

    @property
    def point(self) -> tuple[float, float]:
        return (self.valence, self.energy)


EXTRA_FIELDS = ("tempo", "key", "mode", "loudness", "danceability")


def _num(value, cast=float):
    try:
        return None if value is None or value == "" else cast(float(value))
    except (TypeError, ValueError):
        return None


def features_from_mapping(row) -> Features:
    """Build Features from a dict-like row (dataset CSV, API JSON). Raises if valence/energy/acousticness are missing."""
    return Features(
        float(row["valence"]), float(row["energy"]), float(row["acousticness"]),
        tempo=_num(row.get("tempo")), key=_num(row.get("key"), int), mode=_num(row.get("mode"), int),
        loudness=_num(row.get("loudness")), danceability=_num(row.get("danceability")),
    )


class FeaturesUnavailable(Exception):
    """The provider can't serve features at all (not just for one track)."""


class FeatureProvider(Protocol):
    name: str

    def fetch(self, track_ids: list[str]) -> dict[str, Features]: ...


class SpotifyFeatureProvider:
    name = "spotify"

    def __init__(self, client):
        self.client = client

    def fetch(self, track_ids: list[str]) -> dict[str, Features]:
        return self.client.audio_features(track_ids)


class ReccoBeatsProvider:
    """https://api.reccobeats.com/v1/audio-features?ids=<spotify ids>

    The response identifies tracks by ReccoBeats' own UUID; the Spotify ID is
    recovered from the `href` (https://open.spotify.com/track/<id>).
    """

    name = "reccobeats"
    URL = "https://api.reccobeats.com/v1/audio-features"
    BATCH = 40

    def __init__(self, session: requests.Session | None = None, timeout: float = 5.0):
        self.session = session or requests.Session()
        self.timeout = timeout

    def fetch(self, track_ids: list[str]) -> dict[str, Features]:
        out: dict[str, Features] = {}
        for i in range(0, len(track_ids), self.BATCH):
            batch = track_ids[i:i + self.BATCH]
            r = self.session.get(self.URL, params={"ids": ",".join(batch)}, timeout=self.timeout)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            for item in r.json().get("content", []):
                sid = (item.get("href") or "").rstrip("/").rsplit("/", 1)[-1]
                if sid in batch:
                    out[sid] = features_from_mapping(item)
        return out


@dataclass(frozen=True)
class CsvTrack:
    id: str
    name: str
    artists: str
    duration_ms: int
    features: Features


class CsvFeatureProvider:
    """A local dataset, e.g. Kaggle's "Spotify Tracks Dataset" (track_id, valence, ...)."""

    name = "csv"

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._rows: dict[str, CsvTrack] | None = None

    def rows(self) -> dict[str, CsvTrack]:
        if self._rows is None:
            self._rows = {}
            if self.path.exists():
                with open(self.path, encoding="utf-8", newline="") as f:
                    for row in csv.DictReader(f):
                        tid = row.get("track_id") or row.get("id")
                        try:
                            feats = features_from_mapping(row)
                        except (KeyError, TypeError, ValueError):
                            continue
                        if tid:
                            self._rows[tid] = CsvTrack(
                                tid,
                                row.get("track_name") or row.get("name") or "?",
                                row.get("artists") or "",
                                int(float(row.get("duration_ms") or 0)),
                                feats,
                            )
                log.info("loaded %d tracks from %s", len(self._rows), self.path)
        return self._rows

    def fetch(self, track_ids: list[str]) -> dict[str, Features]:
        rows = self.rows()
        return {t: rows[t].features for t in track_ids if t in rows}


class FeatureCache:
    def __init__(self, db_path: Path | str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS features ("
            " track_id TEXT PRIMARY KEY, valence REAL, energy REAL, acousticness REAL,"
            " source TEXT, fetched_at REAL)"
        )
        # Older caches predate the musical descriptors; add the columns in place.
        have = {r[1] for r in self._db.execute("PRAGMA table_info(features)")}
        for col in EXTRA_FIELDS:
            if col not in have:
                self._db.execute(f"ALTER TABLE features ADD COLUMN {col} REAL")
        self._db.commit()

    def get_many(self, ids: Iterable[str]) -> dict[str, Features]:
        ids = list(ids)
        if not ids:
            return {}
        with self._lock:
            rows = self._db.execute(
                f"SELECT track_id, valence, energy, acousticness, {', '.join(EXTRA_FIELDS)} FROM features"
                f" WHERE track_id IN ({','.join('?' * len(ids))})",
                ids,
            ).fetchall()
        return {r[0]: Features(r[1], r[2], r[3], r[4], _num(r[5], int), _num(r[6], int), r[7], r[8])
                for r in rows}

    def put_many(self, found: dict[str, Features], source: str) -> None:
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO features (track_id, valence, energy, acousticness, source, fetched_at,"
                f" {', '.join(EXTRA_FIELDS)}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(i, f.valence, f.energy, f.acousticness, source, time.time(),
                  f.tempo, f.key, f.mode, f.loudness, f.danceability) for i, f in found.items()],
            )
            self._db.commit()

    def close(self) -> None:
        self._db.close()


class FeatureChain:
    def __init__(self, providers: list[FeatureProvider], cache: FeatureCache | None = None):
        self.providers = providers
        self.cache = cache
        self.disabled: set[str] = set()

    def get_many(self, track_ids: Iterable[str]) -> dict[str, Features]:
        wanted = list(dict.fromkeys(track_ids))
        found = self.cache.get_many(wanted) if self.cache else {}
        missing = [t for t in wanted if t not in found]
        for provider in self.providers:
            if not missing:
                break
            if provider.name in self.disabled:
                continue
            try:
                got = provider.fetch(missing)
            except FeaturesUnavailable as e:
                log.warning("feature provider %s disabled: %s", provider.name, e)
                self.disabled.add(provider.name)
                continue
            except Exception as e:  # network trouble: try the next provider
                log.warning("feature provider %s failed: %s", provider.name, e)
                continue
            if got:
                found.update(got)
                if self.cache:
                    self.cache.put_many(got, provider.name)
                missing = [t for t in missing if t not in got]
        return found

    def get(self, track_id: str) -> Features | None:
        return self.get_many([track_id]).get(track_id)

"""The track database that new songs are picked from.

    python -m backend.catalog import            # download the dataset and import it
    python -m backend.catalog import --csv F    # import a local CSV instead
    python -m backend.catalog stats

Source: the public Spotify Tracks Dataset (Hugging Face, ~114k rows, ~89k
unique tracks) with valence, energy, tempo, key, mode, loudness,
danceability and acousticness per track. It is imported once into SQLite
(`data/catalog.db`); duplicate rows of the same track (one per genre) are
merged. At run time the daemon loads it into a TrackTable: columnar NumPy
arrays, so a search over the whole catalog is a few vectorised operations.
The user's own top/saved tracks are merged in with source "library".
"""
from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

import numpy as np

from backend.features import Features, features_from_mapping

log = logging.getLogger(__name__)

DATASET_URL = ("https://huggingface.co/datasets/maharshipandya/spotify-tracks-dataset/"
               "resolve/main/dataset.csv")


@dataclass(frozen=True)
class CatalogTrack:
    id: str
    name: str
    artists: str
    features: Features
    duration_ms: int = 0
    genre: str = ""
    popularity: int = 0
    explicit: bool = False
    source: str = "dataset"  # "dataset" or "library" (the user's own top/saved tracks)

    @property
    def uri(self) -> str:
        return f"spotify:track:{self.id}"

    @property
    def point(self) -> tuple[float, float]:
        return self.features.point


def _nan(x) -> float:
    return np.nan if x is None else float(x)


class TrackTable:
    """Columnar, read-only view of a set of tracks for fast search."""

    def __init__(self, tracks: Iterable[CatalogTrack]):
        self.tracks: list[CatalogTrack] = list(tracks)
        self.index = {t.id: i for i, t in enumerate(self.tracks)}
        f = [t.features for t in self.tracks]
        self.va = np.array([[x.valence, x.energy] for x in f], dtype=float).reshape(-1, 2)
        self.tempo = np.array([_nan(x.tempo) for x in f], dtype=float)
        self.key = np.array([-1 if x.key is None else x.key for x in f], dtype=int)
        self.mode = np.array([-1 if x.mode is None else x.mode for x in f], dtype=int)
        self.loudness = np.array([_nan(x.loudness) for x in f], dtype=float)
        self.danceability = np.array([_nan(x.danceability) for x in f], dtype=float)
        self.acousticness = np.array([x.acousticness for x in f], dtype=float)
        self.library = np.array([t.source == "library" for t in self.tracks], dtype=bool)
        self.popularity = np.array([t.popularity / 100.0 for t in self.tracks], dtype=float)
        self.explicit = np.array([t.explicit for t in self.tracks], dtype=bool)

    def __len__(self) -> int:
        return len(self.tracks)

    def get(self, track_id: str) -> CatalogTrack | None:
        i = self.index.get(track_id)
        return None if i is None else self.tracks[i]

    def distances(self, point: tuple[float, float]) -> np.ndarray:
        return np.hypot(self.va[:, 0] - point[0], self.va[:, 1] - point[1])

    def radius_search(self, center: tuple[float, float], radius: float, mask: np.ndarray | None = None,
                      min_results: int = 0, grow: float = 1.5, max_radius: float = 1.5) -> np.ndarray:
        """Indices within `radius` of `center` (and in `mask`), nearest first.

        The radius grows by `grow` until at least `min_results` are found
        (or it reaches `max_radius`).
        """
        if not len(self):
            return np.array([], dtype=int)
        d = self.distances(center)
        if mask is not None:
            d = np.where(mask, d, np.inf)
        r = radius
        while True:
            hits = np.nonzero(d <= r)[0]
            if len(hits) >= min_results or r >= max_radius:
                return hits[np.argsort(d[hits], kind="stable")]
            r *= grow

    def merged_with(self, extra: Iterable[CatalogTrack]) -> "TrackTable":
        """A new table with `extra` added; an extra track already present replaces it (e.g. marks it library)."""
        by_id = {t.id: t for t in self.tracks}
        for t in extra:
            old = by_id.get(t.id)
            if old is not None and t.source == "library":
                # Keep the dataset's richer metadata, but remember it's the user's own song.
                t = replace(old, source="library")
            by_id[t.id] = t
        return TrackTable(by_id.values())


# --------------------------------------------------------------------------- persistence

COLUMNS = ("track_id", "name", "artists", "genre", "popularity", "duration_ms", "explicit",
           "valence", "energy", "acousticness", "tempo", "key", "mode", "loudness", "danceability")


class CatalogDB:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path))
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS catalog ("
            " track_id TEXT PRIMARY KEY, name TEXT, artists TEXT, genre TEXT, popularity INTEGER,"
            " duration_ms INTEGER, explicit INTEGER, valence REAL, energy REAL, acousticness REAL,"
            " tempo REAL, key INTEGER, mode INTEGER, loudness REAL, danceability REAL)"
        )
        self._db.commit()

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM catalog").fetchone()[0])

    def import_csv(self, csv_path: Path | str) -> int:
        """Import a Spotify-tracks CSV. Rows of the same track are merged (genres joined). Returns tracks stored."""
        merged: dict[str, dict] = {}
        skipped = 0
        with open(csv_path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                tid = (row.get("track_id") or row.get("id") or "").strip()
                try:
                    feats = features_from_mapping(row)
                except (KeyError, TypeError, ValueError):
                    skipped += 1
                    continue
                if not tid:
                    skipped += 1
                    continue
                genre = (row.get("track_genre") or row.get("genre") or "").strip()
                if tid in merged:
                    if genre and genre not in merged[tid]["genre"].split(", "):
                        merged[tid]["genre"] = ", ".join(filter(None, [merged[tid]["genre"], genre]))
                    continue
                merged[tid] = {
                    "track_id": tid,
                    "name": row.get("track_name") or row.get("name") or "?",
                    "artists": (row.get("artists") or "").replace(";", ", "),
                    "genre": genre,
                    "popularity": int(float(row.get("popularity") or 0)),
                    "duration_ms": int(float(row.get("duration_ms") or 0)),
                    "explicit": str(row.get("explicit", "")).strip().lower() in ("true", "1"),
                    "valence": feats.valence, "energy": feats.energy, "acousticness": feats.acousticness,
                    "tempo": feats.tempo, "key": feats.key, "mode": feats.mode,
                    "loudness": feats.loudness, "danceability": feats.danceability,
                }
        with self._db:
            self._db.execute("DELETE FROM catalog")
            self._db.executemany(
                f"INSERT INTO catalog ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                [tuple(r[c] for c in COLUMNS) for r in merged.values()],
            )
        log.info("imported %d tracks from %s (%d rows skipped)", len(merged), csv_path, skipped)
        return len(merged)

    def load(self) -> list[CatalogTrack]:
        rows = self._db.execute(f"SELECT {', '.join(COLUMNS)} FROM catalog").fetchall()
        return [
            CatalogTrack(
                id=r[0], name=r[1], artists=r[2], genre=r[3] or "", popularity=r[4] or 0,
                duration_ms=r[5] or 0, explicit=bool(r[6]),
                features=Features(r[7], r[8], r[9], r[10], r[11], r[12], r[13], r[14]),
            )
            for r in rows
        ]

    def close(self) -> None:
        self._db.close()


def download_dataset(dest: Path | str, url: str = DATASET_URL) -> Path:
    import requests

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log.info("downloading %s", url)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    tmp.replace(dest)
    return dest


def load_table(db_path: Path | str) -> TrackTable:
    """The catalog as a TrackTable (empty if it hasn't been imported yet)."""
    if not Path(db_path).exists():
        return TrackTable([])
    db = CatalogDB(db_path)
    try:
        return TrackTable(db.load())
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    from shared.config_io import load_config, resolve_path

    p = argparse.ArgumentParser(description="Ambient Mood track catalog")
    sub = p.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="download (or read) the tracks dataset and import it")
    imp.add_argument("--csv", help="import this CSV instead of downloading")
    sub.add_parser("stats", help="show how many tracks are in the catalog")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    paths = load_config().get("paths", {})
    db_path = resolve_path(paths.get("catalog_db", "data/catalog.db"))
    db = CatalogDB(db_path)
    try:
        if args.cmd == "import":
            csv_path = Path(args.csv) if args.csv else resolve_path(paths.get("tracks_csv", "data/tracks.csv"))
            if not args.csv and not csv_path.exists():
                download_dataset(csv_path)
            n = db.import_csv(csv_path)
            print(f"catalog: {n} tracks in {db_path}")
        else:
            print(f"catalog: {db.count()} tracks in {db_path}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The recommender's cache (SQLite): songs it has seen, their language, and artists.

Not a downloaded catalog: every row comes from a live lookup and is kept so
the same song or artist is never looked up twice. Mood features are cached
separately by the feature chain (backend.features.FeatureCache).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path


@dataclass(frozen=True)
class TrackMeta:
    id: str                                   # Spotify track id
    name: str
    artists: tuple[str, ...]
    artist_ids: tuple[str, ...] = ()
    album: str = ""
    release_date: str = ""                    # "YYYY", "YYYY-MM" or "YYYY-MM-DD"
    isrc: str = ""
    duration_ms: int = 0
    explicit: bool = False
    language: str = ""                        # ISO 639-1, "" = unknown
    lang_source: str = ""                     # marker / script / lyrics-script / lyrics-words / artist
    lyrics_checked: bool = False              # LRCLIB already asked (found or not)

    @property
    def uri(self) -> str:
        return f"spotify:track:{self.id}"

    @property
    def artists_text(self) -> str:
        return ", ".join(self.artists)

    def with_language(self, language: str, source: str, lyrics_checked: bool | None = None) -> "TrackMeta":
        return replace(self, language=language, lang_source=source,
                       lyrics_checked=self.lyrics_checked if lyrics_checked is None else lyrics_checked)


@dataclass
class ArtistInfo:
    name: str                                 # key: casefolded name is used for lookups
    spotify_id: str = ""
    deezer_id: int = 0
    related: list[str] = field(default_factory=list)   # Deezer related artists, most similar first
    related_at: float = 0.0
    searched_at: float = 0.0                  # last Spotify search for this artist's songs


def artist_key(name: str) -> str:
    return " ".join(name.casefold().split())


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tracks (
                id TEXT PRIMARY KEY, name TEXT, artists TEXT, artist_ids TEXT, album TEXT,
                release_date TEXT, isrc TEXT, duration_ms INTEGER, explicit INTEGER,
                language TEXT, lang_source TEXT, lyrics_checked INTEGER, seen_at REAL);
            CREATE TABLE IF NOT EXISTS artists (
                key TEXT PRIMARY KEY, name TEXT, spotify_id TEXT, deezer_id INTEGER,
                related TEXT, related_at REAL, searched_at REAL);
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        self._db.commit()

    # ------------------------------------------------------------- tracks
    def put_tracks(self, tracks: list[TrackMeta], seen_at: float = 0.0) -> None:
        """Insert or update; a known language is never replaced by "unknown"."""
        with self._lock:
            for t in tracks:
                old = self._db.execute("SELECT language, lang_source, lyrics_checked FROM tracks WHERE id=?",
                                       (t.id,)).fetchone()
                if old and old[0] and not t.language:
                    t = replace(t, language=old[0], lang_source=old[1], lyrics_checked=bool(old[2]))
                elif old and not t.lyrics_checked and old[2]:
                    t = replace(t, lyrics_checked=True)
                self._db.execute(
                    "INSERT OR REPLACE INTO tracks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (t.id, t.name, json.dumps(t.artists), json.dumps(t.artist_ids), t.album, t.release_date,
                     t.isrc, t.duration_ms, int(t.explicit), t.language, t.lang_source, int(t.lyrics_checked),
                     seen_at))
            self._db.commit()

    def tracks(self, ids: list[str] | None = None) -> dict[str, TrackMeta]:
        cols = ("id, name, artists, artist_ids, album, release_date, isrc, duration_ms, explicit, language,"
                " lang_source, lyrics_checked")
        with self._lock:
            if ids is None:
                rows = self._db.execute(f"SELECT {cols} FROM tracks").fetchall()
            else:
                rows = []
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    rows += self._db.execute(f"SELECT {cols} FROM tracks WHERE id IN ({','.join('?' * len(chunk))})",
                                             chunk).fetchall()
        return {r[0]: TrackMeta(r[0], r[1], tuple(json.loads(r[2])), tuple(json.loads(r[3])), r[4], r[5], r[6],
                                r[7], bool(r[8]), r[9] or "", r[10] or "", bool(r[11])) for r in rows}

    # ------------------------------------------------------------- artists
    def artist(self, name: str) -> ArtistInfo:
        with self._lock:
            r = self._db.execute("SELECT name, spotify_id, deezer_id, related, related_at, searched_at"
                                 " FROM artists WHERE key=?", (artist_key(name),)).fetchone()
        if r is None:
            return ArtistInfo(name)
        return ArtistInfo(r[0], r[1] or "", r[2] or 0, json.loads(r[3] or "[]"), r[4] or 0.0, r[5] or 0.0)

    def put_artist(self, a: ArtistInfo) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO artists VALUES (?,?,?,?,?,?,?)",
                             (artist_key(a.name), a.name, a.spotify_id, a.deezer_id, json.dumps(a.related),
                              a.related_at, a.searched_at))
            self._db.commit()

    # ------------------------------------------------------------- misc
    def get(self, key: str, default=None):
        with self._lock:
            r = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return default if r is None else json.loads(r[0])

    def set(self, key: str, value) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))
            self._db.commit()

    def close(self) -> None:
        self._db.close()

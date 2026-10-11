"""Live data sources. All free; none needs an API key except Spotify (the user's own sign-in).

    Spotify      the user's top tracks; song search by artist (also `year:` for new songs)
    Deezer       related artists ("people who like X like Y")
    LRCLIB       lyrics, for the language
    iTunes       30-second previews, for songs ReccoBeats has no features for
    ReccoBeats   audio analysis of a preview -> valence, energy, tempo, ...

Every call has a timeout; hosts are spaced out to stay inside their rate limits.
"""
from __future__ import annotations

import difflib
import logging
import re
import threading
import time
from typing import Callable

import requests

from backend.features import Features, features_from_mapping
from recommender.store import TrackMeta

log = logging.getLogger(__name__)

USER_AGENT = "Moodly/0.2 (https://github.com/msja1729/Moodly)"


class Http:
    """requests with a timeout, a User-Agent and a minimum gap between calls per host."""

    def __init__(self, session: requests.Session | None = None, timeout: float = 15.0,
                 min_gap_s: dict[str, float] | None = None):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.timeout = timeout
        self.min_gap_s = min_gap_s or {}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def _wait(self, host: str) -> None:
        gap = self.min_gap_s.get(host, 0.0)
        with self._lock:
            delay = self._last.get(host, 0.0) + gap - time.monotonic()
            self._last[host] = time.monotonic() + max(delay, 0.0)
        if delay > 0:
            time.sleep(delay)

    def request(self, method: str, url: str, **kw) -> requests.Response:
        self._wait(url.split("/")[2])
        return self.session.request(method, url, timeout=self.timeout, **kw)

    def get_json(self, url: str, params: dict | None = None):
        r = self.request("GET", url, params=params)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


# --------------------------------------------------------------------------- Spotify

def track_from_spotify(item: dict | None) -> TrackMeta | None:
    if not item or item.get("type", "track") != "track" or not item.get("id") or item.get("is_local"):
        return None
    album = item.get("album") or {}
    return TrackMeta(
        id=item["id"], name=item.get("name", "?"),
        artists=tuple(a.get("name", "") for a in item.get("artists", [])),
        artist_ids=tuple(a.get("id") or "" for a in item.get("artists", [])),
        album=album.get("name", ""), release_date=album.get("release_date", "") or "",
        isrc=(item.get("external_ids") or {}).get("isrc", "") or "",
        duration_ms=int(item.get("duration_ms") or 0), explicit=bool(item.get("explicit")))


class SpotifySource:
    """Moodly's Spotify client (its rate-limit handling included), returning TrackMeta."""

    def __init__(self, client):
        self.client = client

    def top_tracks(self, time_range: str, limit: int = 50) -> list[TrackMeta]:
        return [t for t in map(track_from_spotify, self.client.top_tracks_raw(time_range, limit)) if t]

    def track(self, track_id: str) -> TrackMeta | None:
        return track_from_spotify(self.client.track_raw(track_id))

    def search(self, query: str, pages: int = 1) -> list[TrackMeta]:
        out: list[TrackMeta] = []
        for page in range(pages):
            items = self.client.search_tracks_raw(query, limit=10, offset=10 * page)  # 10 = Spotify's maximum
            out += [t for t in map(track_from_spotify, items) if t]
            if len(items) < 10:
                break
        return out


# --------------------------------------------------------------------------- Deezer

def _norm(name: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", name.casefold()).split())


class Deezer:
    URL = "https://api.deezer.com"

    def __init__(self, http: Http):
        self.http = http

    def artist_id(self, name: str) -> int:
        data = self.http.get_json(f"{self.URL}/search/artist", {"q": name, "limit": 5}) or {}
        hits = data.get("data", [])
        exact = [a for a in hits if _norm(a.get("name", "")) == _norm(name)]
        return int((exact or hits or [{"id": 0}])[0]["id"])

    def related(self, deezer_id: int, limit: int = 10) -> list[str]:
        data = self.http.get_json(f"{self.URL}/artist/{deezer_id}/related", {"limit": limit}) or {}
        return [a["name"] for a in data.get("data", []) if a.get("name")]


# --------------------------------------------------------------------------- LRCLIB

class Lrclib:
    URL = "https://lrclib.net/api"

    def __init__(self, http: Http):
        self.http = http

    def lyrics(self, t: TrackMeta) -> str | None:
        artist = t.artists[0] if t.artists else ""
        params = {"track_name": t.name, "artist_name": artist, "album_name": t.album}
        if t.duration_ms:
            params["duration"] = round(t.duration_ms / 1000)
        hit = self.http.get_json(f"{self.URL}/get", params)
        if hit and hit.get("plainLyrics"):
            return hit["plainLyrics"]
        hits = self.http.get_json(f"{self.URL}/search", {"track_name": t.name, "artist_name": artist}) or []
        return next((h["plainLyrics"] for h in hits if h.get("plainLyrics")), None)


# --------------------------------------------------------------------------- previews -> features

def title_core(name: str) -> str:
    """'Maate Vinadhuga (From "Taxiwaala")' -> 'maate vinadhuga'."""
    return _norm(re.split(r"[(\[]| - ", name)[0])


class ITunes:
    URL = "https://itunes.apple.com/search"

    def __init__(self, http: Http, country: str = "US"):
        self.http = http
        self.country = country

    def preview_url(self, t: TrackMeta) -> str | None:
        artist = t.artists[0] if t.artists else ""
        data = self.http.get_json(self.URL, {"term": f"{title_core(t.name)} {artist}", "entity": "song",
                                             "limit": 8, "country": self.country}) or {}
        want_title, want_artists = title_core(t.name), {_norm(a) for a in t.artists}
        best, best_score = None, 0.0
        for r in data.get("results", []):
            if not r.get("previewUrl"):
                continue
            title = difflib.SequenceMatcher(None, want_title, title_core(r.get("trackName", ""))).ratio()
            artist_ok = any(a and a in _norm(r.get("artistName", "")) for a in want_artists)
            dur = r.get("trackTimeMillis") or 0
            dur_ok = not (dur and t.duration_ms) or abs(dur - t.duration_ms) < 20_000
            score = title + 0.5 * artist_ok + 0.2 * dur_ok
            if title >= 0.75 and artist_ok and score > best_score:
                best, best_score = r["previewUrl"], score
        return best


class ReccoBeatsAnalysis:
    URL = "https://api.reccobeats.com/v1/analysis/audio-features"

    def __init__(self, http: Http):
        self.http = http

    def analyze(self, audio: bytes, filename: str = "preview.m4a") -> Features | None:
        r = self.http.request("POST", self.URL, files={"audioFile": (filename, audio, "application/octet-stream")})
        if not r.ok:
            log.debug("audio analysis failed: %s %s", r.status_code, r.text[:200])
            return None
        return features_from_mapping(r.json())


class PreviewFeatureProvider:
    """Feature provider of last resort: iTunes preview -> ReccoBeats analysis.

    Slow (a download and an upload per song), so each call analyses at most
    `budget` songs; the rest are tried on a later call. Results are cached by
    the feature chain like any other provider's.
    """

    name = "preview"

    def __init__(self, meta: Callable[[list[str]], dict[str, TrackMeta]], itunes: ITunes,
                 analysis: ReccoBeatsAnalysis, http: Http, budget: int = 20):
        self.meta = meta
        self.itunes = itunes
        self.analysis = analysis
        self.http = http
        self.budget = budget
        self.tried: set[str] = set()  # no preview / analysis failed: don't retry this run
        self.stop: threading.Event | None = None  # set by the recommender: stop early on shutdown

    def fetch(self, track_ids: list[str]) -> dict[str, Features]:
        out: dict[str, Features] = {}
        metas = self.meta([i for i in track_ids if i not in self.tried])
        order = [i for i in track_ids if i in metas]  # keep the caller's priority order
        for tid in order[: self.budget]:
            if self.stop is not None and self.stop.is_set():
                break
            t = metas[tid]
            self.tried.add(tid)
            try:
                url = self.itunes.preview_url(t)
                if not url:
                    continue
                audio = self.http.request("GET", url).content
                if f := self.analysis.analyze(audio, url.rsplit("/", 1)[-1] or "preview.m4a"):
                    out[tid] = f
            except requests.RequestException as e:
                log.debug("preview analysis of %s failed: %s", t.name, e)
        return out

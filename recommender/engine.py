"""The recommender: a pool of songs that fit the user's taste and languages, with mood features.

refresh() runs in a background thread (startup, then every `refresh_s`):

    1 profile    (every profile_ttl_s) Spotify top tracks + Moodly's recent plays
                 -> favourite artists, top-2 languages
    2 artists    favourites + Deezer related artists, each with a taste weight
    3 songs      Spotify search for a few artists per refresh: their songs, plus
                 `year:` searches for the newest ones
    4 language   title/album marker or script at once; LRCLIB lyrics for up to
                 `lyrics_budget` songs; then the artist's usual language
    5 features   valence/energy/tempo... from the feature cache, ReccoBeats, or
                 (up to `preview_budget` songs) iTunes preview -> ReccoBeats analysis
    6 pool       songs in the top languages, on taste, with features -> TrackTable

The pool is per session and aimed at the current goal (set_goal): it keeps
only songs that move the mood toward the target ("useful" songs), and artists
whose songs have that mood are searched first. Each refresh does a bounded
amount of work; everything is cached (recommender.store) for the session and
cleared when the session ends (clear()). Moodly picks from the pool by mood
(selection.py), adding the taste and freshness scores computed here.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Callable, Mapping

from backend.catalog import CatalogTrack, TrackTable
from backend.features import FeatureChain
from recommender import language as lang_mod
from recommender.profile import TasteProfile, build_profile, freshness, rank_weights
from recommender.store import Store, TrackMeta, artist_key
from recommender.sources import title_core

log = logging.getLogger(__name__)

DAY = 86400.0


@dataclass(frozen=True)
class RecommenderParams:
    enabled: bool = True
    languages: list[str] = field(default_factory=list)  # fixed languages; empty = from listening ("auto")
    max_languages: int = 2
    min_language_share: float = 0.15
    refresh_s: float = 1800.0
    profile_ttl_s: float = 6 * 3600.0
    recent_days: float = 14.0
    favorite_artists: int = 12
    related_from: int = 6           # favourites whose related artists are explored
    related_per_artist: int = 6
    related_ttl_days: float = 7.0
    artists_per_refresh: int = 8    # artists searched on Spotify per refresh
    search_ttl_days: float = 3.0    # an artist's songs are searched again after this
    search_pages: int = 2           # x10 songs per artist (Spotify's page size)
    lyrics_budget: int = 40
    preview_budget: int = 20
    min_taste: float = 0.05
    fresh_days: int = 365
    country: str = "US"             # iTunes store used for previews
    ready_songs: int = 15           # useful songs needed before the session starts adding
    prepare_max_s: float = 600.0    # stop waiting for `ready_songs` after this long
    settle_band: float = 0.15       # near the target, songs this close to it count as useful

    @classmethod
    def from_config(cls, cfg: Mapping) -> "RecommenderParams":
        r = dict(cfg.get("recommender", {}))
        langs = r.pop("languages", "auto")
        out = {k: type(getattr(cls, k))(v) for k, v in r.items() if k in cls.__dataclass_fields__ and k != "languages"}
        return cls(languages=[] if langs in ("auto", None, "") else list(langs), **out)


def normalize_source(source: Mapping | None) -> dict:
    """A valid song source: {"kind": "general"}, {"kind": "liked"} or {"kind": "playlist", "id", "name"}."""
    source = dict(source or {})
    kind = source.get("kind")
    if kind == "liked":
        return {"kind": "liked"}
    if kind == "playlist" and source.get("id"):
        return {"kind": "playlist", "id": str(source["id"]), "name": str(source.get("name") or "your playlist")}
    return {"kind": "general"}


def source_label(source: Mapping | None) -> str:
    source = normalize_source(source)
    return {"general": "For you", "liked": "Liked Songs"}.get(source["kind"]) or source["name"]


class Recommender:
    def __init__(self, spotify, features: FeatureChain, store: Store, params: RecommenderParams = RecommenderParams(),
                 feedback=None, deezer=None, lrclib=None, now: Callable[[], float] = time.time):
        self.spotify = spotify        # recommender.sources.SpotifySource
        self.features = features      # cache -> ReccoBeats -> ... -> preview analysis
        self.store = store
        self.params = params
        self.feedback = feedback      # backend.feedback.FeedbackLog (Moodly's plays and skips)
        self.deezer = deezer
        self.lrclib = lrclib
        self.now = now
        self.stop = threading.Event()
        self.profile: TasteProfile | None = TasteProfile.from_dict(store.get("profile"))
        self._table = TrackTable([])
        self._wanted_features: dict[str, None] = {}  # songs Moodly met without features
        self._lock = threading.Lock()
        self.status = "not started"
        self.target: tuple[float, float] | None = None  # the goal the pool is aimed at (set_goal)
        self.mood: tuple[float, float] | None = None
        self._preview = None
        # Where songs come from: "general" (taste, mood, languages; the default), "liked" (Liked Songs)
        # or "playlist" (one of the user's own playlists, {"kind", "id", "name"}).
        self.source: dict = {"kind": "general"}
        self.loaded = False  # a playlist / Liked Songs source has been read at least once

    @classmethod
    def build(cls, client, feature_providers: list, cache, store_path, params: RecommenderParams, feedback=None):
        """Wire the live sources. `feature_providers`/`cache` are the daemon's (shared cache)."""
        from recommender.sources import (Deezer, Http, ITunes, Lrclib, PreviewFeatureProvider, ReccoBeatsAnalysis,
                                         SpotifySource)

        http = Http(min_gap_s={"itunes.apple.com": 3.1, "lrclib.net": 0.2, "api.deezer.com": 0.12,
                               "api.reccobeats.com": 0.3})
        store = Store(store_path)
        preview = PreviewFeatureProvider(lambda ids: store.tracks(ids), ITunes(http, params.country),
                                         ReccoBeatsAnalysis(http), http, params.preview_budget)
        features = FeatureChain([p for p in feature_providers if p is not None] + [preview], cache)
        rec = cls(SpotifySource(client), features, store, params, feedback, Deezer(http), Lrclib(http))
        preview.stop = rec.stop
        rec._preview = preview
        return rec

    # ------------------------------------------------------------- results
    def table(self) -> TrackTable:
        return self._table

    @property
    def languages(self) -> list[str]:
        return list(self.profile.top_languages) if self.profile else []

    # ------------------------------------------------------------- song source
    @property
    def custom(self) -> bool:
        """True when the pool is one of the user's playlists or Liked Songs (not the general search)."""
        return self.source.get("kind") in ("liked", "playlist")

    def set_source(self, source: Mapping | None) -> bool:
        """Choose where songs come from. Returns True if it changed (the pool must be rebuilt)."""
        new = normalize_source(source)
        if new == self.source:
            return False
        self.source = new
        self.loaded = False
        self._table = TrackTable([])
        return True

    def source_label(self) -> str:
        return source_label(self.source)

    # ------------------------------------------------------------- goal
    def set_goal(self, target, mood) -> None:
        """Aim the pool at moving `mood` toward `target` (mood None = not known yet)."""
        self.target = tuple(target) if target is not None else None
        self.mood = tuple(mood) if mood is not None else None

    def is_useful(self, point, mood=None, target=None) -> bool:
        """A song is useful if it is closer to the target than the mood is (or, near the target, close to it)."""
        target = target if target is not None else self.target
        mood = mood if mood is not None else self.mood
        if target is None or mood is None:
            return True
        reach = max(math.dist(mood, target), self.params.settle_band)
        return math.dist(point, target) < reach

    def useful_count(self, mood=None, target=None) -> int:
        return sum(self.is_useful(t.point, mood, target) for t in self._table.tracks)

    def prune(self, mood=None, target=None) -> int:
        """Drop pool songs that would take the mood away from the target. Returns how many remain.
        A playlist / Liked Songs pool is kept whole: it is all there is."""
        if self.custom:
            return len(self._table)
        self._table = TrackTable([t for t in self._table.tracks if self.is_useful(t.point, mood, target)])
        return len(self._table)

    def clear(self) -> None:
        """End of session: forget the pool, the profile and the cache on disk."""
        self.store.clear()
        self.profile = None
        self._table = TrackTable([])
        with self._lock:
            self._wanted_features = {}
        if self._preview is not None:
            self._preview.tried.clear()
        self.target = self.mood = None
        self.loaded = False
        self.status = "cleared"

    def want_features(self, track_ids) -> None:
        """Songs Moodly saw without mood features (e.g. the user's queue): analyse them next refresh."""
        with self._lock:
            for t in track_ids:
                self._wanted_features[t] = None

    # ------------------------------------------------------------- refresh
    def refresh(self) -> TrackTable:
        started = time.monotonic()
        if self.custom:
            self.status = f"reading {self.source_label()}"
            self._table = self._source_pool()
            self.loaded = True
            self.status = f"{len(self._table)} songs from {self.source_label()}"
            log.info("recommender: %s (%.0fs)", self.status, time.monotonic() - started)
            return self._table
        p = self.params
        if self.profile is None or self.now() - self.profile.built_at > p.profile_ttl_s:
            self.status = "reading your listening"
            self.profile = self._refresh_profile()
        if not self.profile.top_languages:
            self.status = "no languages known yet"
        artists = self._artist_weights()
        self.status = "finding songs"
        self._explore(artists, self._artist_mood_fit(artists))
        self.status = "detecting languages"
        self._resolve_languages(artists)
        self.status = "analysing moods"
        self._table = self._build_pool(artists)
        self.status = f"{len(self._table)} songs in {', '.join(self.language_names()) or '?'}"
        log.info("recommender: %s (%.0fs)", self.status, time.monotonic() - started)
        return self._table

    def language_names(self) -> list[str]:
        return [lang_mod.NAMES.get(c, c) for c in self.languages]

    # 1 ----------------------------------------------------------- profile
    def _refresh_profile(self) -> TasteProfile:
        p = self.params
        short = self.spotify.top_tracks("short_term")
        medium = self.spotify.top_tracks("medium_term")
        weighted = rank_weights(short, 1.0) + rank_weights(medium, 0.5)
        weighted += self._recent_plays()
        metas = list({t.id: t for t, _ in weighted}.values())
        self.store.put_tracks([self._quick_language(t) for t in metas], self.now())
        # The user's own songs get first claim on the lyrics budget: they decide the languages.
        self._lyrics_for([t.id for t in metas], p.lyrics_budget)
        self._artist_language_fallback()
        known = self.store.tracks([t.id for t in metas])
        weighted = [(known.get(t.id, t), w) for t, w in weighted]
        profile = build_profile(weighted, p.max_languages, p.min_language_share, p.languages or None, self.now())
        self.store.set("profile", profile.to_dict())
        log.info("taste profile: languages %s, favourite artists %s", profile.languages,
                 [profile.names[k] for k, _ in profile.favourites(5)])
        return profile

    def _recent_plays(self) -> list[tuple[TrackMeta, float]]:
        if self.feedback is None:
            return []
        since = self.now() - self.params.recent_days * DAY
        plays = [(tid, bool(skipped)) for ts, _, _, _, skipped, tid, _ in self.feedback.events() if tid and ts >= since]
        ids = list(dict.fromkeys(tid for tid, _ in plays))[-60:]
        metas = self.store.tracks(ids)
        for tid in [i for i in ids if i not in metas][:20]:  # one Spotify call each; cached afterwards
            if self.stop.is_set():
                break
            try:
                if t := self.spotify.track(tid):
                    metas[tid] = self._quick_language(t)
                    self.store.put_tracks([metas[tid]], self.now())
            except Exception as e:
                log.debug("track lookup failed: %s", e)
        return [(metas[tid], -0.4 if skipped else 0.6) for tid, skipped in plays if tid in metas]

    # 2 ----------------------------------------------------------- artists
    def _artist_weights(self) -> dict[str, tuple[float, str]]:
        """artist key -> (taste weight 0..1, display name): favourites and their related artists."""
        p, prof = self.params, self.profile
        out: dict[str, tuple[float, str]] = {k: (w, prof.names.get(k, k)) for k, w in prof.favourites(p.favorite_artists)}
        for k, w in prof.favourites(p.related_from):
            if self.stop.is_set():
                break
            related = self._related(prof.names.get(k, k))
            for i, name in enumerate(related[: p.related_per_artist]):
                rw = 0.7 * w * (1 - i / (2 * p.related_per_artist))
                rk = artist_key(name)
                if rw > out.get(rk, (0.0, ""))[0]:
                    out[rk] = (round(rw, 4), name)
        # Artists the user keeps skipping in Moodly count for less.
        for k, rate in self._artist_skip_rates().items():
            if k in out:
                w, name = out[k]
                out[k] = (round(w * (1 - 0.6 * rate), 4), name)
        return out

    def _related(self, name: str) -> list[str]:
        info = self.store.artist(name)
        if self.deezer is None or self.now() - info.related_at < self.params.related_ttl_days * DAY:
            return info.related
        try:
            info.deezer_id = info.deezer_id or self.deezer.artist_id(name)
            info.related = self.deezer.related(info.deezer_id, 12) if info.deezer_id else []
            info.related_at = self.now()
            self.store.put_artist(info)
        except Exception as e:
            log.debug("related artists of %s failed: %s", name, e)
        return info.related

    def _artist_skip_rates(self) -> dict[str, float]:
        if self.feedback is None:
            return {}
        rows = [(tid, bool(s)) for _, _, _, kind, s, tid, _ in self.feedback.events() if tid and kind != "none"]
        metas = self.store.tracks(list({tid for tid, _ in rows}))
        plays, skips = Counter(), Counter()
        for tid, skipped in rows:
            if t := metas.get(tid):
                for a in t.artists[:1]:
                    plays[artist_key(a)] += 1
                    skips[artist_key(a)] += skipped
        return {k: skips[k] / (plays[k] + 2) for k in plays}

    # 3 ----------------------------------------------------------- songs
    def _artist_mood_fit(self, artists: dict[str, tuple[float, str]]) -> dict[str, float]:
        """artist key -> 0..1, how close the artist's known songs are (on average) to the target."""
        cache = getattr(self.features, "cache", None)
        if self.target is None or cache is None:
            return {}
        tracks = self.store.tracks()
        feats = cache.get_many(list(tracks))
        points: dict[str, list] = defaultdict(list)
        for tid, f in feats.items():
            for a in tracks[tid].artists[:1]:
                points[artist_key(a)].append(f.point)
        out = {}
        for k in artists:
            if pts := points.get(k):
                centre = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
                out[k] = max(0.0, 1.0 - math.dist(centre, self.target) / 0.5)
        return out

    def _explore(self, artists: dict[str, tuple[float, str]], mood_fit: dict[str, float] | None = None) -> None:
        """Search a few artists' songs: the best taste first, boosted when their songs have the target mood."""
        p = self.params
        mood_fit = mood_fit or {}
        due = [(w * (0.75 + 0.5 * mood_fit.get(k, 0.5)), name) for k, (w, name) in artists.items()
               if self.now() - self.store.artist(name).searched_at > p.search_ttl_days * DAY]
        year = datetime.fromtimestamp(self.now()).year
        for w, name in sorted(due, reverse=True)[: p.artists_per_refresh]:
            if self.stop.is_set():
                return
            found = self.spotify.search(f'artist:"{name}"', p.search_pages)
            found += self.spotify.search(f'artist:"{name}" year:{year - 1}-{year}', 1)
            mine = [self._quick_language(t) for t in found if artist_key(name) in map(artist_key, t.artists)]
            self.store.put_tracks(list({t.id: t for t in mine}.values()), self.now())
            info = self.store.artist(name)
            info.searched_at = self.now()
            self.store.put_artist(info)

    # 4 ----------------------------------------------------------- language
    @staticmethod
    def _quick_language(t: TrackMeta) -> TrackMeta:
        if t.language:
            return t
        lang, source = lang_mod.detect(t.name, t.album, t.artists_text)
        return t.with_language(lang, source) if lang else t

    def _lyrics_for(self, ids: list[str], budget: int) -> int:
        if self.lrclib is None or budget <= 0:
            return 0
        todo = [t for t in self.store.tracks(ids).values() if not t.language and not t.lyrics_checked][:budget]
        for t in todo:
            if self.stop.is_set():
                break
            try:
                lyrics = self.lrclib.lyrics(t)
            except Exception as e:
                log.debug("lyrics of %s failed: %s", t.name, e)
                continue
            lang, source = lang_mod.detect(t.name, t.album, t.artists_text, lyrics)
            self.store.put_tracks([t.with_language(lang, source, lyrics_checked=True)], self.now())
        return len(todo)

    def _resolve_languages(self, artists: dict[str, tuple[float, str]]) -> None:
        unknown = [t for t in self.store.tracks().values() if not t.language and not t.lyrics_checked]
        weight = {k: w for k, (w, _) in artists.items()}
        unknown.sort(key=lambda t: -max((weight.get(artist_key(a), 0.0) for a in t.artists), default=0.0))
        self._lyrics_for([t.id for t in unknown], self.params.lyrics_budget)
        self._artist_language_fallback()

    def _artist_language_fallback(self, min_known: int = 2, min_share: float = 0.7) -> None:
        """Songs still unknown take their main artist's usual language, if the artist clearly has one."""
        tracks = self.store.tracks()
        langs: dict[str, Counter] = defaultdict(Counter)
        for t in tracks.values():
            if t.language and t.lang_source != "artist" and t.artists:
                langs[artist_key(t.artists[0])][t.language] += 1
        updates = []
        for t in tracks.values():
            if t.language or not t.artists:
                continue
            c = langs.get(artist_key(t.artists[0]))
            if c and sum(c.values()) >= min_known:
                lang, n = c.most_common(1)[0]
                if n >= min_share * sum(c.values()):
                    updates.append(t.with_language(lang, "artist"))
        if updates:
            self.store.put_tracks(updates, self.now())

    # 5-6 --------------------------------------------------------- features and pool
    def _source_pool(self) -> TrackTable:
        """Every song of the chosen playlist / Liked Songs that has mood features. No language or taste
        filter: the user picked these songs. Songs still without features are analysed on later refreshes."""
        if self.source["kind"] == "liked":
            metas = self.spotify.saved_tracks()
        else:
            metas = self.spotify.playlist_tracks(self.source["id"])
        self.store.put_tracks(metas, self.now())  # the preview analysis looks names up here
        with self._lock:
            wanted, self._wanted_features = list(self._wanted_features), {}
        feats = self.features.get_many(wanted + [t.id for t in metas])
        today = date.fromtimestamp(self.now())
        pool, seen = [], set()
        for t in metas:
            f = feats.get(t.id)
            key = (title_core(t.name), artist_key(t.artists[0]) if t.artists else "")
            if f is None or key in seen:
                continue
            seen.add(key)
            pool.append(CatalogTrack(t.id, t.name, t.artists_text, f, t.duration_ms, explicit=t.explicit,
                                     source="playlist", taste=0.5,
                                     fresh=freshness(t.release_date, today, self.params.fresh_days)))
        missing = len({t.id for t in metas}) - len(pool)
        if missing > 0:
            log.info("%s: %d songs have no mood data yet", self.source_label(), missing)
        return TrackTable(pool)

    def _build_pool(self, artists: dict[str, tuple[float, str]]) -> TrackTable:
        p, prof = self.params, self.profile
        langs = set(prof.top_languages)
        own = set(prof.own_tracks)
        weight = {k: w for k, (w, _) in artists.items()}
        today = date.fromtimestamp(self.now())
        top_share = max(prof.languages.values(), default=1.0) or 1.0
        scored: list[tuple[float, TrackMeta]] = []
        for t in self.store.tracks().values():
            if langs and t.language not in langs:
                continue
            taste = max((weight.get(artist_key(a), 0.0) for a in t.artists), default=0.0)
            if t.id in own:
                taste = max(taste, 0.8)
            # The user's first language gets a little more than the second, as in their listening.
            taste *= 0.8 + 0.2 * prof.languages.get(t.language, 0.0) / top_share
            if taste >= p.min_taste:
                scored.append((taste, t))
        scored.sort(key=lambda x: -x[0])
        with self._lock:
            wanted, self._wanted_features = list(self._wanted_features), {}
        # The best-matching songs are first in line for the (slow) preview analysis.
        feats = self.features.get_many(wanted + [t.id for _, t in scored])
        pool, seen = [], set()
        for taste, t in scored:
            f = feats.get(t.id)
            # One copy per song: the same recording is often on several releases (film album, single, ...).
            key = (title_core(t.name), artist_key(t.artists[0]) if t.artists else "")
            if f is None or key in seen or not self.is_useful(f.point):
                continue
            seen.add(key)
            pool.append(CatalogTrack(t.id, t.name, t.artists_text, f, t.duration_ms, explicit=t.explicit,
                                     source="library" if t.id in own else "recommender", language=t.language,
                                     taste=round(taste, 4), fresh=freshness(t.release_date, today, p.fresh_days)))
        return TrackTable(pool)

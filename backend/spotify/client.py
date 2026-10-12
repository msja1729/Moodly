"""Spotify access: the live spotipy client plus a scripted mock for tests/demo.

Both implement the same small interface used by the rest of the daemon:
    current_playback() -> Playback | None
    add_to_queue(uri)
    library_tracks(limit) -> list[Track]
    audio_features(track_ids) -> dict[id, Features]   (may raise FeaturesUnavailable)

and, for the playlist session (Phase 2):
    player_state() -> PlayerState | None          now playing, progress, context, shuffle
    queue() -> list[Item]                         Spotify's upcoming queue (read-only)
    create_playlist(name, description) -> (id, uri)
    playlist_items(id) -> list[Item]
    playlist_add(id, uris, position)              insert at a position
    playlist_remove(id, uris)                     removes every occurrence of each URI
    play_context(context_uri, offset, position_ms)
    set_shuffle(on)
    delete_playlist(id, uri)                      remove it from the user's library

and, for the recommender (raw Spotify track JSON):
    top_tracks_raw(time_range, limit)             the user's top tracks
    search_tracks_raw(query, limit, offset)       track search (Spotify allows 10 per page)
    track_raw(id)                                 one track

Playlist calls use the `/playlists/{id}/items` endpoints and `POST /me/playlists`
(Spotify's February 2026 API; spotipy still calls the removed `/tracks` paths).

All methods are blocking; the daemon calls them through asyncio.to_thread().
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Protocol

import numpy as np

from backend.features import Features, FeaturesUnavailable

log = logging.getLogger(__name__)

SCOPES = " ".join([
    "user-read-currently-playing",
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-library-read",
    "user-top-read",
    "user-read-private",  # account `product`, for the Premium check
    "playlist-read-private",   # the session playlist
    "playlist-modify-private",
])


@dataclass(frozen=True)
class Track:
    id: str
    name: str
    artists: str
    duration_ms: int
    image: str | None = field(default=None, compare=False)  # album cover URL (~300 px)

    @property
    def uri(self) -> str:
        return f"spotify:track:{self.id}"


@dataclass(frozen=True)
class Playback:
    track: Track
    progress_ms: int
    is_playing: bool
    observed_at: float  # clock time of the observation


@dataclass(frozen=True)
class Item:
    """Something playable: a track or a podcast episode."""
    uri: str
    name: str
    artists: str
    duration_ms: int
    kind: str = "track"  # "track" or "episode"

    @property
    def id(self) -> str:
        return self.uri.rsplit(":", 1)[-1]

    @property
    def key(self) -> str:
        return song_key(self.name, self.artists)


def song_key(name: str, artists: str) -> str:
    """A song's identity by title and artists. Spotify relinks tracks per country, so the same song can
    show up with a different id in the queue than in the playlist; the title and artists stay the same."""
    return f"{name}|{artists}".casefold().strip()


@dataclass(frozen=True)
class PlayerState:
    item: Item | None
    progress_ms: int
    is_playing: bool
    context_uri: str | None
    shuffle: bool
    observed_at: float


class PremiumRequired(Exception):
    """Spotify answered 403 PREMIUM_REQUIRED: the account isn't Premium."""


class RateLimited(Exception):
    def __init__(self, retry_after: float):
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


class SpotifyClient(Protocol):
    def current_playback(self) -> Playback | None: ...
    def add_to_queue(self, uri: str) -> None: ...
    def library_tracks(self, limit: int = 200) -> list[Track]: ...
    def audio_features(self, track_ids: list[str]) -> dict[str, Features]: ...


def _item_from(item: dict | None) -> Item | None:
    if not item or not item.get("uri") or item.get("type") not in ("track", "episode"):
        return None  # ad, local file, unavailable
    if item["type"] == "episode":
        who = (item.get("show") or {}).get("name", "")
    else:
        who = ", ".join(a["name"] for a in item.get("artists", []))
    return Item(item["uri"], item.get("name", "?"), who, int(item.get("duration_ms") or 0), item["type"])


def _track_from_item(item: dict) -> Track | None:
    if not item or item.get("type") != "track" or not item.get("id"):
        return None  # podcast episode, ad, or local file
    return Track(
        id=item["id"],
        name=item.get("name", "?"),
        artists=", ".join(a["name"] for a in item.get("artists", [])),
        duration_ms=int(item.get("duration_ms") or 0),
        image=_cover_url(item),
    )


def _cover_url(item: dict, want_px: int = 300) -> str | None:
    """The smallest album image at least `want_px` wide (Spotify lists 640, 300 and 64 px)."""
    images = [im for im in (item.get("album") or {}).get("images") or [] if im.get("url")]
    if not images:
        return None
    big = [im for im in images if (im.get("width") or 0) >= want_px]
    pick = min(big, key=lambda im: im.get("width") or 0) if big else max(images, key=lambda im: im.get("width") or 0)
    return pick["url"]


class LiveSpotifyClient:
    """Live Spotify Web API via spotipy (Premium needed for playback/queue).

    Uses the active account linked in the UI's account window
    (backend.spotify.auth.AccountStore). The token is looked up on every
    request, so switching accounts in the UI takes effect immediately and
    logging out stops all requests. No client secret is involved (PKCE).
    """

    def __init__(self, clock, store=None):
        import spotipy

        from backend.spotify.auth import AccountStore, make_auth

        store = store or AccountStore()
        auth = make_auth(store.active_cache_handler())
        self._spotipy = spotipy
        self.clock = clock
        # Retries are off so a long Retry-After never blocks a worker thread;
        # the daemon does its own backoff when it sees RateLimited.
        self.sp = spotipy.Spotify(auth_manager=auth, requests_timeout=5, retries=0, status_retries=0)

    def _call(self, fn: Callable, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except self._spotipy.SpotifyException as e:
            if e.http_status == 429:
                headers = getattr(e, "headers", None) or {}
                raise RateLimited(float(headers.get("Retry-After", 30))) from e
            if e.http_status == 403 and "PREMIUM_REQUIRED" in f"{getattr(e, 'reason', '')} {e.msg}":
                raise PremiumRequired(str(e)) from e
            raise

    def current_playback(self) -> Playback | None:
        data = self._call(self.sp.current_user_playing_track)
        if not data:
            return None
        track = _track_from_item(data.get("item"))
        if track is None:
            return None
        return Playback(track, int(data.get("progress_ms") or 0), bool(data.get("is_playing")), self.clock.now())

    def add_to_queue(self, uri: str) -> None:
        self._call(self.sp.add_to_queue, uri)

    # -- playlist session -----------------------------------------------------
    def player_state(self) -> PlayerState | None:
        data = self._call(self.sp.current_playback, additional_types="episode")
        if not data:
            return None
        return PlayerState(
            _item_from(data.get("item")), int(data.get("progress_ms") or 0), bool(data.get("is_playing")),
            (data.get("context") or {}).get("uri"), bool(data.get("shuffle_state")), self.clock.now())

    def queue(self) -> list[Item]:
        data = self._call(self.sp.queue) or {}
        return [it for it in (_item_from(x) for x in data.get("queue", [])) if it]

    def create_playlist(self, name: str, description: str = "") -> tuple[str, str]:
        data = self._call(self.sp._post, "me/playlists",
                          payload={"name": name, "public": False, "description": description})
        return data["id"], data["uri"]

    def playlist_items(self, playlist_id: str) -> list[Item]:
        out: list[Item] = []
        offset = 0
        while True:
            page = self._call(self.sp._get, f"playlists/{playlist_id}/items", limit=50, offset=offset,
                              additional_types="track,episode")
            rows = page.get("items", [])
            for row in rows:
                # The 2026 API nests each entry under "item" (formerly "track").
                if it := _item_from(row.get("item") or row.get("track")):
                    out.append(it)
            if not page.get("next") or not rows:
                return out
            offset += len(rows)

    def playlist_add(self, playlist_id: str, uris: list[str], position: int | None = None) -> None:
        for i in range(0, len(uris), 100):
            payload = {"uris": uris[i:i + 100]}
            if position is not None:
                payload["position"] = position + i
            self._call(self.sp._post, f"playlists/{playlist_id}/items", payload=payload)

    def playlist_remove(self, playlist_id: str, uris: list[str]) -> None:
        for i in range(0, len(uris), 100):
            self._call(self.sp._delete, f"playlists/{playlist_id}/items",
                       payload={"items": [{"uri": u} for u in uris[i:i + 100]]})

    def play_context(self, context_uri: str, offset: int = 0, position_ms: int = 0) -> None:
        self._call(self.sp.start_playback, context_uri=context_uri, offset={"position": offset},
                   position_ms=max(0, int(position_ms)))

    def set_shuffle(self, on: bool) -> None:
        self._call(self.sp.shuffle, on)

    # -- player buttons in the sidebar ------------------------------------------
    def pause(self) -> None:
        self._call(self.sp.pause_playback)

    def resume(self) -> None:
        self._call(self.sp.start_playback)

    def next_track(self) -> None:
        self._call(self.sp.next_track)

    def delete_playlist(self, playlist_id: str, uri: str) -> None:
        """Spotify has no hard delete: unfollowing an owned playlist deletes it for the user.

        Tries the classic unfollow endpoint first (works with our playlist scopes;
        still served to apps created before Spotify's Feb 2026 change), then the
        2026 replacement `DELETE /me/library`, which needs `user-library-modify`.
        """
        try:
            self._call(self.sp.current_user_unfollow_playlist, playlist_id)
        except self._spotipy.SpotifyException as e:
            if e.http_status not in (400, 403, 404, 410):
                raise
            self._call(self.sp._delete, "me/library", uris=uri)

    def library_tracks(self, limit: int = 200) -> list[Track]:
        tracks: dict[str, Track] = {}
        for time_range in ("short_term", "medium_term"):
            res = self._call(self.sp.current_user_top_tracks, limit=50, time_range=time_range)
            for item in res.get("items", []):
                if t := _track_from_item(item):
                    tracks[t.id] = t
        offset = 0
        while len(tracks) < limit:
            res = self._call(self.sp.current_user_saved_tracks, limit=50, offset=offset)
            items = res.get("items", [])
            for it in items:
                if t := _track_from_item(it.get("track")):
                    tracks[t.id] = t
            if not res.get("next"):
                break
            offset += 50
        return list(tracks.values())[:limit]

    # -- recommender ------------------------------------------------------------
    def top_tracks_raw(self, time_range: str = "short_term", limit: int = 50) -> list[dict]:
        return (self._call(self.sp.current_user_top_tracks, limit=limit, time_range=time_range) or {}).get("items", [])

    def search_tracks_raw(self, query: str, limit: int = 10, offset: int = 0) -> list[dict]:
        res = self._call(self.sp.search, q=query, type="track", limit=limit, offset=offset) or {}
        return (res.get("tracks") or {}).get("items", [])

    def track_raw(self, track_id: str) -> dict | None:
        return self._call(self.sp.track, track_id)

    # -- song sources (the user's own playlists, Liked Songs) ------------------
    def my_playlists_raw(self) -> list[dict]:
        """The user's own and collaborative playlists. Spotify doesn't let this app read the songs of
        playlists the user only follows (403), so those are left out."""
        me = (self._call(self.sp.current_user) or {}).get("id")
        out, offset = [], 0
        while True:
            page = self._call(self.sp._get, "me/playlists", limit=50, offset=offset) or {}
            rows = [p for p in page.get("items", []) if p]
            out += [p for p in rows if (p.get("owner") or {}).get("id") == me or p.get("collaborative")]
            if not page.get("next") or not rows:
                return out
            offset += len(rows)

    def playlist_tracks_raw(self, playlist_id: str, limit: int = 500) -> list[dict]:
        out, offset = [], 0
        while len(out) < limit:
            page = self._call(self.sp._get, f"playlists/{playlist_id}/items", limit=50, offset=offset,
                              additional_types="track") or {}
            rows = page.get("items", [])
            out += [it for it in ((r.get("item") or r.get("track")) for r in rows) if it]
            if not page.get("next") or not rows:
                break
            offset += len(rows)
        return out[:limit]

    def saved_tracks_raw(self, limit: int = 500) -> list[dict]:
        out, offset = [], 0
        while len(out) < limit:
            page = self._call(self.sp.current_user_saved_tracks, limit=50, offset=offset) or {}
            rows = page.get("items", [])
            out += [r["track"] for r in rows if r.get("track")]
            if not page.get("next") or not rows:
                break
            offset += len(rows)
        return out[:limit]

    def saved_tracks_count(self) -> int:
        return int((self._call(self.sp.current_user_saved_tracks, limit=1) or {}).get("total") or 0)

    def audio_features(self, track_ids: list[str]) -> dict[str, Features]:
        out: dict[str, Features] = {}
        try:
            for i in range(0, len(track_ids), 100):
                for f in self._call(self.sp.audio_features, track_ids[i:i + 100]) or []:
                    if f:
                        out[f["id"]] = Features(f["valence"], f["energy"], f["acousticness"])
        except self._spotipy.SpotifyException as e:
            if e.http_status in (403, 404):
                raise FeaturesUnavailable("Spotify audio-features is not available to this app") from e
            raise
        return out


# --------------------------------------------------------------------------- mock

@dataclass
class _Play:
    track: Track
    listen_s: float  # how long the user listens before the track ends/skips


@dataclass
class MockSpotifyClient:
    """A scripted listening session that runs on an injectable clock.

    The user plays `script` (pairs of (track_id, fraction listened)) and then
    keeps choosing random tracks near the last one. Tracks pushed with
    add_to_queue() play next; each is skipped with probability `queue_skip_p`.
    `rate_limit_every` / `error_every` inject failures on every k-th call and
    `gap_after` inserts a stretch of silence (no playback) after that many plays.

    Playlist session support: playlists can be created and edited, and
    play_context() plays one from a given entry and millisecond. Like
    Spotify, it follows edits to the playing playlist, plays songs added with
    add_to_queue() first, and stops when the playlist runs out. user_pause(),
    user_resume() and user_switch_context() simulate what the user does in the
    Spotify app.
    """

    catalog: dict[str, tuple[Track, Features]]
    clock: object
    script: list[tuple[str, float]] = field(default_factory=list)
    seed: int = 0
    queue_skip_p: float = 0.2
    playlist_skip_p: float = 0.1
    features_available: bool = True
    rate_limit_every: int = 0
    error_every: int = 0
    gap_after: int = 0
    gap_s: float = 90.0
    context_report_lag_s: float = 0.0  # like Spotify: the new source shows up in player_state() late
    follows_playlist_edits: bool = True  # False = like the real Spotify app: plays a snapshot taken at play_context()

    def __post_init__(self):
        self.rng = np.random.default_rng(self.seed)
        self.manual_queue: list[str] = []  # songs added with add_to_queue(), played first
        self.queued_log: list[str] = []
        self.plays: list[tuple[str, bool]] = []  # (track_id, skipped) for finished plays
        self.calls = 0
        self.playlists: dict[str, list[list[str]]] = {}  # id -> [[entry uid, uri], ...]
        self.user_playlists: dict[str, tuple[str, list[str]]] = {}  # song sources: id -> (name, track ids)
        self.liked: list[str] = []
        self.deleted_playlists: list[str] = []
        self.context: str | None = None  # playing playlist URI
        self._reported_context: str | None = None
        self._context_changed_at = -1e18
        self.context_starts: list[tuple[str, int, int]] = []  # (uri, offset, position_ms)
        self.shuffle = True
        self.stopped = False
        self._ctx_uid: str | None = None  # entry of the playlist last played from the context
        self._ctx_snapshot: list[list[str]] | None = None
        self._ctx_pos = 0
        self._uid = 0
        self._paused_at: float | None = None
        self._script = list(self.script)
        self._current: _Play | None = None
        self._start_next(self.clock.now())

    # -- simulation -----------------------------------------------------------
    def _new_uid(self) -> str:
        self._uid += 1
        return f"e{self._uid}"

    def _context_entries(self) -> list[list[str]] | None:
        if not self.context:
            return None
        live = self.playlists.get(self.context.rsplit(":", 1)[-1])
        if live is not None and not self.follows_playlist_edits:
            return self._ctx_snapshot
        return live

    def _context_index(self) -> int:
        entries = self._context_entries() or []
        for i, (uid, _) in enumerate(entries):
            if uid == self._ctx_uid:
                return i
        return self._ctx_pos - 1  # the entry was removed; carry on from where it was

    def _pick_next(self) -> tuple[str, float] | None:
        if self.manual_queue:
            tid = self.manual_queue.pop(0)
            return tid, (0.3 if self.rng.random() < self.queue_skip_p else 1.0)
        entries = self._context_entries()
        if self.context:
            if entries is None:  # the playlist was deleted: Spotify falls back to other music
                self.context = None
            else:
                nxt = self._context_index() + 1
                if nxt >= len(entries):
                    return None  # the playlist ran out: playback stops
                self._ctx_uid, self._ctx_pos = entries[nxt][0], nxt
                fraction = 0.3 if self.rng.random() < self.playlist_skip_p else 1.0
                return entries[nxt][1].rsplit(":", 1)[-1], fraction
        if self._script:
            return self._script.pop(0)
        # Free listening: a track near the current one in mood space.
        here = self.catalog[self._current.track.id][1] if self._current else Features(0.5, 0.5, 0.5)
        ids = list(self.catalog)
        pts = np.array([[f.valence, f.energy] for _, f in self.catalog.values()])
        d = np.hypot(pts[:, 0] - here.valence, pts[:, 1] - here.energy)
        near = [ids[i] for i in np.argsort(d)[1:8]]
        return near[int(self.rng.integers(len(near)))], (0.4 if self.rng.random() < 0.15 else 1.0)

    def _start_next(self, start_at: float) -> None:
        picked = self._pick_next()
        if picked is None:
            self._current, self.stopped = None, True
            return
        tid, fraction = picked
        track = self.catalog[tid][0]
        self._current = _Play(track, track.duration_ms / 1000 * fraction)
        self._started = start_at

    def _advance(self) -> None:
        if self._paused_at is not None:
            return
        now = self.clock.now()
        while self._current and now >= self._started + self._current.listen_s:
            end = self._started + self._current.listen_s
            skipped = self._current.listen_s < self._current.track.duration_ms / 1000 * 0.999
            self.plays.append((self._current.track.id, skipped))
            gap = self.gap_s if self.gap_after and len(self.plays) == self.gap_after else 0.0
            self._start_next(end + gap)

    def _tick(self) -> None:
        self.calls += 1
        if self.rate_limit_every and self.calls % self.rate_limit_every == 0:
            raise RateLimited(5.0)
        if self.error_every and self.calls % self.error_every == 0:
            raise ConnectionError("simulated network failure")

    def _progress_s(self) -> float:
        now = self.clock.now() if self._paused_at is None else self._paused_at
        return now - self._started

    # -- what the user does in the Spotify app ---------------------------------
    def user_pause(self) -> None:
        self._advance()
        if self._paused_at is None:
            self._paused_at = self.clock.now()

    def user_resume(self) -> None:
        if self._paused_at is not None:
            self._started += self.clock.now() - self._paused_at
            self._paused_at = None

    # The sidebar's player buttons.
    def pause(self) -> None:
        self.user_pause()

    def resume(self) -> None:
        self.user_resume()

    def next_track(self) -> None:
        """Skip: the current song ends now."""
        self._advance()
        if self._current is not None and self._paused_at is None:
            self._current = _Play(self._current.track, max(0.0, self._progress_s()))
            self._advance()

    def user_switch_context(self, track_ids: list[str]) -> None:
        """The user starts other music (e.g. an album) in Spotify."""
        self._advance()
        self.context, self._ctx_uid = None, None
        self.manual_queue.clear()
        self._script = [(t, 1.0) for t in track_ids]
        self._start_next(self.clock.now())

    # -- interface ------------------------------------------------------------
    def current_playback(self) -> Playback | None:
        self._tick()
        self._advance()
        now = self.clock.now()
        if self._current is None or now < self._started:  # stopped, or inside a silence gap
            return None
        progress = int(self._progress_s() * 1000)
        return Playback(self._current.track, progress, self._paused_at is None, now)

    def add_to_queue(self, uri: str) -> None:
        self._tick()
        tid = uri.rsplit(":", 1)[-1]
        if tid not in self.catalog:
            raise ValueError(f"unknown track {uri}")
        self.manual_queue.append(tid)
        self.queued_log.append(tid)

    def library_tracks(self, limit: int = 200) -> list[Track]:
        return [t for t, _ in list(self.catalog.values())[:limit]]

    # Song sources: tests fill `user_playlists` ({id: (name, [track ids])}) and `liked`.
    def _raw(self, tid: str) -> dict:
        t = self.catalog[tid][0]
        return {"type": "track", "id": t.id, "name": t.name, "artists": [{"name": t.artists, "id": ""}],
                "duration_ms": t.duration_ms, "album": {"name": "", "release_date": ""}}

    def my_playlists_raw(self) -> list[dict]:
        return [{"id": pid, "name": name, "items": {"total": len(ids)}} for pid, (name, ids) in self.user_playlists.items()]

    def playlist_tracks_raw(self, playlist_id: str, limit: int = 500) -> list[dict]:
        return [self._raw(t) for t in self.user_playlists.get(playlist_id, ("", []))[1][:limit]]

    def saved_tracks_raw(self, limit: int = 500) -> list[dict]:
        return [self._raw(t) for t in self.liked[:limit]]

    def saved_tracks_count(self) -> int:
        return len(self.liked)

    # The mock has no search or top-track history; the recommender is off in mock mode.
    def top_tracks_raw(self, time_range: str = "short_term", limit: int = 50) -> list[dict]:
        return []

    def search_tracks_raw(self, query: str, limit: int = 10, offset: int = 0) -> list[dict]:
        return []

    def track_raw(self, track_id: str) -> dict | None:
        return None

    def audio_features(self, track_ids: list[str]) -> dict[str, Features]:
        if not self.features_available:
            raise FeaturesUnavailable("mock: audio-features disabled")
        return {i: self.catalog[i][1] for i in track_ids if i in self.catalog}

    # -- playlist session -----------------------------------------------------
    def _item(self, uri: str) -> Item:
        t = self.catalog[uri.rsplit(":", 1)[-1]][0]
        return Item(uri, t.name, t.artists, t.duration_ms)

    def player_state(self) -> PlayerState | None:
        self._tick()
        self._advance()
        now = self.clock.now()
        if self._current is None or now < self._started:
            return None
        lagging = now - self._context_changed_at < self.context_report_lag_s
        return PlayerState(self._item(self._current.track.uri), int(self._progress_s() * 1000),
                           self._paused_at is None, self._reported_context if lagging else self.context,
                           self.shuffle, now)

    def queue_snapshot(self) -> list[str]:
        """Upcoming track ids as Spotify would play them (for tests)."""
        upcoming = list(self.manual_queue)
        entries = self._context_entries()
        if self.context and entries is not None:
            upcoming += [uri.rsplit(":", 1)[-1] for _, uri in entries[self._context_index() + 1:]]
        else:
            upcoming += [t for t, _ in self._script]
        return upcoming

    def queue(self) -> list[Item]:
        self._tick()
        self._advance()
        return [self._item(f"spotify:track:{t}") for t in self.queue_snapshot()[:20]]

    def create_playlist(self, name: str, description: str = "") -> tuple[str, str]:
        self._tick()
        pid = f"mockpl{len(self.playlists) + len(self.deleted_playlists) + 1}"
        self.playlists[pid] = []
        return pid, f"spotify:playlist:{pid}"

    def playlist_items(self, playlist_id: str) -> list[Item]:
        self._tick()
        return [self._item(uri) for _, uri in self.playlists[playlist_id]]

    def playlist_add(self, playlist_id: str, uris: list[str], position: int | None = None) -> None:
        self._tick()
        self._advance()
        for u in uris:
            if u.rsplit(":", 1)[-1] not in self.catalog:
                raise ValueError(f"unknown track {u}")
        entries = self.playlists[playlist_id]
        new = [[self._new_uid(), u] for u in uris]
        at = len(entries) if position is None else min(max(position, 0), len(entries))
        entries[at:at] = new

    def playlist_remove(self, playlist_id: str, uris: list[str]) -> None:
        self._tick()
        self._advance()
        drop = set(uris)
        entries = self.playlists[playlist_id]
        if self.context and self.context.endswith(playlist_id):
            # Keep the "where am I" pointer valid when earlier entries disappear.
            here = self._context_index()
            self._ctx_pos = here + 1 - sum(1 for _, u in entries[:here] if u in drop)
        entries[:] = [e for e in entries if e[1] not in drop]

    def play_context(self, context_uri: str, offset: int = 0, position_ms: int = 0) -> None:
        self._tick()
        entries = self.playlists[context_uri.rsplit(":", 1)[-1]]
        uid, uri = entries[offset]
        track = self.catalog[uri.rsplit(":", 1)[-1]][0]
        if self.context != context_uri:
            self._reported_context, self._context_changed_at = self.context, self.clock.now()
        self._ctx_snapshot = [list(e) for e in entries]
        self.context, self._ctx_uid, self._ctx_pos = context_uri, uid, offset + 1
        self._current = _Play(track, track.duration_ms / 1000)
        self._started = self.clock.now() - position_ms / 1000
        self._paused_at, self.stopped = None, False
        self.context_starts.append((context_uri, offset, position_ms))

    def set_shuffle(self, on: bool) -> None:
        self._tick()
        self.shuffle = on

    def delete_playlist(self, playlist_id: str, uri: str) -> None:
        self._tick()
        self.playlists.pop(playlist_id, None)
        self.deleted_playlists.append(playlist_id)


def demo_catalog(n_side: int = 12, seed: int = 7) -> dict[str, tuple[Track, Features]]:
    """A synthetic library spread across the whole mood space.

    Tempo, key, mode, loudness and danceability come from a second random
    stream, so the mood-space positions are the same as before they existed.
    """
    rng = np.random.default_rng(seed)
    extra = np.random.default_rng(seed + 1000)
    catalog: dict[str, tuple[Track, Features]] = {}
    k = 0
    for i in range(n_side):
        for j in range(n_side):
            v = min(max((i + rng.random()) / n_side, 0.0), 1.0)
            e = min(max((j + rng.random()) / n_side, 0.0), 1.0)
            a = float(np.clip(1.0 - e + rng.normal(0, 0.1), 0, 1))
            tid = f"demo{k:04d}"
            catalog[tid] = (
                Track(tid, f"Demo track {k:03d}", f"Mock Artist {k % 24}", int(rng.integers(150, 260)) * 1000),
                Features(round(v, 3), round(e, 3), round(a, 3),
                         tempo=round(float(np.clip(70 + 100 * e + extra.normal(0, 12), 55, 200)), 1),
                         key=int(extra.integers(0, 12)), mode=int(extra.random() < 0.3 + 0.5 * v),
                         loudness=round(float(-22 + 17 * e + extra.normal(0, 2)), 1),
                         danceability=round(float(np.clip(0.3 + 0.5 * v + extra.normal(0, 0.1), 0, 1)), 3)),
            )
            k += 1
    return catalog

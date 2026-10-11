"""The user's taste: favourite artists and the languages they listen to.

Built from weighted listening:
    Spotify top tracks, last ~4 weeks     weight 1.0 .. 0.5 (by rank)
    Spotify top tracks, last ~6 months    weight 0.5 .. 0.25
    songs played in Moodly, last 14 days  +0.6 if finished, -0.4 if skipped

Artists: summed weight (first artist full, the others 0.6), scaled so the
favourite is 1. Languages: share of the positive weight among songs whose
language is known; the top one always, a second if it has at least
`min_share` of the listening.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date

from recommender.store import TrackMeta, artist_key


@dataclass
class TasteProfile:
    artists: dict[str, float] = field(default_factory=dict)    # artist key -> 0..1
    names: dict[str, str] = field(default_factory=dict)        # artist key -> display name
    languages: dict[str, float] = field(default_factory=dict)  # language -> share
    top_languages: list[str] = field(default_factory=list)
    own_tracks: list[str] = field(default_factory=list)        # the user's top/recent track ids
    built_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "TasteProfile | None":
        return cls(**d) if d else None

    def favourites(self, n: int) -> list[tuple[str, float]]:
        return sorted(self.artists.items(), key=lambda kv: -kv[1])[:n]


def rank_weights(tracks: list[TrackMeta], top: float) -> list[tuple[TrackMeta, float]]:
    n = max(len(tracks), 1)
    return [(t, top * (1.0 - 0.5 * i / n)) for i, t in enumerate(tracks)]


def build_profile(weighted: list[tuple[TrackMeta, float]], max_languages: int = 2, min_share: float = 0.15,
                  fixed_languages: list[str] | None = None, now: float = 0.0) -> TasteProfile:
    artist_w: dict[str, float] = defaultdict(float)
    names: dict[str, str] = {}
    lang_w: dict[str, float] = defaultdict(float)
    own: dict[str, None] = {}
    for t, w in weighted:
        for i, a in enumerate(t.artists[:4]):
            k = artist_key(a)
            artist_w[k] += w * (1.0 if i == 0 else 0.6)
            names.setdefault(k, a)
        if w > 0:
            own[t.id] = None
            if t.language:
                lang_w[t.language] += w
    top = max((v for v in artist_w.values() if v > 0), default=1.0)
    artists = {k: round(v / top, 4) for k, v in artist_w.items() if v > 0}
    total = sum(lang_w.values()) or 1.0
    languages = {k: round(v / total, 4) for k, v in sorted(lang_w.items(), key=lambda kv: -kv[1])}
    if fixed_languages:
        top_languages = list(fixed_languages)
    else:
        ranked = list(languages.items())
        top_languages = [lang for i, (lang, share) in enumerate(ranked[:max_languages]) if i == 0 or share >= min_share]
    return TasteProfile(artists, {k: names[k] for k in artists}, languages, top_languages, list(own), now)


def freshness(release_date: str, today: date, fresh_days: int = 365) -> float:
    """1 for a song out today, falling to 0 at `fresh_days` old (and for unknown dates)."""
    if not release_date:
        return 0.0
    parts = [int(p) for p in release_date.split("-")[:3] if p.isdigit()]
    try:
        released = date(parts[0], parts[1] if len(parts) > 1 else 7, parts[2] if len(parts) > 2 else 1)
    except (ValueError, IndexError):
        return 0.0
    age = (today - released).days
    return round(max(0.0, 1.0 - max(age, 0) / fresh_days), 4)

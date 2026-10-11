from datetime import datetime

from backend.features import FeatureChain, Features
from recommender.engine import Recommender, RecommenderParams
from recommender.profile import build_profile, freshness, rank_weights
from recommender.store import Store, TrackMeta

NOW = datetime(2026, 10, 11).timestamp()
TELUGU_LYRICS = "నేను నిన్ను చూసి నవ్వు నువ్వు నా మనసు లో ఉంది ప్రేమ"
HINDI_LYRICS = "तुम ही हो अब तुम ही हो ज़िन्दगी अब तुम ही हो"
TAMIL_LYRICS = "கண்ணே கலைமானே கன்னி மயிலென கண்டேன் உனை நானே"


def T(i, name, artists, album="", lang="", release="2020-01-01"):
    return TrackMeta(f"t{i}", name, tuple(artists), album=album, release_date=release, language=lang,
                     lang_source="script" if lang else "")


class FakeSpotify:
    def __init__(self, short, search=None):
        self.short, self._search = list(short), search or {}
        self.queries = []

    def top_tracks(self, time_range, limit=50):
        return self.short if time_range == "short_term" else []

    def search(self, query, pages=1):
        self.queries.append(query)
        return [t for q, ts in self._search.items() if q in query for t in ts]

    def track(self, tid):
        return None


class FakeDeezer:
    def __init__(self, related):
        self.rel = related

    def artist_id(self, name):
        return 1 if name in self.rel else 0

    def related(self, deezer_id, limit=10):
        return next(iter(self.rel.values()))


class FakeLyrics:
    def __init__(self, by_name):
        self.by_name = by_name
        self.calls = []

    def lyrics(self, t):
        self.calls.append(t.name)
        return self.by_name.get(t.name)


class FixedFeatures:
    name = "fixed"

    def __init__(self, missing=()):
        self.missing = set(missing)

    def fetch(self, ids):
        return {i: Features(0.5, 0.5, 0.2) for i in ids if i not in self.missing}


class FakeFeedback:
    def __init__(self, rows):
        self.rows = rows

    def events(self):
        return self.rows


def make(short, search=None, lyrics=None, related=None, missing=(), params=None, feedback=None):
    spotify = FakeSpotify(short, search)
    rec = Recommender(spotify, FeatureChain([FixedFeatures(missing)]), Store(":memory:"),
                      params or RecommenderParams(lyrics_budget=50), feedback,
                      FakeDeezer(related or {}), FakeLyrics(lyrics or {}), now=lambda: NOW)
    return rec, spotify


def test_profile_top_two_languages_and_favourites():
    short = [T(1, "a", ["Thaman S"], lang="te"), T(2, "b", ["Thaman S"], lang="te"),
             T(3, "c", ["Arijit Singh"], lang="hi"), T(4, "d", ["Justin Bieber"], lang="en")]
    p = build_profile(rank_weights(short, 1.0), max_languages=2, min_share=0.15)
    assert p.top_languages == ["te", "hi"]
    assert p.favourites(1)[0][0] == "thaman s"
    assert build_profile(rank_weights(short, 1.0), fixed_languages=["ta"]).top_languages == ["ta"]


def test_second_language_needs_enough_share():
    short = [T(i, f"s{i}", ["X"], lang="te") for i in range(9)] + [T(9, "e", ["Y"], lang="en")]
    assert build_profile(rank_weights(short, 1.0), min_share=0.15).top_languages == ["te"]


def test_freshness():
    today = datetime(2026, 10, 11).date()
    assert freshness("2026-10-11", today) == 1.0
    assert 0.4 < freshness("2026-04-01", today) < 0.6
    assert freshness("2019", today) == 0.0 and freshness("", today) == 0.0


def test_refresh_builds_a_pool_in_the_users_languages():
    short = [T(1, "Yetu Pone", ["Sid Sriram"]), T(2, "Channa Mereya", ["Arijit Singh"]),
             T(3, "Toliprema", ["Sid Sriram"])]
    search = {
        '"Sid Sriram"': [T(10, "Inkem Inkem", ["Sid Sriram"]), T(11, "Kanne Kalai", ["Sid Sriram"])],
        '"Arijit Singh"': [T(20, "Kesariya", ["Arijit Singh"], release="2026-06-01")],
        '"Karthik"': [T(30, "Song by related", ["Karthik"]), T(31, "Not by the artist", ["Other"])],
    }
    lyrics = {"Yetu Pone": TELUGU_LYRICS, "Toliprema": TELUGU_LYRICS, "Channa Mereya": HINDI_LYRICS,
              "Inkem Inkem": TELUGU_LYRICS, "Kesariya": HINDI_LYRICS, "Song by related": TELUGU_LYRICS,
              "Kanne Kalai": TAMIL_LYRICS}
    rec, spotify = make(short, search, lyrics, related={"Sid Sriram": ["Karthik"]})
    pool = rec.refresh()

    assert rec.languages == ["te", "hi"]
    names = {t.name for t in pool.tracks}
    assert {"Yetu Pone", "Channa Mereya", "Inkem Inkem", "Kesariya", "Song by related"} <= names
    assert "Kanne Kalai" not in names          # Tamil: not one of the user's languages
    assert "Not by the artist" not in names    # search noise
    by = {t.name: t for t in pool.tracks}
    assert by["Yetu Pone"].source == "library" and by["Inkem Inkem"].source == "recommender"
    assert by["Song by related"].taste < by["Inkem Inkem"].taste  # related artist < favourite
    assert by["Kesariya"].fresh > 0.6 and by["Inkem Inkem"].fresh == 0.0
    assert any("year:2025-2026" in q for q in spotify.queries)    # newest songs searched too


def test_songs_without_features_are_left_out_and_duplicates_merged():
    short = [T(1, "A", ["Thaman S"], lang="te"), T(2, "B", ["Thaman S"], lang="te")]
    search = {'"Thaman S"': [T(3, "Naari Naari", ["Thaman S"], lang="te"),
                             T(4, 'Naari Naari (From "Aagadu")', ["Thaman S"], lang="te"),
                             T(5, "No features", ["Thaman S"], lang="te")]}
    rec, _ = make(short, search, missing={"t5"})
    names = [t.name for t in rec.refresh().tracks]
    assert sum(n.startswith("Naari Naari") for n in names) == 1
    assert "No features" not in names


def test_lyrics_budget_and_no_repeat_lookups():
    short = [T(i, f"s{i}", ["X"]) for i in range(10)]
    rec, _ = make(short, lyrics={f"s{i}": TELUGU_LYRICS for i in range(10)},
                  params=RecommenderParams(lyrics_budget=4, profile_ttl_s=0))
    rec.refresh()
    assert len(rec.lrclib.calls) <= 8           # 4 for the profile + 4 for the rest
    rec.refresh()
    assert len(set(rec.lrclib.calls)) == len(rec.lrclib.calls)  # a song's lyrics are fetched once


def test_artist_language_fallback():
    short = [T(1, "a", ["Thaman S"]), T(2, "b", ["Thaman S"]), T(3, "c", ["Thaman S"])]
    rec, _ = make(short, lyrics={"a": TELUGU_LYRICS, "b": TELUGU_LYRICS})
    rec.refresh()
    t3 = rec.store.tracks(["t3"])["t3"]
    assert (t3.language, t3.lang_source) == ("te", "artist")


def test_recent_skips_lower_an_artist():
    short = [T(1, "a", ["Fav"], lang="te"), T(2, "b", ["Skipped"], lang="te")]
    rows = [(NOW - 3600, 0.5, 0.5, "playlist_add", 1, "t2", None)] * 5
    rec, _ = make(short, feedback=FakeFeedback(rows))
    rec.profile = rec._refresh_profile()
    w = rec._artist_weights()
    assert w["skipped"][0] < 0.5 * w["fav"][0]


def test_stop_ends_a_refresh_early():
    short = [T(i, f"s{i}", [f"A{i}"], lang="te") for i in range(5)]
    rec, spotify = make(short, search={f'"A{i}"': [] for i in range(5)})
    rec.stop.set()
    rec.refresh()
    assert spotify.queries == []

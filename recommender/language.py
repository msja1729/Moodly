"""Which language a song is sung in, from what we can see without listening.

Spotify gives no language (and, since February 2026, no artist genres), so it
is inferred from, in order of trust:

    marker   "Kgf Chapter 2 [TELUGU]", "The Karma Theme - Telugu (From ...)"
             (dubbed film songs say so; beats everything else)
    script   the title or album written in a native script (Telugu, Devanagari, ...)
    lyrics   the script of the lyrics (LRCLIB lyrics are mostly in native script);
             lyrics in Latin letters are matched against common words, which also
             catches romanized Hindi/Telugu ("tenu sang rakhna", "nenu ninnu")

Codes are ISO 639-1 ("te", "hi", "en", ...). Devanagari is taken as Hindi.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter

NAMES = {
    "te": "Telugu", "hi": "Hindi", "ta": "Tamil", "kn": "Kannada", "ml": "Malayalam", "bn": "Bengali",
    "pa": "Punjabi", "mr": "Marathi", "gu": "Gujarati", "or": "Odia", "en": "English", "es": "Spanish",
    "pt": "Portuguese", "fr": "French", "de": "German", "it": "Italian", "ko": "Korean", "ja": "Japanese",
    "zh": "Chinese", "ar": "Arabic", "ru": "Russian", "el": "Greek", "he": "Hebrew", "th": "Thai",
}

# First word of the Unicode character name -> language.
SCRIPT_LANG = {
    "TELUGU": "te", "DEVANAGARI": "hi", "TAMIL": "ta", "KANNADA": "kn", "MALAYALAM": "ml",
    "BENGALI": "bn", "GURMUKHI": "pa", "GUJARATI": "gu", "ORIYA": "or", "HANGUL": "ko",
    "HIRAGANA": "ja", "KATAKANA": "ja", "CJK": "zh", "ARABIC": "ar", "CYRILLIC": "ru",
    "GREEK": "el", "HEBREW": "he", "THAI": "th",
}

_MARKER_WORDS = {
    "telugu": "te", "hindi": "hi", "tamil": "ta", "kannada": "kn", "malayalam": "ml", "bengali": "bn",
    "bangla": "bn", "punjabi": "pa", "marathi": "mr", "gujarati": "gu", "odia": "or",
}
_MARKER = re.compile(r"\b(" + "|".join(_MARKER_WORDS) + r")\b", re.IGNORECASE)

# Common words of sung lyrics. Only words that are distinctive for the language.
_WORDS = {
    "en": "the and you i me my your we to of in it is that be baby oh yeah know don't can't when all so "
          "just love never want heart tonight like what with",
    "hi": "hai hain main mein tum tera teri tere mera meri mere dil kya nahi nahin hum ho ka ki ke se ko jo "
          "yeh ye woh wo pyaar pyar ishq jaana jaan saath tujhe mujhe kabhi bhi kuch raat aankhon yaara "
          "sajna zindagi tujhse dard tu hoon hu rahe raha kaise jab tak",
    "pa": "tenu menu sohna sohni kudi jatt pind nal ni ve mainu tainu dass laggda kithe gal",
    "te": "nenu nuvvu ninnu naa nee naaku neeku ani emo enti oka cheli prema manasu kanulu kallu chusi "
          "undi ledu kada inka antu navvu priya kosam ayina gunde sakhi nannu ninne mana chudu vachi "
          "ela ika ivvu raave raa",
    "ta": "naan un en kanne kadhal kaadhal penne pennae pothum vaa illa enna unnai ennai nenjam azhagu "
          "thaan nee yen",
    "es": "el la que de y yo tu te mi amor corazón por para con una como quiero eres más pero",
    "pt": "você não meu minha coração eu com para uma que saudade amor",
}
_WORD_SETS = {lang: set(words.split()) for lang, words in _WORDS.items()}
_TOKEN = re.compile(r"[a-zA-ZÀ-ÿ']+")


def marker_language(*texts: str | None) -> str | None:
    """A language named in the title or album ("[TELUGU]", "- Telugu", "(Hindi)")."""
    for text in texts:
        if text and (m := _MARKER.search(text)):
            return _MARKER_WORDS[m.group(1).lower()]
    return None


def script_counts(text: str) -> Counter:
    counts: Counter = Counter()
    for ch in text:
        if ch.isalpha():
            counts[unicodedata.name(ch, "?").split(" ")[0]] += 1
    return counts


def script_language(text: str | None, min_share: float = 0.2, min_letters: int = 2) -> str | None:
    """The language of the dominant non-Latin script, if there is enough of it."""
    if not text:
        return None
    counts = script_counts(text)
    total = sum(counts.values())
    best = max(((n, s) for s, n in counts.items() if s in SCRIPT_LANG), default=None)
    if best is None or best[0] < min_letters or best[0] < min_share * total:
        return None
    return SCRIPT_LANG[best[1]]


def word_language(text: str | None, min_rate: float = 0.06, margin: float = 1.3) -> str | None:
    """Language of Latin-letter text from its common words (romanized Indian lyrics included)."""
    if not text:
        return None
    tokens = [t.lower() for t in _TOKEN.findall(text)]
    if len(tokens) < 8:
        return None
    scores = {lang: sum(t in words for t in tokens) / len(tokens) for lang, words in _WORD_SETS.items()}
    # Bollywood lyrics borrow Punjabi words; call it Punjabi only when it clearly dominates.
    scores["pa"] = scores["pa"] / 1.5
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    (lang, top), (_, second) = ranked[0], ranked[1]
    if top < min_rate or top < margin * second:
        return None
    return lang


def detect(name: str, album: str = "", artists: str = "", lyrics: str | None = None) -> tuple[str, str]:
    """(language, source), or ("", "") when it can't be told.

    source is "marker", "script", "lyrics-script" or "lyrics-words".
    """
    if lang := marker_language(name, album):
        return lang, "marker"
    if lang := script_language(f"{name} {album} {artists}"):
        return lang, "script"
    if lang := script_language(lyrics, min_letters=10):
        return lang, "lyrics-script"
    if lang := word_language(lyrics):
        return lang, "lyrics-words"
    return "", ""

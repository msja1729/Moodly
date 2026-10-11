from recommender import language as L


def test_marker_beats_everything():
    # KGF 2's Telugu version: LRCLIB returns the Tamil lyrics, the album says Telugu.
    tamil = "வா-வா என் அன்பே\nஎன் வாழ்வின் பேரன்பே\nவந்தாய் கண் முன்பே"
    assert L.detect("Mehabooba", "Kgf Chapter 2 [TELUGU]", "Ananya Bhat", tamil) == ("te", "marker")
    assert L.detect('The Karma Theme - Telugu (From "U Turn")', "", "")[0] == "te"
    assert L.marker_language("Soja Zara", "Bahubali (Hindi)") == "hi"


def test_native_script_in_title_or_lyrics():
    assert L.detect("మాటే వినదుగ", "", "") == ("te", "script")
    assert L.detect("Channa Mereya", "Ae Dil Hai Mushkil", "Arijit Singh",
                    "अच्छा चलता हूँ, दुआओं में याद रखना\nमेरे ज़िक्र का ज़ुबाँ") == ("hi", "lyrics-script")
    # Mostly Telugu with English words mixed in is still Telugu.
    assert L.script_language("లైలా... Like-a like my లైల Like-a like your లైల ఓ ప్రేమ") == "te"


def test_romanized_lyrics():
    hindi = "Tum hi ho, ab tum hi ho, zindagi ab tum hi ho, chain bhi, mera dard bhi, meri aashiqui ab tum hi ho"
    telugu = "Nenu ninnu chusi navvu, nuvvu naa manasu lo undi, prema ani cheli, ninne ninne"
    english = "I wanna be yours, let me be your baby, I know you love me and you never want to go tonight"
    assert L.detect("Tum Hi Ho", "", "", hindi) == ("hi", "lyrics-words")
    assert L.word_language(telugu) == "te"
    assert L.word_language(english) == "en"


def test_unknown_when_nothing_to_go_on():
    assert L.detect("Maate Vinadhuga", "Taxiwaala", "Sid Sriram") == ("", "")
    assert L.word_language("la la la") is None

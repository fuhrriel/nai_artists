import pytest

from nai_artists.match import default_name, match, norm_chars, normalize, strip_artists
from nai_artists.templates import CharCaption, get_template, load_settings

BODY = "1::solo, 1girl::\n\n1.1:: location, very aesthetic::, \n\noutdoors, "


@pytest.mark.parametrize(
    "prompt, artists, body",
    [
        ("artist:fuhrriel,\n\n" + BODY, ["fuhrriel"], BODY),                      # UI layout: own block
        ("::artist:fuhrriel,::\n" + BODY, ["fuhrriel"], BODY),                    # our artist_line
        ("1.2::artist:a, artist:b c,::\n" + BODY, ["a", "b c"], BODY),    # weighted, two artists
        ("artist:a, artist:b\n\n" + BODY, ["a", "b"], BODY),              # no trailing comma
        ("{artist:a}, [artist:b],\n\n" + BODY, ["a", "b"], BODY),         # brace/bracket emphasis
        ("1::solo, artist:a, 1girl::", ["a"], "1::solo, 1girl::"),         # inline mid-block
        ("1::solo, 1girl, artist:a::", ["a"], "1::solo, 1girl::"),         # inline last before closer
        ("solo, 1girl, artist:a", ["a"], "solo, 1girl"),                  # inline at end of prompt
        ("artist:a, artist:A, artist:a_", ["a", "a_"], ""),               # dedupe by slug, first spelling
        (BODY, [], BODY),                                                  # baseline: untouched
        ("rating:sensitive, 1girl", [], "rating:sensitive, 1girl"),        # other namespaces untouched
        ("cartist:x, 1girl", [], "cartist:x, 1girl"),                      # not a token
    ],
)
def test_strip_artists(prompt, artists, body):
    assert strip_artists(prompt) == (artists, body)


def test_strip_is_byte_exact_on_fixtures(solo_comment, duo_comment):
    a, body = strip_artists(solo_comment["v4_prompt"]["caption"]["base_caption"])
    assert a == ["fuhrriel"]
    assert body == solo_comment["v4_prompt"]["caption"]["base_caption"].split("\n\n", 1)[1]
    a, body = strip_artists(duo_comment["v4_prompt"]["caption"]["base_caption"])
    assert a == ["fuhrriel"]
    assert body.startswith("1::duo, 1girl, 1boy, rating:general::,\n\n") and body.endswith("outdoors, side-by-side, ")


def test_normalize():
    assert normalize("a,  b, \n\n\n  c,\nd , ,\n") == "a, b\n\nc, d"
    assert normalize("1::x::,\n \n") == "1::x::"
    assert normalize("outdoors,\nside-by-side") == normalize("outdoors, side-by-side, ")


def test_norm_chars_accepts_both_shapes():
    d = [{"char_caption": "girl, ", "centers": [{"x": 0.5, "y": 0.5}]}]
    assert norm_chars(d) == norm_chars((CharCaption("girl,", 0.5, 0.5),)) == (("girl", 0.5, 0.5),)
    assert norm_chars(None) == ()


def test_default_name():
    assert default_name(BODY) == "solo, 1girl"
    assert default_name("plain, tags, ") == "plain, tags"
    assert default_name("   ") == "untitled"


def test_match_exact_and_closest(solo_comment):
    s = load_settings()
    t = get_template("1girl", s)
    cap = solo_comment["v4_prompt"]["caption"]
    m = match(cap["base_caption"], cap["char_captions"], [t])
    assert m.exact and m.template is t and m.artists == ["fuhrriel"] and m.diffs == []

    m = match("::artist:x,::\n" + t.body.replace("outdoors", "indoors"), cap["char_captions"], [t])
    assert not m.exact and m.template is t and m.artists == ["x"]
    assert any("-outdoors" in d for d in m.diffs) and any("+indoors" in d for d in m.diffs)

    m = match(t.body, [], [t])  # chars missing -> not exact, diff says so
    assert not m.exact and any(d.startswith("chars:") for d in m.diffs)

    m = match(t.body, [], [])
    assert m.template is None and not m.exact

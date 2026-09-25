from PIL import Image, PngImagePlugin

from nai_artists.meta import base_caption, char_captions, read_comment, read_text_chunks
from tests.conftest import DUO, SOLO


def test_text_chunks_present():
    t = read_text_chunks(SOLO)
    assert t["Source"].startswith("NovelAI Diffusion V5")
    assert "Comment" in t


def test_read_comment_from_text(solo_comment):
    c = read_comment(SOLO)
    assert c == solo_comment
    assert base_caption(c).startswith("artist:fuhrriel,")
    assert char_captions(c)[0]["char_caption"] == "girl, "


def test_duo_chars(duo_comment):
    cc = char_captions(read_comment(DUO))
    assert [c["char_caption"] for c in cc] == ["girl, ", "boy, "]
    assert cc[1]["centers"] == [{"x": 0.3, "y": 0.5}]


def test_stealth_fallback_when_text_chunks_stripped(tmp_path, solo_comment):
    # Re-encode without tEXt chunks; pixels (and thus alpha LSBs) survive PNG re-encoding.
    stripped = tmp_path / "stripped.png"
    with Image.open(SOLO) as im:
        im.save(stripped, "PNG", pnginfo=PngImagePlugin.PngInfo())
    assert "Comment" not in read_text_chunks(stripped)
    c = read_comment(stripped)
    assert c is not None
    assert c["seed"] == solo_comment["seed"]
    assert base_caption(c) == base_caption(solo_comment)
    assert c["v4_prompt"] == solo_comment["v4_prompt"]


def test_no_metadata_returns_none(tmp_path):
    p = tmp_path / "plain.png"
    Image.new("RGBA", (64, 64), (1, 2, 3, 255)).save(p)
    assert read_comment(p) is None

from pathlib import Path

import pytest

from nai_artists import config
from nai_artists.nai import build_body
from nai_artists.templates import (
    CharCaption,
    TemplateError,
    build_v4_prompts,
    get_template,
    load_settings,
    load_template,
    load_templates,
    split_front_matter,
    template_hash,
    write_template,
)


def test_shipped_settings():
    s = load_settings()
    assert (s.model, s.width, s.height, s.steps, s.scale) == ("nai-diffusion-5-full", 1024, 1024, 28, 5.0)
    assert (s.sampler, s.noise_schedule, s.seed) == ("k_euler_ancestral", "karras", 2137)
    assert s.artist_line == "::artist:{artist},::\n"
    assert s.pacing.gap_ms == 2000
    assert s.battery.check_every == 10 and s.battery.min_percent == 5
    assert "pacing" not in s.hash_material() and "battery" not in s.hash_material()
    assert "negative" in s.hash_material()


def test_pacing_is_one_fixed_gap(tmp_path: Path):
    from nai_artists.pacing import Pacer

    assert Pacer(load_settings().pacing).next_delay() == (2.0, "gap")
    old = tmp_path / "settings.toml"  # the jittered ranges of older versions are refused, not half-read
    old.write_text(config.SETTINGS_FILE.read_text(encoding="utf-8").replace("gap_ms = 2000", "gap_ms = [1000, 5000]"), encoding="utf-8")
    with pytest.raises(TemplateError, match="gap_ms"):
        load_settings(old)


def test_negative_is_verbatim_from_fixture(solo_comment):
    assert load_settings().negative == solo_comment["uc"]


def test_split_front_matter_keeps_trailing_space():
    fm, body = split_front_matter("---\nname: x\n---\na, b, \n\nc, \n")
    assert fm == {"name": "x"}
    assert body == "a, b, \n\nc, "  # one trailing newline stripped, trailing ', ' kept


def test_split_front_matter_errors():
    with pytest.raises(TemplateError):
        split_front_matter("name: x\n---\nbody")
    with pytest.raises(TemplateError):
        split_front_matter("---\nname: x\nbody")


def test_1girl_template_reproduces_fixture(solo_comment):
    s = load_settings()
    t = get_template("1girl", s)
    assert t.primary and t.enabled
    expected_body = solo_comment["v4_prompt"]["caption"]["base_caption"].split("\n\n", 1)[1]
    assert t.body == expected_body
    prompt, v4p, v4n = build_v4_prompts(s, t, ["fuhrriel"])
    assert prompt == "::artist:fuhrriel,::\n" + expected_body
    assert v4p["caption"]["char_captions"] == solo_comment["v4_prompt"]["caption"]["char_captions"]
    assert v4p["use_order"] is True and v4p["use_coords"] is False and v4p["legacy_uc"] is False
    assert v4n["caption"]["char_captions"] == solo_comment["v4_negative_prompt"]["caption"]["char_captions"]
    assert v4n["caption"]["base_caption"] == solo_comment["uc"]
    assert v4n["use_order"] is False


def test_multi_artist_prefix():
    s = load_settings()
    t = get_template("1girl", s)
    prompt, *_ = build_v4_prompts(s, t, ["a", "b c"])
    assert prompt.startswith("::artist:a,::\n::artist:b c,::\n1::solo")


def test_hash_changes_with_body_chars_and_settings(tmp_path: Path):
    s = load_settings()
    chars = (CharCaption("girl, ", 0.5, 0.5),)
    h = template_hash("body", chars, s)
    assert h != template_hash("body2", chars, s)
    assert h != template_hash("body", (CharCaption("girl, ", 0.4, 0.5),), s)
    s2 = load_settings()
    object.__setattr__(s2, "raw", {**s.raw, "steps": 29})
    assert h != template_hash("body", chars, s2)
    s3 = load_settings()
    object.__setattr__(s3, "raw", {**s.raw, "pacing": {"gap_ms": 1}})
    assert h == template_hash("body", chars, s3)
    s4 = load_settings()
    object.__setattr__(s4, "raw", {**s.raw, "battery": {"min_percent": 50}})
    assert h == template_hash("body", chars, s4)


def test_battery_low():
    from nai_artists.nai import battery_low
    assert battery_low({"battery_percent": 5, "is_negative": False}, 5)
    assert battery_low({"battery_percent": 40, "is_negative": True}, 5)
    assert not battery_low({"battery_percent": 6, "is_negative": False}, 5)
    assert not battery_low({"battery_percent": None, "is_negative": False}, 5)


def test_write_and_reload_roundtrip(tmp_path: Path):
    s = load_settings()
    p = tmp_path / "x.txt"
    body = "1::solo, 1girl::\n\nstuff, "
    write_template(p, "X", body, chars=[CharCaption("girl, ", 0.5, 0.5)], primary=True, sort=3)
    t = load_template(p, s)
    assert t.id == "x" and t.name == "X" and t.primary and t.enabled and t.sort == 3
    assert t.body == body
    assert t.chars == (CharCaption("girl, ", 0.5, 0.5),)


def test_load_templates_sort_order(tmp_path: Path):
    s = load_settings()
    write_template(tmp_path / "zzz.txt", "z", "b", sort=1)
    write_template(tmp_path / "aaa.txt", "a", "b")
    write_template(tmp_path / "mmm.txt", "m", "b", sort=0)
    assert [t.id for t in load_templates(s, tmp_path)] == ["mmm", "zzz", "aaa"]


def test_body_shape():
    s = load_settings()
    t = get_template("1girl", s)
    b = build_body(s, t, ["fuhrriel"])
    p = b["parameters"]
    assert b["model"] == "nai-diffusion-5-full" and b["action"] == "generate"
    assert b["input"] == p["prompt"] == p["v4_prompt"]["caption"]["base_caption"]
    assert p["negative_prompt"] == p["v4_negative_prompt"]["caption"]["base_caption"] == s.negative
    assert p["seed"] == p["extra_noise_seed"] == 2137
    assert (p["params_version"], p["ucPreset"], p["qualityToggle"]) == (4, 3, False)
    assert (p["width"], p["height"], p["steps"], p["scale"]) == (1024, 1024, 28, 5.0)
    assert p["sm"] is False and p["sm_dyn"] is False and p["prefer_brownian"] is True
    assert p["image_format"] == "png" and p["n_samples"] == 1


def test_slugs():
    assert config.artist_slug("FUHRRIEL") == "fuhrriel"
    assert config.artist_slug("abc109 (foo)") == "abc109__foo_"
    assert config.combo_slug(["A", "b c"]) == "a+b_c"
    assert config.combo_slug([]) == "_base"
    for bad in ("", "   ", "_base", "-BASE"):
        with pytest.raises(ValueError):
            config.artist_slug(bad)


def test_baseline_prompt_is_bare_body():
    s = load_settings()
    t = get_template("1girl", s)
    prompt, v4p, _ = build_v4_prompts(s, t, [])
    assert prompt == t.body
    assert "artist" not in prompt
    assert v4p["caption"]["char_captions"]  # chars still apply to the baseline

"""Cross-platform bits of the CLI and file handling. No network."""

from pathlib import Path

import pytest

from nai_artists import config
from nai_artists.cli import expand_paths
from nai_artists.templates import write_template


def test_expand_paths_globs_what_the_shell_did_not(tmp_path: Path, monkeypatch):
    for n in ("b.png", "a.png", "c.txt", "[x].png"):
        (tmp_path / n).write_bytes(b"")
    got = expand_paths([str(tmp_path / "*.png"), str(tmp_path / "[x].png"), str(tmp_path / "nope*.png")])
    assert got == [tmp_path / "[x].png", tmp_path / "a.png", tmp_path / "b.png",  # sorted matches
                   tmp_path / "[x].png",  # exists literally: not treated as a pattern
                   tmp_path / "nope*.png"]  # no match: kept, reported missing by the caller
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert expand_paths(["~/a.png"]) == [tmp_path / "a.png"]


def test_templates_are_written_with_lf(tmp_path: Path):
    p = tmp_path / "x.txt"
    write_template(p, "X", "a, \n\nb, ")
    assert b"\r" not in p.read_bytes()


def test_slug_avoids_windows_device_names():
    assert config.artist_slug("CON") == "con_" and config.artist_slug("com1") == "com1_"
    assert config.combo_slug(["aux", "b"]) == "aux_+b"
    assert config.artist_slug("console") == "console" and config.artist_slug("com10") == "com10"


def test_gen_is_one_image_per_run(monkeypatch):
    """Everything is refused before a NovelAI client exists."""
    from nai_artists import cli

    monkeypatch.setattr(cli, "NAIClient", lambda *a, **k: pytest.fail("gen reached the network"))
    assert cli.main(["gen", "-a", "x", "-t", "1girl", "-t", "1girl"]) == 2
    assert cli.main(["gen", "-a", "x"]) == 2
    assert cli.main(["gen", "-t", "1girl"]) == 2  # neither an artist nor --base
    assert cli.main(["gen", "--base", "-a", "x", "-t", "1girl"]) == 2
    for flag in ("--all", "--primary"):
        with pytest.raises(SystemExit):
            cli.main(["gen", "-a", "x", flag])

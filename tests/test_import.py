"""End-to-end: template-from-image + import against the two reference PNGs, in a temp data dir."""

import pytest
from PIL import Image

from nai_artists import config, db
from nai_artists.importer import import_png, params_check
from nai_artists.meta import read_comment
from nai_artists.nai import build_body
from nai_artists.templates import create_from_image, get_template, load_settings, load_templates
from nai_artists.thumbs import make_thumb
from tests.conftest import DUO, SOLO, TEMPLATES
from tests.test_app import make_png


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "FULL_DIR", data / "full")
    monkeypatch.setattr(config, "THUMBS_DIR", data / "thumbs")
    monkeypatch.setattr(config, "INBOX_DIR", data / "inbox")
    monkeypatch.setattr(config, "DB_FILE", data / "nai.db")
    monkeypatch.setattr(config, "TEMPLATES_DIR", tmp_path / "templates")
    return tmp_path


def test_thumb(tmp_path):
    out = make_thumb(SOLO, tmp_path / "t.webp")
    with Image.open(out) as im:
        assert im.format == "WEBP" and max(im.size) == 384 and im.mode == "RGB"


def test_params_check_on_fixture(solo_comment):
    ok, note = params_check(load_settings(), solo_comment)
    assert not ok and "size 1088x960 != 1024x1024" in note and "seed 507589385 != 2137" in note
    fixed = {**solo_comment, "width": 1024, "height": 1024, "seed": 2137}
    assert params_check(load_settings(), fixed) == (True, None)


def test_template_from_image_reproduces_1girl_byte_for_byte(sandbox):
    t, artists = create_from_image(read_comment(SOLO), "1girl", "Solo girl", primary=True)
    assert artists == ["fuhrriel"]
    assert t.path.read_bytes() == (TEMPLATES / "1girl.txt").read_bytes()


def test_template_from_image_validation(sandbox):
    from nai_artists.templates import TemplateError
    c = read_comment(SOLO)
    with pytest.raises(TemplateError):
        create_from_image(c, "Bad Id")
    create_from_image(c, "x")
    with pytest.raises(TemplateError):
        create_from_image(c, "x")
    t, _ = create_from_image(c, "x", "renamed", force=True)
    assert t.name == "renamed"


def test_create_then_reimport_matches(sandbox):
    """Create 1girl + duo from the PNGs, re-import both -> own template, fuhrriel, params_match=false."""
    s = load_settings()
    conn = db.connect()
    for src, tid, name, primary in ((SOLO, "1girl", "Solo girl", True), (DUO, "duo", "Duo", False)):
        t, artists = create_from_image(read_comment(src), tid, name, primary=primary, settings=s)
        assert artists == ["fuhrriel"]
    templates = load_templates(s)
    assert [t.id for t in templates] == ["1girl", "duo"]
    assert [len(t.chars) for t in templates] == [1, 2]

    for src, tid in ((SOLO, "1girl"), (DUO, "duo")):
        r = import_png(conn, s, templates, src)
        assert r.status == "imported" and r.exact and r.template_id == tid and r.artists == ["fuhrriel"]
        assert r.params_match is False and "size 1088x960" in r.mismatch_note
        assert r.dest == config.FULL_DIR / "fuhrriel" / f"{tid}.png" and r.dest.read_bytes() == src.read_bytes()
        assert (config.THUMBS_DIR / "fuhrriel" / f"{tid}.webp").is_file()
        # second time: cell exists, nothing touched unless replace
        assert import_png(conn, s, templates, src).status == "exists"
        assert import_png(conn, s, templates, src, replace=True).status == "replaced"

    rows = db.list_images(conn)
    assert {(r["template_id"], r["source"], r["seed"], r["params_match"]) for r in rows} == {
        ("1girl", "imported", 507589385, 0), ("duo", "imported", 507589385, 0)}
    assert rows[0]["path"] == "full/fuhrriel/1girl.png" and rows[0]["thumb_path"] == "thumbs/fuhrriel/1girl.webp"
    assert rows[0]["template_hash"] == get_template("1girl", s).hash
    meta = db.image_meta(rows[0])
    assert meta["model_hash"] == "0ADF9AB7" and "prompt" not in meta and "uc" not in meta and "v4_prompt" not in meta
    assert not list(config.INBOX_DIR.glob("*")) if config.INBOX_DIR.exists() else True


def test_import_no_match_goes_to_inbox_and_force_assign(sandbox):
    s = load_settings()
    conn = db.connect()
    create_from_image(read_comment(SOLO), "1girl", settings=s)
    templates = load_templates(s)
    r = import_png(conn, s, templates, DUO)
    assert r.status == "no_match" and r.template_id == "1girl" and r.diffs and r.artists == ["fuhrriel"]
    assert r.inbox == config.INBOX_DIR / DUO.name and r.inbox.is_file()
    assert db.list_images(conn) == []
    # same file again gets a fresh name in inbox, not a clobber
    assert import_png(conn, s, templates, DUO).inbox.name != DUO.name
    r = import_png(conn, s, templates, DUO, force_template=templates[0])
    assert r.status == "imported" and not r.exact and r.params_match is False
    assert r.mismatch_note.startswith("prompt differs from template") and "size 1088x960" in r.mismatch_note
    assert db.get_image(conn, db.get_artist_by_slug(conn, "fuhrriel")["id"], "1girl")["params_match"] == 0


def test_import_dry_run_touches_nothing(sandbox):
    s = load_settings()
    conn = db.connect()
    create_from_image(read_comment(SOLO), "1girl", settings=s)
    r = import_png(conn, s, load_templates(s), SOLO, dry_run=True)
    assert r.status == "dry_run" and r.exact and r.params_match is False
    assert db.list_images(conn) == [] and not config.FULL_DIR.exists()


def test_import_no_metadata(sandbox, tmp_path):
    s = load_settings()
    conn = db.connect()
    p = tmp_path / "plain.png"
    Image.new("RGB", (8, 8)).save(p)
    r = import_png(conn, s, [], p)
    assert r.status == "no_metadata" and r.inbox.is_file()


def test_import_baseline_and_own_output(sandbox, tmp_path):
    """A PNG with no artist tokens is the _base cell; our own ::artist:x,:: line form matches too."""
    s = load_settings()
    conn = db.connect()
    t, _ = create_from_image(read_comment(SOLO), "1girl", settings=s)
    for artists in ([], ["fuhrriel"]):
        src = tmp_path / f"own_{len(artists)}.png"
        src.write_bytes(make_png(build_body(s, t, artists), s.seed))
        r = import_png(conn, s, [t], src)
        assert r.status == "imported" and r.artists == artists and r.exact
        assert r.slug == config.combo_slug(artists) and r.params_match is True, r.mismatch_note


def test_register_never_clobbers_unregistered_file_on_disk(sandbox):
    """A cell file that exists on disk but not in the DB (generated pre-DB) is not overwritten."""
    s = load_settings()
    conn = db.connect()
    t, _ = create_from_image(read_comment(SOLO), "1girl", settings=s)
    dest = config.FULL_DIR / "fuhrriel" / "1girl.png"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"precious")
    r = import_png(conn, s, [t], SOLO)
    assert r.status == "exists" and dest.read_bytes() == b"precious" and db.list_images(conn) == []
    r = import_png(conn, s, [t], SOLO, replace=True)
    assert r.status == "replaced" and dest.read_bytes() == SOLO.read_bytes() and len(db.list_images(conn)) == 1

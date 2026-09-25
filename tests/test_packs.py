"""Artist packs: export picked artists x templates as a zip, import it into another install. No network.

Two installs in one test: `make_home` points every config path at a fresh root; the exporter's files stay
on disk after switching, so the receiver can be compared against them byte for byte.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import warnings
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from nai_artists import config, db, packs
from nai_artists.app import create_app
from nai_artists.importer import import_png
from nai_artists.meta import read_comment
from nai_artists.nai import build_body
from nai_artists.packs import PackError
from nai_artists.templates import create_from_image, load_settings, load_template, load_templates
from tests.conftest import DUO, ROOT, SOLO
from tests.test_app import FAST_PACING, FakeNAI, make_png

ARTISTS = ([], ["fuhrriel"], ["painter a"], ["painter a", "fuhrriel"], ["private one"])


def make_home(monkeypatch, root: Path, seed: int | None = None) -> Path:
    """One install: settings.toml, templates/, data/ under `root`, and every config path pointed at it."""
    root.mkdir(parents=True)
    text = (ROOT / "settings.toml").read_text(encoding="utf-8").split("\n[pacing]")[0] + FAST_PACING
    if seed is not None:
        text = re.sub(r"(?m)^seed = \d+", f"seed = {seed}", text)
    (root / "settings.toml").write_text(text, encoding="utf-8")
    data = root / "data"
    for k, v in {"DATA_DIR": data, "FULL_DIR": data / "full", "THUMBS_DIR": data / "thumbs", "INBOX_DIR": data / "inbox",
                 "REFS_DIR": data / "refs", "DB_FILE": data / "nai.db", "TEMPLATES_DIR": root / "templates",
                 "SETTINGS_FILE": root / "settings.toml"}.items():
        monkeypatch.setattr(config, k, v)
    return root


def add_cell(conn, artists: list[str], tid: str, seed: int | None = None) -> None:
    s = load_settings()
    t = next(t for t in load_templates(s) if t.id == tid)
    src = config.DATA_DIR / "src" / f"{config.combo_slug(artists)}-{tid}-{seed}.png"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(make_png(build_body(s, t, artists), s.seed if seed is None else seed))
    r = import_png(conn, s, [t], src, replace=True)
    assert r.status in ("imported", "replaced") and r.exact, r.line()


@pytest.fixture
def exporter(tmp_path, monkeypatch):
    """Install A: 1girl + duo, every artist in ARTISTS has both cells, plus things that must never travel."""
    home = make_home(monkeypatch, tmp_path / "a")
    s = load_settings()
    create_from_image(read_comment(SOLO), "1girl", "Solo girl", primary=True, sort=5, settings=s)
    create_from_image(read_comment(DUO), "duo", "Duo", settings=s)
    conn = db.connect()
    for artists in ARTISTS:
        for tid in ("1girl", "duo"):
            add_cell(conn, artists, tid)
    fid = db.get_artist_by_slug(conn, "fuhrriel")["id"]
    db.set_rating(conn, fid, None, 2, "secret note")
    db.set_rating(conn, fid, "1girl", 1, None)
    db.set_labels(conn, fid, ["painterly"])
    db.replace_refs(conn, fid, [{"post_id": 1, "rank": 0, "path": "refs/full/fuhrriel/1.jpg", "thumb_path": "refs/thumbs/fuhrriel/1.webp"}])
    return home


def export(dest: Path, slugs: set[str], tids=("1girl", "duo"), name="test pack") -> packs.ExportPlan:
    s = load_settings()
    plan = packs.export_plan(db.connect(), load_templates(s), slugs, list(tids))
    dest.write_bytes(b"".join(packs.stream_pack(plan, s, name)))
    return plan


def import_all(path: Path, replace: bool = False) -> tuple[dict, dict[str, list[packs.CellResult]]]:
    """What the Import tab does: inspect, templates, then one call per artist."""
    s = load_settings()
    conn = db.connect()
    with packs.open_pack(str(path)) as pack:
        info = packs.inspect(conn, s, load_templates(s), pack)
    with packs.open_pack(str(path)) as pack:
        packs.install_templates(pack, load_templates(s))
    results = {}
    for a in info["artists"]:
        with packs.open_pack(str(path)) as pack:
            results[a["slug"]] = packs.import_artist(conn, s, load_templates(s), pack, a["slug"], replace=replace)
    return info, results


def statuses(results) -> dict[str, list[str]]:
    return {slug: [r.status for r in rs] for slug, rs in results.items()}


def test_round_trip(exporter, tmp_path, monkeypatch):
    pack = tmp_path / "pack.zip"
    plan = export(pack, {"fuhrriel", "painter_a+fuhrriel"})
    assert plan.skipped == [] and len(plan.cells) == 6
    with zipfile.ZipFile(pack) as zf:
        names = zf.namelist()
        assert names[-1] == "manifest.json" and all(zi.compress_type == zipfile.ZIP_STORED for zi in zf.infolist())
        assert sorted(names) == sorted(["manifest.json", "templates/1girl.txt", "templates/duo.txt",
                                        *(f"images/{s}/{t}.png" for s in ("_base", "fuhrriel", "painter_a+fuhrriel") for t in ("1girl", "duo"))])
        m = json.loads(zf.read("manifest.json"))
        assert "primary: false" in zf.read("templates/1girl.txt").decode() and "sort" not in zf.read("templates/1girl.txt").decode()
    assert m["format"] == 1 and m["name"] == "test pack" and m["settings"] == load_settings().hash_material()
    assert "pacing" not in m["settings"] and "battery" not in m["settings"]
    assert {c["slug"]: c["tags"] for c in m["cells"]}["painter_a+fuhrriel"] == ["painter a", "fuhrriel"]
    a_full, a_templates = config.FULL_DIR, config.TEMPLATES_DIR

    make_home(monkeypatch, tmp_path / "b")
    info, results = import_all(pack)
    assert info["settings_diff"] == [] and info["existing"] == 0 and info["cells"] == 6
    assert [(t["id"], t["action"], t["target"]) for t in info["templates"]] == [("1girl", "install", "1girl"), ("duo", "install", "duo")]
    assert [a["slug"] for a in info["artists"]] == ["_base", "fuhrriel", "painter_a+fuhrriel"]
    assert statuses(results) == {s: ["imported", "imported"] for s in ("_base", "fuhrriel", "painter_a+fuhrriel")}

    s = load_settings()
    ts = {t.id: t for t in load_templates(s)}
    assert set(ts) == {"1girl", "duo"} and not ts["1girl"].primary and ts["1girl"].enabled and ts["1girl"].sort is None
    theirs = load_template(a_templates / "1girl.txt", s)
    assert ts["1girl"].name == "Solo girl" and (ts["1girl"].body, ts["1girl"].chars) == (theirs.body, theirs.chars)
    conn = db.connect()
    assert [a["slug"] for a in db.list_artists(conn)] == ["_base", "fuhrriel", "painter_a+fuhrriel"]
    rows = db.list_images(conn)
    assert len(rows) == 6
    for r in rows:
        slug = db.get_artist(conn, r["artist_id"])["slug"]
        assert db.abs_path(r["path"]).read_bytes() == (a_full / slug / f"{r['template_id']}.png").read_bytes()
        assert db.abs_path(r["thumb_path"]).is_file()
        assert r["template_hash"] == ts[r["template_id"]].hash and r["params_match"] and r["source"] == "imported"
    assert db.list_ratings(conn) == {} and db.list_labels(conn) == {} and db.list_refs(conn) == {}
    assert db.get_artist_by_slug(conn, "fuhrriel")["tag"] == "fuhrriel"
    assert not (config.DATA_DIR / "tmp").exists() or not any((config.DATA_DIR / "tmp").iterdir())

    # again: everything is already there, templates are found by content
    info, results = import_all(pack)
    assert info["existing"] == 6 and {t["action"] for t in info["templates"]} == {"reuse"}
    assert statuses(results) == {s: ["exists", "exists"] for s in ("_base", "fuhrriel", "painter_a+fuhrriel")}


def test_equivalent_template_reused_and_id_clash_renamed(exporter, tmp_path, monkeypatch):
    pack = tmp_path / "pack.zip"
    export(pack, {"fuhrriel"})
    make_home(monkeypatch, tmp_path / "b")
    s = load_settings()
    create_from_image(read_comment(SOLO), "solo", "My solo", primary=True, settings=s)  # same content as their 1girl
    config.TEMPLATES_DIR.joinpath("duo.txt").write_text("---\nname: mine\n---\nsomething else entirely,\n", encoding="utf-8")
    mine = config.TEMPLATES_DIR.joinpath("duo.txt").read_bytes()
    info, results = import_all(pack)
    t = {x["id"]: x for x in info["templates"]}
    assert (t["1girl"]["action"], t["1girl"]["target"]) == ("reuse", "solo")
    assert t["duo"]["action"] == "rename" and re.fullmatch(r"duo-[0-9a-f]{6}", t["duo"]["target"])
    renamed = t["duo"]["target"]
    assert config.TEMPLATES_DIR.joinpath("duo.txt").read_bytes() == mine
    assert {x.id for x in load_templates(s)} == {"solo", "duo", renamed}
    assert {(r.template, r.target, r.status) for r in results["fuhrriel"]} == {("1girl", "solo", "imported"), ("duo", renamed, "imported")}
    conn = db.connect()
    fid = db.get_artist_by_slug(conn, "fuhrriel")["id"]
    assert {r["template_id"] for r in db.list_images(conn, artist_id=fid)} == {"solo", renamed}
    # a re-run finds the renamed template by content instead of renaming again
    info, _ = import_all(pack)
    assert {x["id"]: (x["action"], x["target"]) for x in info["templates"]} == {"1girl": ("reuse", "solo"), "duo": ("reuse", renamed)}


def test_case_insensitive_id_clash(exporter, tmp_path, monkeypatch):
    pack = tmp_path / "pack.zip"
    export(pack, {"fuhrriel"}, tids=("duo",))
    make_home(monkeypatch, tmp_path / "b")
    config.TEMPLATES_DIR.mkdir()
    config.TEMPLATES_DIR.joinpath("Duo.txt").write_text("---\nname: mine\n---\nsomething else,\n", encoding="utf-8")
    info, _ = import_all(pack)
    assert info["templates"][0]["action"] == "rename"


def test_settings_mismatch_warns_and_marks_cells(exporter, tmp_path, monkeypatch):
    pack = tmp_path / "pack.zip"
    export(pack, {"fuhrriel"})
    make_home(monkeypatch, tmp_path / "b", seed=7)
    info, results = import_all(pack)
    assert info["settings_diff"] == ["seed"]
    cells = [r for rs in results.values() for r in rs]
    assert all(r.status == "imported" and r.params_match is False and "seed 2137 != 7" in r.mismatch_note for r in cells)


def test_existing_cells_and_replace(exporter, tmp_path, monkeypatch):
    pack = tmp_path / "pack.zip"
    export(pack, {"fuhrriel"}, tids=("1girl",))
    make_home(monkeypatch, tmp_path / "b")
    create_from_image(read_comment(SOLO), "1girl", "Solo girl", settings=load_settings())
    conn = db.connect()
    add_cell(conn, [], "1girl", seed=99)  # the receiver's own baseline, different bytes
    own = (config.FULL_DIR / "_base" / "1girl.png").read_bytes()
    info, results = import_all(pack)
    assert {a["slug"]: a["existing"] for a in info["artists"]} == {"_base": 1, "fuhrriel": 0}
    assert statuses(results) == {"_base": ["exists"], "fuhrriel": ["imported"]}
    assert (config.FULL_DIR / "_base" / "1girl.png").read_bytes() == own
    _, results = import_all(pack, replace=True)
    assert statuses(results) == {"_base": ["replaced"], "fuhrriel": ["replaced"]}
    assert (config.FULL_DIR / "_base" / "1girl.png").read_bytes() != own


def test_export_skips_missing_stale_forced_and_gone(exporter, tmp_path):
    conn = db.connect()
    s = load_settings()
    ts = {t.id: t for t in load_templates(s)}
    fid = db.get_artist_by_slug(conn, "fuhrriel")["id"]
    pid = db.get_artist_by_slug(conn, "painter_a")["id"]
    conn.execute("UPDATE images SET template_hash = 'old' WHERE artist_id = ? AND template_id = 'duo'", (fid,))
    db.delete_image(conn, db.get_image(conn, pid, "duo")["id"])
    (config.FULL_DIR / "painter_a" / "1girl.png").unlink()
    forced = import_png(conn, s, [ts["1girl"]], DUO, force_template=ts["1girl"], replace=True)  # duo prompt forced onto 1girl
    assert forced.status == "replaced" and not forced.exact
    comma = db.get_or_create_artist(conn, ["foo, bar"])  # the Add artists API allows it; artist_tags() splits it
    assert comma["slug"] == "foo__bar"
    add_cell(conn, ["foo"], "1girl")
    conn.execute("UPDATE images SET artist_id = ? WHERE artist_id = ?", (comma["id"], db.get_artist_by_slug(conn, "foo")["id"]))
    plan = packs.export_plan(conn, load_templates(s), {"fuhrriel", "painter_a", "foo__bar", "not_in_db"}, ["1girl", "duo"])
    assert sorted((x["slug"], x["template"], x["reason"]) for x in plan.skipped) == [
        ("foo__bar", "1girl", "bad tag"), ("foo__bar", "duo", "bad tag"),
        ("fuhrriel", "1girl", "forced"), ("fuhrriel", "duo", "stale"),
        ("painter_a", "1girl", "file missing"), ("painter_a", "duo", "missing")]
    assert [(c.slug, c.template.id) for c in plan.cells] == [("_base", "1girl"), ("_base", "duo")]
    assert plan.summary()["skipped_counts"] == {"bad tag": 2, "forced": 1, "stale": 1, "file missing": 1, "missing": 1}
    with pytest.raises(PackError):
        packs.export_plan(conn, load_templates(s), {"fuhrriel"}, ["nope"])


# --- untrusted archives --------------------------------------------------------------

def rewrite(src: Path, dst: Path, *, drop=(), edit=None, add=(), manifest=None, prefix="") -> Path:
    """Copy a pack, changing it on the way. `add`: (ZipInfo | name, bytes) pairs written as-is."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout, warnings.catch_warnings():
        warnings.simplefilter("ignore")  # duplicate names
        m = json.loads(zin.read("manifest.json"))
        if manifest:
            manifest(m)
        for zi in zin.infolist():
            if zi.filename in drop or zi.filename == "manifest.json":
                continue
            data = zin.read(zi)
            if edit and zi.filename in edit:
                data = edit[zi.filename](data)
            zout.writestr(prefix + zi.filename, data)
        for name, data in add:
            zout.writestr(name, data)
        zout.writestr(prefix + "manifest.json", json.dumps(m))
    return dst


def link(name: str) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(name)
    zi.external_attr = (stat.S_IFLNK | 0o777) << 16
    return zi


def deflated(name: str) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(name)
    zi.compress_type = zipfile.ZIP_DEFLATED
    return zi


def set_cell(m, which, tid, **kw):
    next(c for c in m["cells"] if (c["slug"], c["template"]) == (which, tid)).update(kw)


BAD = {
    "dotdot": dict(add=[("../evil.png", b"x")]),
    "absolute": dict(add=[("/tmp/evil.png", b"x")]),
    "stray": dict(add=[("notes.txt", b"hi")]),
    "stray dir": dict(add=[("images/other/", b"")]),
    "symlink": dict(drop=["images/fuhrriel/1girl.png"], add=[(link("images/fuhrriel/1girl.png"), b"/home/someone/.ssh/id_ed25519")]),
    "duplicate": dict(add=[("templates/1girl.txt", b"---\nname: x\n---\nx\n")]),
    "missing member": dict(drop=["images/fuhrriel/duo.png"]),
    "bad template id": dict(manifest=lambda m: m["templates"][0].update(id="../1girl")),
    "slug not from tags": dict(manifest=lambda m: set_cell(m, "fuhrriel", "1girl", slug="../../fuhrriel")),
    "reserved tag": dict(manifest=lambda m: set_cell(m, "fuhrriel", "1girl", tags=["_base"])),
    "template sha": dict(edit={"templates/1girl.txt": lambda b: b.replace(b"outdoors", b"indoors")}),
    "bomb": dict(drop=["images/fuhrriel/duo.png"], add=[(deflated("images/fuhrriel/duo.png"), b"\0" * (8 << 20))]),
    "format": dict(manifest=lambda m: m.update(format=2)),
    "in a folder": dict(prefix="pack/"),
}


@pytest.mark.parametrize("case", BAD)
def test_malicious_pack_rejected_before_writing(exporter, tmp_path, monkeypatch, case):
    good = tmp_path / "pack.zip"
    export(good, {"fuhrriel"})
    bad = rewrite(good, tmp_path / "bad.zip", **BAD[case])
    make_home(monkeypatch, tmp_path / "b")
    with pytest.raises(PackError):
        with packs.open_pack(str(bad)):
            pass
    assert not config.TEMPLATES_DIR.exists() and not config.DATA_DIR.exists()


def test_not_a_zip_oversized_and_path_forms(exporter, tmp_path, monkeypatch):
    good = tmp_path / "pack.zip"
    export(good, {"fuhrriel"})
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not a zip at all")
    with pytest.raises(PackError, match="not a zip"):
        packs.open_pack(str(junk))
    with pytest.raises(PackError, match="no such file"):
        packs.open_pack(str(tmp_path / "nope.zip"))
    with packs.open_pack(f'  "{good}"  ') as p:  # Windows "Copy as path" adds quotes
        assert p.name == "test pack"
    monkeypatch.setattr(packs, "MAX_PNG_BYTES", 100)
    with pytest.raises(PackError, match="larger than"):
        packs.open_pack(str(good))


def test_bad_cells_are_rejected_one_by_one(exporter, tmp_path, monkeypatch):
    good = tmp_path / "pack.zip"
    export(good, {"fuhrriel", "painter_a"})
    other = (config.FULL_DIR / "painter_a" / "duo.png").read_bytes()
    bad = rewrite(good, tmp_path / "bad.zip",
                  edit={"images/fuhrriel/1girl.png": lambda b: b + b"x",  # sha no longer matches
                        "images/fuhrriel/duo.png": lambda b: other},  # painter a's image under fuhrriel's name
                  manifest=lambda m: set_cell(m, "fuhrriel", "duo", sha256=hashlib.sha256(other).hexdigest()))
    make_home(monkeypatch, tmp_path / "b")
    _, results = import_all(bad)
    r = {x.template: x for x in results["fuhrriel"]}
    assert r["1girl"].status == "rejected" and "sha256" in r["1girl"].reason
    assert r["duo"].status == "rejected" and "painter a" in r["duo"].reason
    assert statuses(results)["painter_a"] == ["imported", "imported"]
    assert not (config.FULL_DIR / "fuhrriel").exists() and not (config.INBOX_DIR).exists()
    assert db.get_artist_by_slug(db.connect(), "fuhrriel") is None


def test_huge_image_rejected(exporter, tmp_path, monkeypatch):
    good = tmp_path / "pack.zip"
    export(good, {"fuhrriel"}, tids=("1girl",))
    buf = io.BytesIO()
    Image.new("L", (64, 64)).save(buf, "PNG")
    monkeypatch.setattr(packs, "MAX_PIXELS", 32 * 32)
    big = buf.getvalue()
    bad = rewrite(good, tmp_path / "bad.zip", edit={"images/fuhrriel/1girl.png": lambda b: big},
                  manifest=lambda m: set_cell(m, "fuhrriel", "1girl", sha256=hashlib.sha256(big).hexdigest()))
    make_home(monkeypatch, tmp_path / "b")
    _, results = import_all(bad)
    assert results["fuhrriel"][0].status == "rejected" and "larger than" in results["fuhrriel"][0].reason


# --- routes ------------------------------------------------------------------------

def test_routes_pick_export_import(exporter, tmp_path, monkeypatch):
    with TestClient(create_app(FakeNAI)) as c:
        assert c.get("/api/matrix").json()["picks"] == []
        assert c.post("/api/picks", json={"add": ["_base"]}).status_code == 400
        assert c.post("/api/picks", json={"add": ["nobody"]}).status_code == 400
        assert c.get("/api/export?templates=1girl").status_code == 400  # nothing picked
        assert c.post("/api/picks", json={"add": ["fuhrriel", "painter_a", "private_one"]}).json()["picks"] == ["fuhrriel", "painter_a", "private_one"]
        assert c.post("/api/picks", json={"remove": ["private_one"]}).json()["picks"] == ["fuhrriel", "painter_a"]
        assert c.get("/api/matrix").json()["picks"] == ["fuhrriel", "painter_a"]
        p = c.post("/api/export/preview", json={"templates": ["1girl", "duo"]}).json()
        assert (p["artists"], p["cells"], p["base_cells"], p["picked"], p["skipped"]) == (2, 6, 2, 2, [])
        assert p["bytes"] == sum(f.stat().st_size for f in config.FULL_DIR.glob("*/*.png")
                                 if f.parent.name in ("_base", "fuhrriel", "painter_a"))
        assert c.post("/api/export/preview", json={"templates": ["nope"]}).status_code == 400
        r = c.get("/api/export", params={"templates": ["1girl", "duo"], "name": "My pack / 2026"})
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        assert r.headers["content-disposition"] == 'attachment; filename="My_pack_2026.zip"'
        assert c.post("/api/picks", json={"clear": True}).json()["picks"] == []
    pack = tmp_path / "dl.zip"
    pack.write_bytes(r.content)

    make_home(monkeypatch, tmp_path / "b")
    with TestClient(create_app(FakeNAI)) as c:
        assert c.post("/api/import-pack/inspect", json={"path": str(tmp_path / "nope.zip")}).status_code == 400
        info = c.post("/api/import-pack/inspect", json={"path": f'"{pack}"'}).json()
        assert info["name"] == "My pack / 2026" and [a["slug"] for a in info["artists"]] == ["_base", "fuhrriel", "painter_a"]
        early = c.post("/api/import-pack/artist", json={"path": str(pack), "slug": "fuhrriel"}).json()
        assert early["counts"] == {"rejected": 2}  # templates step not run yet
        t = c.post("/api/import-pack/templates", json={"path": str(pack)}).json()
        assert [x["action"] for x in t["templates"]] == ["install", "install"]
        for a in info["artists"]:
            r = c.post("/api/import-pack/artist", json={"path": str(pack), "slug": a["slug"]}).json()
            assert r["counts"] == {"imported": 2}, r
        assert c.post("/api/import-pack/artist", json={"path": str(pack), "slug": "nobody"}).status_code == 400
        m = c.get("/api/matrix").json()
        assert [a["slug"] for a in m["artists"]] == ["_base", "fuhrriel", "painter_a"]
        assert all(len(a["cells"]) == 2 for a in m["artists"])

import sqlite3

import pytest

from nai_artists import config, db


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


def test_schema_and_baseline(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"artists", "images", "ratings", "jobs"} <= tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    base = db.get_artist_by_slug(conn, config.BASE_SLUG)
    assert base["tag"] == "" and db.artist_tags(base) == []
    db.migrate(conn)  # idempotent
    assert len(db.list_artists(conn)) == 1


def test_get_or_create_artist(conn):
    a = db.get_or_create_artist(conn, ["FUHRRIEL"])
    assert a["slug"] == "fuhrriel" and a["tag"] == "FUHRRIEL" and not a["is_combo"]
    assert db.get_or_create_artist(conn, [" fuhrriel "])["id"] == a["id"]  # slug is the identity
    c = db.get_or_create_artist(conn, ["a", "b c"])
    assert c["slug"] == "a+b_c" and c["is_combo"] and db.artist_tags(c) == ["a", "b c"]
    assert db.get_or_create_artist(conn, [])["slug"] == config.BASE_SLUG
    with pytest.raises(ValueError):
        db.get_or_create_artist(conn, ["_base"])
    assert [r["slug"] for r in db.list_artists(conn)] == ["_base", "a+b_c", "fuhrriel"]


def test_image_upsert_and_constraints(conn):
    a = db.get_or_create_artist(conn, ["x"])
    kw = dict(artist_id=a["id"], template_id="1girl", template_hash="h1", path="full/x/1girl.png",
              thumb_path="thumbs/x/1girl.webp", seed=1, source="imported", params_match=False,
              mismatch_note="size", meta={"seed": 1})
    r1 = db.upsert_image(conn, **kw)
    r2 = db.upsert_image(conn, **{**kw, "template_hash": "h2", "source": "generated", "params_match": True, "mismatch_note": None})
    assert r1["id"] == r2["id"] and r2["template_hash"] == "h2" and r2["params_match"] == 1
    assert db.image_meta(r2) == {"seed": 1}
    assert len(db.list_images(conn)) == 1 and len(db.list_images(conn, template_id="nope")) == 0
    with pytest.raises(sqlite3.IntegrityError):
        db.upsert_image(conn, **{**kw, "source": "bogus"})
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO ratings (artist_id, template_id, value) VALUES (?, NULL, 5)", (a["id"],))
    conn.execute("INSERT INTO ratings (artist_id, template_id, value) VALUES (?, NULL, 2)", (a["id"],))
    with pytest.raises(sqlite3.IntegrityError):  # one artist-level rating per artist
        conn.execute("INSERT INTO ratings (artist_id, template_id, value) VALUES (?, NULL, 1)", (a["id"],))
    conn.execute("DELETE FROM artists WHERE id = ?", (a["id"],))
    assert db.list_images(conn) == [] and conn.execute("SELECT count(*) FROM ratings").fetchone()[0] == 0

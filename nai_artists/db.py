"""SQLite, plain sqlite3, WAL. No ORM.

Never holds a prompt: templates are files, and `images.meta` is the PNG Comment
minus prompt / uc / v4_* (those stay inside the PNG, read them from there).

Paths in `images` are relative to config.DATA_DIR so the data dir can move.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import config

SCHEMA_VERSION = 2  # v2: artist_labels, refs, ref_fetches (all CREATE IF NOT EXISTS, so migrate just re-runs SCHEMA)

SCHEMA = """
CREATE TABLE IF NOT EXISTS artists (
    id          INTEGER PRIMARY KEY,
    tag         TEXT NOT NULL UNIQUE,          -- '' for the baseline; combos joined with ', '
    slug        TEXT NOT NULL UNIQUE,          -- config.combo_slug(tags); '_base' for the baseline
    is_combo    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    note        TEXT
);

CREATE TABLE IF NOT EXISTS images (
    id             INTEGER PRIMARY KEY,
    artist_id      INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    template_id    TEXT NOT NULL,              -- templates/<id>.txt stem
    template_hash  TEXT NOT NULL,              -- hash the image was generated / imported under
    path           TEXT NOT NULL,              -- relative to DATA_DIR, e.g. full/wlop/1girl.png
    thumb_path     TEXT NOT NULL,              -- relative to DATA_DIR, e.g. thumbs/wlop/1girl.webp
    seed           INTEGER,
    source         TEXT NOT NULL CHECK (source IN ('generated', 'imported')),
    params_match   INTEGER NOT NULL DEFAULT 1,
    mismatch_note  TEXT,
    meta           TEXT,                       -- json, see module docstring
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (artist_id, template_id)
);
CREATE INDEX IF NOT EXISTS images_template ON images(template_id);

CREATE TABLE IF NOT EXISTS ratings (
    artist_id    INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    template_id  TEXT,                         -- NULL = rating for the whole artist row
    value        INTEGER NOT NULL CHECK (value BETWEEN -1 AND 2),
    note         TEXT,
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
-- SQLite treats NULLs as distinct in UNIQUE, so the artist-level rating needs its own index.
CREATE UNIQUE INDEX IF NOT EXISTS ratings_cell ON ratings(artist_id, template_id) WHERE template_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ratings_artist ON ratings(artist_id) WHERE template_id IS NULL;

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY,
    artist_id    INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    template_id  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'running', 'done', 'error', 'skipped')),
    error        TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    started_at   TEXT,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, id);

-- User-defined style labels ("painterly", "dark", ...), free text, normalized by normalize_label().
CREATE TABLE IF NOT EXISTS artist_labels (
    artist_id  INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    label      TEXT NOT NULL,
    PRIMARY KEY (artist_id, label)
);
CREATE INDEX IF NOT EXISTS artist_labels_label ON artist_labels(label);

-- Reference images: the artist's top-scored booru posts (refs.py). Single artists only.
CREATE TABLE IF NOT EXISTS refs (
    id          INTEGER PRIMARY KEY,
    artist_id   INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    source      TEXT NOT NULL DEFAULT 'gelbooru',
    post_id     INTEGER NOT NULL,
    rank        INTEGER NOT NULL,              -- 0 = best score
    score       INTEGER,
    rating      TEXT,                          -- general / sensitive / questionable / explicit
    width       INTEGER,
    height      INTEGER,
    path        TEXT NOT NULL,                 -- relative to DATA_DIR, e.g. refs/full/wlop/123.jpg
    thumb_path  TEXT NOT NULL,                 -- relative to DATA_DIR, e.g. refs/thumbs/wlop/123.webp
    post_url    TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (artist_id, source, post_id)
);

-- One row per artist once refs were fetched: remembers "found nothing" and the search override.
CREATE TABLE IF NOT EXISTS ref_fetches (
    artist_id   INTEGER PRIMARY KEY REFERENCES artists(id) ON DELETE CASCADE,
    override    TEXT,                          -- booru tag searched instead of the derived one
    query       TEXT,                          -- full search string last sent
    fetched_at  TEXT,
    found       INTEGER,                       -- refs kept by the last successful fetch
    error       TEXT
);
"""

COMBO_SEP = ", "


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) and migrate. Row factory is sqlite3.Row."""
    path = path or config.DB_FILE
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit; use explicit BEGIN when needed
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    with conn:
        conn.execute("BEGIN")
        conn.executescript(SCHEMA)
        # The baseline row: tag '' / slug '_base'. Created here, never deleted.
        conn.execute(
            "INSERT OR IGNORE INTO artists (tag, slug, is_combo, note) VALUES ('', ?, 0, 'artist-less baseline')",
            (config.BASE_SLUG,),
        )
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


# --- artists --------------------------------------------------------------------

def artist_tags(row: sqlite3.Row | dict) -> list[str]:
    """Inverse of the `tag` column: [] for the baseline, one entry per artist for combos."""
    tag = row["tag"]
    return [t for t in tag.split(COMBO_SEP)] if tag else []


def get_artist_by_slug(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM artists WHERE slug = ?", (slug,)).fetchone()


def get_artist(conn: sqlite3.Connection, artist_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM artists WHERE id = ?", (artist_id,)).fetchone()


def get_or_create_artist(conn: sqlite3.Connection, tags: list[str]) -> sqlite3.Row:
    """[] -> the baseline row. Tags are trimmed; the slug is the identity, the first-seen spelling is kept."""
    tags = [t.strip() for t in tags if t.strip()]
    slug = config.combo_slug(tags)  # validates, rejects '_base' lookalikes
    row = get_artist_by_slug(conn, slug)
    if row is None:
        conn.execute(
            "INSERT INTO artists (tag, slug, is_combo) VALUES (?, ?, ?)",
            (COMBO_SEP.join(tags), slug, int(len(tags) > 1)),
        )
        row = get_artist_by_slug(conn, slug)
    assert row is not None
    return row


def list_artists(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Baseline first, then by tag."""
    return conn.execute("SELECT * FROM artists ORDER BY (slug != ?), tag COLLATE NOCASE", (config.BASE_SLUG,)).fetchall()


# --- images ---------------------------------------------------------------------

def rel_path(p: Path) -> str:
    """Store paths relative to DATA_DIR, forward slashes."""
    return Path(p).resolve().relative_to(config.DATA_DIR).as_posix()


def abs_path(rel: str) -> Path:
    return config.DATA_DIR / rel


def get_image(conn: sqlite3.Connection, artist_id: int, template_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM images WHERE artist_id = ? AND template_id = ?", (artist_id, template_id)
    ).fetchone()


def upsert_image(
    conn: sqlite3.Connection,
    *,
    artist_id: int,
    template_id: str,
    template_hash: str,
    path: str,
    thumb_path: str,
    seed: int | None,
    source: str,
    params_match: bool,
    mismatch_note: str | None,
    meta: dict[str, Any] | None,
) -> sqlite3.Row:
    conn.execute(
        """
        INSERT INTO images (artist_id, template_id, template_hash, path, thumb_path, seed, source,
                            params_match, mismatch_note, meta)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (artist_id, template_id) DO UPDATE SET
            template_hash = excluded.template_hash,
            path          = excluded.path,
            thumb_path    = excluded.thumb_path,
            seed          = excluded.seed,
            source        = excluded.source,
            params_match  = excluded.params_match,
            mismatch_note = excluded.mismatch_note,
            meta          = excluded.meta,
            created_at    = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        """,
        (
            artist_id, template_id, template_hash, path, thumb_path, seed, source,
            int(params_match), mismatch_note, json.dumps(meta, ensure_ascii=False) if meta is not None else None,
        ),
    )
    row = get_image(conn, artist_id, template_id)
    assert row is not None
    return row


def delete_image(conn: sqlite3.Connection, image_id: int) -> None:
    conn.execute("DELETE FROM images WHERE id = ?", (image_id,))


def list_images(conn: sqlite3.Connection, artist_id: int | None = None, template_id: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM images WHERE 1=1"
    args: list[Any] = []
    if artist_id is not None:
        sql += " AND artist_id = ?"
        args.append(artist_id)
    if template_id is not None:
        sql += " AND template_id = ?"
        args.append(template_id)
    sql += " ORDER BY artist_id, template_id"
    return conn.execute(sql, args).fetchall()


def image_meta(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["meta"]) if row["meta"] else {}


def list_images_with_hash(conn: sqlite3.Connection) -> dict[tuple[int, str], sqlite3.Row]:
    """(artist_id, template_id) -> image row, for the matrix."""
    return {(r["artist_id"], r["template_id"]): r for r in conn.execute("SELECT * FROM images")}


def get_image_by_id(conn: sqlite3.Connection, image_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()


# --- ratings --------------------------------------------------------------------

def set_rating(conn: sqlite3.Connection, artist_id: int, template_id: str | None, value: int, note: str | None) -> None:
    if not -1 <= value <= 2:
        raise ValueError("rating value must be between -1 and 2")
    if template_id is None:
        conn.execute(
            """INSERT INTO ratings (artist_id, template_id, value, note) VALUES (?, NULL, ?, ?)
               ON CONFLICT (artist_id) WHERE template_id IS NULL DO UPDATE SET
               value = excluded.value, note = excluded.note, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')""",
            (artist_id, value, note),
        )
    else:
        conn.execute(
            """INSERT INTO ratings (artist_id, template_id, value, note) VALUES (?, ?, ?, ?)
               ON CONFLICT (artist_id, template_id) WHERE template_id IS NOT NULL DO UPDATE SET
               value = excluded.value, note = excluded.note, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')""",
            (artist_id, template_id, value, note),
        )


def delete_rating(conn: sqlite3.Connection, artist_id: int, template_id: str | None) -> None:
    if template_id is None:
        conn.execute("DELETE FROM ratings WHERE artist_id = ? AND template_id IS NULL", (artist_id,))
    else:
        conn.execute("DELETE FROM ratings WHERE artist_id = ? AND template_id = ?", (artist_id, template_id))


def list_ratings(conn: sqlite3.Connection) -> dict[tuple[int, str | None], sqlite3.Row]:
    return {(r["artist_id"], r["template_id"]): r for r in conn.execute("SELECT * FROM ratings")}


# --- jobs -----------------------------------------------------------------------

PENDING = ("queued", "running")
_JOB_SELECT = "SELECT j.*, a.slug, a.tag FROM jobs j JOIN artists a ON a.id = j.artist_id"


def enqueue_job(conn: sqlite3.Connection, artist_id: int, template_id: str) -> sqlite3.Row:
    cur = conn.execute("INSERT INTO jobs (artist_id, template_id) VALUES (?, ?)", (artist_id, template_id))
    return get_job(conn, cur.lastrowid)


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute(f"{_JOB_SELECT} WHERE j.id = ?", (job_id,)).fetchone()


def next_queued_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(f"{_JOB_SELECT} WHERE j.status = 'queued' ORDER BY j.id LIMIT 1").fetchone()


def pending_cells(conn: sqlite3.Connection) -> dict[tuple[int, str], sqlite3.Row]:
    """(artist_id, template_id) -> the queued/running job for that cell."""
    rows = conn.execute(f"{_JOB_SELECT} WHERE j.status IN ('queued', 'running') ORDER BY j.id").fetchall()
    return {(r["artist_id"], r["template_id"]): r for r in rows}


def set_job_status(conn: sqlite3.Connection, job_id: int, status: str, error: str | None = None) -> None:
    now = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
    if status == "running":
        conn.execute(f"UPDATE jobs SET status = ?, started_at = {now} WHERE id = ?", (status, job_id))
    elif status in ("done", "error", "skipped"):
        conn.execute(f"UPDATE jobs SET status = ?, error = ?, finished_at = {now} WHERE id = ?", (status, error, job_id))
    else:
        conn.execute("UPDATE jobs SET status = ?, error = ? WHERE id = ?", (status, error, job_id))


def list_jobs(conn: sqlite3.Connection, finished_limit: int = 50) -> list[sqlite3.Row]:
    """Every pending job plus the most recent finished ones, oldest first."""
    return conn.execute(
        f"""{_JOB_SELECT} WHERE j.status IN ('queued', 'running')
            OR j.id IN (SELECT id FROM jobs WHERE status NOT IN ('queued', 'running') ORDER BY id DESC LIMIT ?)
            ORDER BY j.id""",
        (finished_limit,),
    ).fetchall()


def job_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {s: 0 for s in ("queued", "running", "done", "error", "skipped")}
    for r in conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"):
        counts[r["status"]] = r["n"]
    return counts


def clear_queued_jobs(conn: sqlite3.Connection) -> int:
    return conn.execute("DELETE FROM jobs WHERE status = 'queued'").rowcount


def fail_jobs(conn: sqlite3.Connection, status: str, error: str) -> int:
    """On startup: every 'running' / 'queued' job becomes an error. Never silently re-run them (no generation on startup)."""
    return conn.execute(
        "UPDATE jobs SET status = 'error', error = ?, finished_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE status = ?",
        (error, status),
    ).rowcount


# --- labels ---------------------------------------------------------------------

LABEL_MAX = 40


def normalize_label(s: str) -> str:
    """Lowercase, whitespace collapsed. Commas are rejected: the UI and the URL use them as separators."""
    label = " ".join(str(s).strip().lower().split())
    if not label or len(label) > LABEL_MAX or "," in label:
        raise ValueError(f"invalid label {s!r}: empty, over {LABEL_MAX} chars, or contains a comma")
    return label


def set_labels(conn: sqlite3.Connection, artist_id: int, labels: list[str]) -> list[str]:
    """Replace the artist's label set. Returns it normalized and sorted."""
    clean = sorted({normalize_label(x) for x in labels})
    with conn:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM artist_labels WHERE artist_id = ?", (artist_id,))
        conn.executemany("INSERT INTO artist_labels (artist_id, label) VALUES (?, ?)", [(artist_id, x) for x in clean])
    return clean


def list_labels(conn: sqlite3.Connection) -> dict[int, list[str]]:
    """artist_id -> sorted labels."""
    out: dict[int, list[str]] = {}
    for r in conn.execute("SELECT artist_id, label FROM artist_labels ORDER BY label"):
        out.setdefault(r["artist_id"], []).append(r["label"])
    return out


def label_counts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every label in use with its artist count, most used first."""
    rows = conn.execute("SELECT label, COUNT(*) AS n FROM artist_labels GROUP BY label ORDER BY n DESC, label")
    return [{"label": r["label"], "count": r["n"]} for r in rows]


# --- refs -----------------------------------------------------------------------

def list_refs(conn: sqlite3.Connection, artist_id: int | None = None) -> dict[int, list[sqlite3.Row]]:
    """artist_id -> ref rows, best score first."""
    sql, args = "SELECT * FROM refs", ()
    if artist_id is not None:
        sql, args = sql + " WHERE artist_id = ?", (artist_id,)
    out: dict[int, list[sqlite3.Row]] = {}
    for r in conn.execute(sql + " ORDER BY artist_id, rank", args):
        out.setdefault(r["artist_id"], []).append(r)
    return out


def replace_refs(conn: sqlite3.Connection, artist_id: int, rows: list[dict[str, Any]], source: str = "gelbooru") -> list[sqlite3.Row]:
    """Swap the artist's refs for `rows`. Returns the old rows (the caller deletes files no longer used)."""
    old = conn.execute("SELECT * FROM refs WHERE artist_id = ?", (artist_id,)).fetchall()
    cols = ("post_id", "rank", "score", "rating", "width", "height", "path", "thumb_path", "post_url")
    with conn:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM refs WHERE artist_id = ?", (artist_id,))
        conn.executemany(
            f"INSERT INTO refs (artist_id, source, {', '.join(cols)}) VALUES (?, ?, {', '.join('?' * len(cols))})",
            [(artist_id, source, *(r.get(c) for c in cols)) for r in rows],
        )
    return old


def get_ref_fetch(conn: sqlite3.Connection, artist_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM ref_fetches WHERE artist_id = ?", (artist_id,)).fetchone()


def list_ref_fetches(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    return {r["artist_id"]: r for r in conn.execute("SELECT * FROM ref_fetches")}


def set_ref_fetch(conn: sqlite3.Connection, artist_id: int, *, query: str, found: int | None, error: str | None) -> None:
    """Record a fetch attempt; keeps the override. found=None (a failed search) keeps the previous count."""
    conn.execute(
        """INSERT INTO ref_fetches (artist_id, query, fetched_at, found, error)
           VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), ?, ?)
           ON CONFLICT (artist_id) DO UPDATE SET query = excluded.query, fetched_at = excluded.fetched_at,
               found = COALESCE(excluded.found, ref_fetches.found), error = excluded.error""",
        (artist_id, query, found, error),
    )


def set_ref_override(conn: sqlite3.Connection, artist_id: int, override: str | None) -> None:
    conn.execute(
        """INSERT INTO ref_fetches (artist_id, override) VALUES (?, ?)
           ON CONFLICT (artist_id) DO UPDATE SET override = excluded.override""",
        (artist_id, override or None),
    )

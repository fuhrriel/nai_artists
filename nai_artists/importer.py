"""Register PNGs as matrix cells (CLI `import` / `template-from-image`, /api/import).

`register_image` is the one path by which a PNG becomes a cell: copy the original untouched
into full/<slug>/<template>.png (metadata intact), make the thumb, check params against
settings.toml, upsert the row. The CLI `import`, `template-from-image`, and later the server
queue all go through it.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config, db
from .match import MatchResult, match
from .meta import base_caption, char_captions, read_comment, read_text_chunks
from .templates import Settings, Template
from .thumbs import make_thumb

# Fixed request-body values (nai.build_body) that the Comment echoes back.
FIXED_PARAMS = {"cfg_rescale": 0.0, "uncond_scale": 0.0, "sm": False, "sm_dyn": False,
                "dynamic_thresholding": False, "prefer_brownian": True}
# Comment keys kept in images.meta. No prompt, no uc, no v4_*: prompts never go in SQLite.
META_KEYS = ("seed", "width", "height", "steps", "scale", "sampler", "noise_schedule", "cfg_rescale",
             "uncond_scale", "sm", "sm_dyn", "dynamic_thresholding", "prefer_brownian", "model_name", "model_hash")


class ImportError_(Exception):
    pass


@dataclass
class ImportResult:
    file: Path
    status: str  # imported | replaced | exists | no_match | no_metadata | dry_run
    template_id: str | None = None
    artists: list[str] = field(default_factory=list)
    slug: str | None = None
    exact: bool = False
    params_match: bool | None = None
    mismatch_note: str | None = None
    diffs: list[str] = field(default_factory=list)
    dest: Path | None = None
    inbox: Path | None = None

    def line(self) -> str:
        who = " + ".join(self.artists) or "(base)"
        if self.status in ("imported", "replaced", "exists", "dry_run"):
            pm = "ok" if self.params_match else f"MISMATCH: {self.mismatch_note}"
            how = "exact" if self.exact else "forced"
            return f"{self.status:<9} {self.file.name} -> {self.template_id} x {who} [{how}] params {pm}"
        if self.status == "no_match":
            closest = f"closest: {self.template_id}" if self.template_id else "no templates"
            return f"{self.status:<9} {self.file.name} ({who}) {closest} -> {self.inbox}"
        return f"{self.status:<9} {self.file.name} -> {self.inbox}"


def params_check(settings: Settings, comment: dict[str, Any]) -> tuple[bool, str | None]:
    """Compare the image's generation params with settings.toml. Returns (match, note)."""
    notes = []
    w, h = comment.get("width"), comment.get("height")
    if (w, h) != (settings.width, settings.height):
        notes.append(f"size {w}x{h} != {settings.width}x{settings.height}")
    for key in ("steps", "scale", "sampler", "noise_schedule", "seed"):
        want, got = getattr(settings, key), comment.get(key)
        if isinstance(want, float):
            same = got is not None and abs(float(got) - want) < 1e-6
        else:
            same = got == want
        if not same:
            notes.append(f"{key} {got!r} != {want!r}")
    uc = comment.get("uc")
    if uc is not None and uc != settings.negative:
        notes.append("negative prompt differs")
    for key, want in FIXED_PARAMS.items():
        if key in comment and comment[key] != want:
            notes.append(f"{key} {comment[key]!r} != {want!r}")
    return (not notes), ("; ".join(notes) if notes else None)


def slim_meta(comment: dict[str, Any], chunks: dict[str, str]) -> dict[str, Any]:
    meta = {k: comment[k] for k in META_KEYS if k in comment}
    for k in ("Source", "Software", "Generation time", "Generation_time"):
        if k in chunks:
            meta[k.replace(" ", "_")] = chunks[k]
    return meta


def cell_paths(slug: str, template_id: str) -> tuple[Path, Path]:
    return config.FULL_DIR / slug / f"{template_id}.png", config.THUMBS_DIR / slug / f"{template_id}.webp"


def _same_file(a: Path, b: Path) -> bool:
    return b.exists() and a.resolve() == b.resolve()


def register_image(
    conn: sqlite3.Connection,
    settings: Settings,
    template: Template,
    artists: list[str],
    src: Path,
    comment: dict[str, Any],
    *,
    source: str = "imported",
    exact: bool = True,
    replace: bool = False,
    extra_note: str | None = None,
) -> ImportResult:
    """Make `src` the cell (artists x template). Copies the file unless it already is the cell file."""
    artist = db.get_or_create_artist(conn, artists)
    slug = artist["slug"]
    dest, thumb = cell_paths(slug, template.id)
    existing = db.get_image(conn, artist["id"], template.id)
    pm, note = params_check(settings, comment)
    if not exact:
        pm = False
        note = "; ".join(n for n in ("prompt differs from template", extra_note, note) if n)
    result = ImportResult(src, "imported", template.id, list(artists), slug, exact, pm, note, dest=dest)
    # A cell "exists" if the DB has it OR the file is already on disk (e.g. generated before the DB was
    # created). Never clobber either without replace=True.
    on_disk = dest.exists() and not _same_file(src, dest)
    if (existing is not None or on_disk) and not replace:
        result.status = "exists"
        return result
    if not _same_file(src, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)  # bytes untouched: tEXt chunks and stealth alpha survive
    make_thumb(dest, thumb)
    db.upsert_image(
        conn,
        artist_id=artist["id"],
        template_id=template.id,
        template_hash=template.hash,
        path=db.rel_path(dest),
        thumb_path=db.rel_path(thumb),
        seed=comment.get("seed"),
        source=source,
        params_match=pm,
        mismatch_note=note,
        meta=slim_meta(comment, read_text_chunks(dest)),
    )
    result.status = "replaced" if (existing is not None or on_disk) else "imported"
    return result


def to_inbox(src: Path) -> Path:
    """Unmatched files go to data/inbox/ untouched (no-op if src already lives there)."""
    config.INBOX_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.INBOX_DIR / src.name
    if _same_file(src, dest):
        return dest
    i = 1
    while dest.exists():
        dest = config.INBOX_DIR / f"{src.stem}~{i}{src.suffix}"
        i += 1
    shutil.copyfile(src, dest)
    return dest


def match_file(src: Path, templates: list[Template]) -> tuple[dict[str, Any] | None, MatchResult | None]:
    comment = read_comment(src)
    if comment is None:
        return None, None
    return comment, match(base_caption(comment), char_captions(comment), templates)


def import_png(
    conn: sqlite3.Connection,
    settings: Settings,
    templates: list[Template],
    src: Path,
    *,
    force_template: Template | None = None,
    replace: bool = False,
    dry_run: bool = False,
) -> ImportResult:
    src = Path(src)
    comment, m = match_file(src, templates)
    if comment is None or m is None:
        return ImportResult(src, "no_metadata", inbox=None if dry_run else to_inbox(src))
    if m.exact:
        target, exact, extra = m.template, True, None
    elif force_template is not None:
        target, exact, extra = force_template, False, None
        if m.template is not None and m.template.id == force_template.id and m.diffs:
            extra = " | ".join(m.diffs)
    else:
        r = ImportResult(src, "no_match", m.template_id, m.artists, exact=False, diffs=m.diffs)
        if not dry_run:
            r.inbox = to_inbox(src)
        return r
    if dry_run:
        pm, note = params_check(settings, comment)
        if not exact:
            pm, note = False, "prompt differs from template" + (f"; {note}" if note else "")
        return ImportResult(src, "dry_run", target.id, m.artists, config.combo_slug(m.artists), exact, pm, note, diffs=m.diffs)
    return register_image(conn, settings, target, m.artists, src, comment, exact=exact, replace=replace, extra_note=extra)

"""Artist packs: hand out generated cells (picked artists x templates) as one zip, import someone else's.

Layout, every member stored (PNGs don't compress):

    templates/<id>.txt                  rendered from the loaded template: primary false, enabled, no sort
    images/<slug>/<template_id>.png     the cell PNG byte for byte, NovelAI metadata intact
    manifest.json                       written last: format, settings, sha256 of every other member

Never included: thumbs (rebuilt on import), refs, labels, ratings, notes, jobs, the DB.

A pack is untrusted input. The importer reads the zip in place, member by member, and checks the whole
structure on every call before writing anything: the entry set must be exactly what the manifest implies,
no links / special entries / encryption, sizes capped and counted while reading (headers can lie). Slugs
are recomputed from the manifest's tags, and each PNG's own prompt must agree with the manifest before
it becomes a cell. Templates are matched by content (what the matcher compares), not by id.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import stat
import uuid
import zipfile
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from . import __version__, config, db
from .importer import FORCED_NOTE, cell_paths, register_image
from .match import norm_chars, normalize, strip_artists
from .meta import base_caption, char_captions, read_comment
from .templates import (
    TEMPLATE_ID,
    CharCaption,
    Settings,
    Template,
    TemplateError,
    _parse_chars,
    split_front_matter,
    template_text,
)

FORMAT = 1
MANIFEST = "manifest.json"
MAX_ENTRIES = 100_000
MAX_PNG_BYTES = 64 << 20
MAX_TEMPLATE_BYTES = 1 << 20
MAX_MANIFEST_BYTES = 64 << 20
MAX_TOTAL_BYTES = 64 << 30
MAX_RATIO = 100  # uncompressed / compressed per member; our own packs are stored (1:1)
MAX_PIXELS = 4096 * 4096
MAX_TAG_LEN = 200
CHUNK = 1 << 20
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PackError(ValueError):
    pass


def content_key(body: str, chars) -> str:
    """A template's identity across installs: normalized body + chars (what match.py compares), hashed.
    `chars` may be CharCaption objects or NAI char_caption dicts."""
    blob = json.dumps([normalize(body), norm_chars(chars)], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _by_content(templates: list[Template]) -> dict[str, Template]:
    """content key -> the first template (in load order) with that content."""
    out: dict[str, Template] = {}
    for t in templates:
        out.setdefault(content_key(t.body, t.chars), t)
    return out


# --- export ---------------------------------------------------------------------

@dataclass
class ExportCell:
    slug: str
    tags: list[str]
    template: Template
    path: Path
    size: int

    @property
    def member(self) -> str:
        return f"images/{self.slug}/{self.template.id}.png"


@dataclass
class ExportPlan:
    templates: list[Template]
    cells: list[ExportCell]
    skipped: list[dict[str, str]]  # {slug, template, reason}: missing | stale | forced | file missing | bad tag

    def summary(self) -> dict[str, Any]:
        reasons: dict[str, int] = {}
        for s in self.skipped:
            reasons[s["reason"]] = reasons.get(s["reason"], 0) + 1
        artists = {c.slug for c in self.cells if c.slug != config.BASE_SLUG}
        return {
            "templates": [t.id for t in self.templates], "artists": len(artists), "cells": len(self.cells),
            "base_cells": sum(c.slug == config.BASE_SLUG for c in self.cells),
            "bytes": sum(c.size for c in self.cells), "skipped": self.skipped, "skipped_counts": reasons,
        }


def export_plan(conn: sqlite3.Connection, templates: list[Template], slugs: set[str], template_ids: list[str]) -> ExportPlan:
    """Picked artists (+ the baseline, always) x the given templates. Only fresh cells go in: current
    template hash, file on disk, not force-assigned. Everything else is listed in `skipped`."""
    by_id = {t.id: t for t in templates}
    ids = list(dict.fromkeys(template_ids))
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise PackError(f"no such template: {', '.join(unknown)}")
    if not ids:
        raise PackError("pick at least one template")
    chosen = [by_id[i] for i in ids]
    images = db.list_images_with_hash(conn)
    cells, skipped = [], []
    for a in db.list_artists(conn):
        if a["slug"] != config.BASE_SLUG and a["slug"] not in slugs:
            continue
        tags = db.artist_tags(a)
        try:
            portable = config.combo_slug(tags) == a["slug"]
        except ValueError:
            portable = False
        for t in chosen:
            row = images.get((a["id"], t.id))
            reason, size = None, 0
            if not portable:  # e.g. a tag with a comma: the importer rebuilds the slug from the tags and would refuse the pack
                reason = "bad tag"
            elif row is None:
                reason = "missing"
            elif row["template_hash"] != t.hash:
                reason = "stale"
            elif (row["mismatch_note"] or "").startswith(FORCED_NOTE):
                reason = "forced"
            else:
                try:
                    size = db.abs_path(row["path"]).stat().st_size
                except OSError:
                    reason = "file missing"
            if reason:
                skipped.append({"slug": a["slug"], "template": t.id, "reason": reason})
            else:
                cells.append(ExportCell(a["slug"], tags, t, db.abs_path(row["path"]), size))
    return ExportPlan(chosen, cells, skipped)


def file_name(name: str) -> str:
    """Download file name: ASCII, no quotes or separators."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return f"{safe or 'artists'}.zip"


class _Sink:
    """Write-only target for a streamed zip. It can't tell() or seek(), so zipfile writes data
    descriptors after each member and we hand the bytes out as they come."""

    def __init__(self):
        self.buf = bytearray()

    def write(self, b) -> int:
        self.buf += b
        return len(b)

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        out = bytes(self.buf)
        self.buf.clear()
        return out


def _zinfo(name: str, when: datetime) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(name, date_time=when.timetuple()[:6])
    zi.compress_type = zipfile.ZIP_STORED
    zi.external_attr = (stat.S_IFREG | 0o644) << 16
    return zi


def stream_pack(plan: ExportPlan, settings: Settings, name: str) -> Iterator[bytes]:
    """The zip as a byte stream: templates, then the PNGs, then the manifest with their sha256s.
    A cell file that vanished since the plan was made is left out (and out of the manifest)."""
    now = datetime.now(timezone.utc)
    local = now.astimezone()  # zip entry times are local wall-clock time; the manifest says UTC
    sink = _Sink()
    m_templates, m_cells = [], []
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED) as zf:
        for t in plan.templates:
            data = template_text(t.name, t.body, t.chars).encode("utf-8")
            zf.writestr(_zinfo(f"templates/{t.id}.txt", local), data)
            m_templates.append({"id": t.id, "sha256": hashlib.sha256(data).hexdigest()})
        for c in plan.cells:
            h = hashlib.sha256()
            try:
                src = c.path.open("rb")  # before the member is started: a missing file writes nothing
            except OSError:
                continue
            with src, zf.open(_zinfo(c.member, local), "w") as dst:
                while chunk := src.read(CHUNK):
                    h.update(chunk)
                    dst.write(chunk)
                    if len(sink.buf) >= CHUNK:
                        yield sink.take()
            m_cells.append({"slug": c.slug, "tags": c.tags, "template": c.template.id, "sha256": h.hexdigest()})
            if sink.buf:
                yield sink.take()
        manifest = {
            "format": FORMAT, "tool": "nai_artists", "version": __version__, "name": name,
            "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "settings": settings.hash_material(),
            "templates": m_templates, "cells": m_cells,
        }
        zf.writestr(_zinfo(MANIFEST, local), json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8"))
    yield sink.take()  # the manifest and the central directory, written on close


# --- reading a pack ---------------------------------------------------------------

@dataclass(frozen=True)
class PackTemplate:
    id: str
    name: str
    body: str
    chars: tuple[CharCaption, ...]
    key: str  # content_key


@dataclass(frozen=True)
class PackCell:
    slug: str
    tags: tuple[str, ...]
    template: str
    sha256: str

    @property
    def member(self) -> str:
        return f"images/{self.slug}/{self.template}.png"


@dataclass
class Pack:
    path: Path
    zf: zipfile.ZipFile
    name: str
    created_at: str
    version: str
    settings: dict[str, Any]
    templates: dict[str, PackTemplate]  # manifest order
    cells: list[PackCell]
    sizes: dict[str, int] = field(default_factory=dict)  # member -> declared size

    def __enter__(self) -> Pack:
        return self

    def __exit__(self, *exc) -> None:
        self.zf.close()


def resolve_path(raw: str) -> Path:
    """What the user pasted: surrounding quotes (Windows "Copy as path") stripped, ~ expanded."""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    if not s:
        raise PackError("give the path of a pack .zip")
    p = Path(s).expanduser()
    if not p.is_file():
        raise PackError(f"no such file: {p}")
    return p


def open_pack(raw_path: str) -> Pack:
    """Open and fully validate a pack's structure, manifest and templates. PNG contents are checked per
    cell at import time. Raises PackError (a ValueError) on anything off; nothing is written."""
    path = resolve_path(raw_path)
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise PackError(f"{path.name} is not a zip file ({e})") from None
    try:
        return _load(path, zf)
    except (zipfile.BadZipFile, EOFError, OSError) as e:
        zf.close()
        raise PackError(f"{path.name}: damaged zip ({e})") from None
    except BaseException:
        zf.close()
        raise


def _read_member(zf: zipfile.ZipFile, name: str, cap: int, dest: Path | None = None) -> tuple[str, bytes]:
    """(sha256 hex, bytes) of one member, or write it to `dest` (then bytes is b""). Counts real bytes."""
    h = hashlib.sha256()
    n = 0
    parts: list[bytes] = []
    out = dest.open("wb") if dest is not None else None
    try:
        with zf.open(name) as src:
            while chunk := src.read(CHUNK):
                n += len(chunk)
                if n > cap:
                    raise PackError(f"{name}: larger than {cap} bytes")
                h.update(chunk)
                if out is not None:
                    out.write(chunk)
                else:
                    parts.append(chunk)
    finally:
        if out is not None:
            out.close()
    return h.hexdigest(), b"".join(parts)


def _str(v: Any, what: str, limit: int = 1000) -> str:
    if not isinstance(v, str) or len(v) > limit:
        raise PackError(f"manifest: {what} must be a string (at most {limit} characters)")
    return v


def _load(path: Path, zf: zipfile.ZipFile) -> Pack:
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise PackError(f"{len(infos)} entries; a pack holds at most {MAX_ENTRIES}")
    files: dict[str, zipfile.ZipInfo] = {}
    dirs: set[str] = set()
    total = 0
    for zi in infos:
        name = zi.filename
        if zi.flag_bits & 0x1:
            raise PackError(f"{name!r}: encrypted entries are not allowed")
        kind = stat.S_IFMT(zi.external_attr >> 16)  # 0 when the tool wrote no unix mode (or permissions only)
        if kind and kind not in (stat.S_IFREG, stat.S_IFDIR):
            raise PackError(f"{name!r}: links and special files are not allowed")
        if zi.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise PackError(f"{name!r}: unsupported compression")
        if name.endswith("/"):
            if zi.file_size:
                raise PackError(f"{name!r}: a directory with data")
            dirs.add(name)
            continue
        if name in files:
            raise PackError(f"{name!r}: duplicate entry")
        if zi.file_size > (1 << 20) and zi.file_size > zi.compress_size * MAX_RATIO:
            raise PackError(f"{name!r}: suspicious compression ratio")
        total += zi.file_size
        files[name] = zi
    if total > MAX_TOTAL_BYTES:
        raise PackError(f"{total} bytes uncompressed; a pack holds at most {MAX_TOTAL_BYTES}")
    if MANIFEST not in files:
        raise PackError("no manifest.json at the top of the zip (was it re-zipped inside a folder?)")

    _, raw = _read_member(zf, MANIFEST, MAX_MANIFEST_BYTES)
    try:
        m = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise PackError(f"manifest.json is not valid JSON ({e})") from None
    if not isinstance(m, dict):
        raise PackError("manifest.json must be an object")
    if m.get("format") != FORMAT:
        raise PackError(f"unsupported pack format {m.get('format')!r} (this version reads {FORMAT})")
    settings = m.get("settings", {})
    if not isinstance(settings, dict):
        raise PackError("manifest: settings must be an object")
    t_list, c_list = m.get("templates"), m.get("cells")
    if not isinstance(t_list, list) or not isinstance(c_list, list):
        raise PackError("manifest: templates and cells must be lists")

    t_sha: dict[str, str] = {}
    for t in t_list:
        if not isinstance(t, dict):
            raise PackError("manifest: every template must be an object")
        tid, sha = _str(t.get("id"), "template id", 100), _str(t.get("sha256"), "template sha256", 64)
        if not TEMPLATE_ID.match(tid):
            raise PackError(f"manifest: bad template id {tid!r}")
        if not _SHA256.match(sha):
            raise PackError(f"manifest: template {tid}: bad sha256")
        if tid in t_sha:
            raise PackError(f"manifest: template {tid} listed twice")
        t_sha[tid] = sha

    cells: list[PackCell] = []
    seen: set[tuple[str, str]] = set()
    for c in c_list:
        if not isinstance(c, dict):
            raise PackError("manifest: every cell must be an object")
        tags = c.get("tags")
        if not isinstance(tags, list) or not all(isinstance(x, str) and 0 < len(x) <= MAX_TAG_LEN for x in tags):
            raise PackError("manifest: cell tags must be a list of artist tags")
        try:
            slug = config.combo_slug([x.strip() for x in tags])
        except ValueError as e:
            raise PackError(f"manifest: {e}") from None
        if c.get("slug") != slug:
            raise PackError(f"manifest: cell slug {c.get('slug')!r} does not belong to tags {tags!r}")
        tid, sha = _str(c.get("template"), "cell template", 100), _str(c.get("sha256"), "cell sha256", 64)
        if tid not in t_sha:
            raise PackError(f"manifest: cell {slug} / {tid}: template not in the pack")
        if not _SHA256.match(sha):
            raise PackError(f"manifest: cell {slug} / {tid}: bad sha256")
        if (slug, tid) in seen:
            raise PackError(f"manifest: cell {slug} / {tid} listed twice")
        seen.add((slug, tid))
        cells.append(PackCell(slug, tuple(tags), tid, sha))

    expected = {MANIFEST} | {f"templates/{tid}.txt" for tid in t_sha} | {c.member for c in cells}
    stray = sorted(set(files) - expected)
    if stray:
        raise PackError(f"unexpected entries in the zip: {', '.join(repr(s) for s in stray[:5])}"
                        + (f" and {len(stray) - 5} more" if len(stray) > 5 else ""))
    missing = sorted(expected - set(files))
    if missing:
        raise PackError(f"listed in the manifest but not in the zip: {', '.join(missing[:5])}"
                        + (f" and {len(missing) - 5} more" if len(missing) > 5 else ""))
    ok_dirs = {"templates/", "images/"} | {f"images/{c.slug}/" for c in cells}
    if dirs - ok_dirs:
        raise PackError(f"unexpected entries in the zip: {', '.join(repr(d) for d in sorted(dirs - ok_dirs)[:5])}")
    for c in cells:
        if files[c.member].file_size > MAX_PNG_BYTES:
            raise PackError(f"{c.member}: larger than {MAX_PNG_BYTES} bytes")

    templates: dict[str, PackTemplate] = {}
    for tid, sha in t_sha.items():
        got, raw = _read_member(zf, f"templates/{tid}.txt", MAX_TEMPLATE_BYTES)
        if got != sha:
            raise PackError(f"templates/{tid}.txt: sha256 differs from the manifest (corrupt or tampered)")
        try:
            fm, body = split_front_matter(raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n"))
            chars = _parse_chars(fm.get("chars"))
        except (UnicodeDecodeError, TemplateError, TypeError, ValueError) as e:
            raise PackError(f"templates/{tid}.txt: {e}") from None
        if not body.strip():
            raise PackError(f"templates/{tid}.txt: empty prompt")
        name = str(fm.get("name", tid))[:200]
        templates[tid] = PackTemplate(tid, name, body, chars, content_key(body, chars))

    return Pack(path, zf, _str(m.get("name", path.stem), "name")[:200], _str(m.get("created_at", ""), "created_at", 100),
                _str(m.get("version", ""), "version", 100), settings, templates, cells,
                {n: zi.file_size for n, zi in files.items()})


# --- importing ----------------------------------------------------------------------

@dataclass
class TemplateAction:
    id: str        # the pack's id
    name: str
    action: str    # reuse | install | rename
    target: str    # the receiver's id
    enabled: bool = True


def plan_templates(pack: Pack, templates: list[Template]) -> list[TemplateAction]:
    """Reuse an equivalent template under any id; else install under the pack's id; on an id clash
    with a different body install as <id>-<6 hex of the content key>. Ids compare case-insensitively
    (Windows and macOS file systems do)."""
    have = _by_content(templates)
    taken = {t.id.lower() for t in templates}
    planned: dict[str, str] = {}  # content key -> target, for two pack templates with the same content
    out = []
    for pt in pack.templates.values():
        if pt.key in have:
            t = have[pt.key]
            out.append(TemplateAction(pt.id, pt.name, "reuse", t.id, t.enabled))
            continue
        if pt.key in planned:
            out.append(TemplateAction(pt.id, pt.name, "reuse", planned[pt.key]))
            continue
        target = pt.id if pt.id.lower() not in taken else f"{pt.id}-{pt.key[:6]}"
        if target.lower() in taken:
            raise PackError(f"template {pt.id}: {target} already exists with a different prompt; rename or move it")
        taken.add(target.lower())
        planned[pt.key] = target
        out.append(TemplateAction(pt.id, pt.name, "install" if target == pt.id else "rename", target))
    return out


def settings_diff(theirs: dict[str, Any], ours: dict[str, Any]) -> list[str]:
    return [k for k in sorted(set(theirs) | set(ours)) if theirs.get(k) != ours.get(k)]


def inspect(conn: sqlite3.Connection, settings: Settings, templates: list[Template], pack: Pack) -> dict[str, Any]:
    """The dry run: what the templates step would do, the settings difference, and per artist how many
    of its cells are new here. Writes nothing."""
    plan = plan_templates(pack, templates)
    target = {a.id: a.target for a in plan}
    known = {a["slug"]: a for a in db.list_artists(conn)}
    images = db.list_images_with_hash(conn)
    artists: dict[str, dict[str, Any]] = {}
    for c in pack.cells:
        a = artists.setdefault(c.slug, {"slug": c.slug, "tags": list(c.tags), "cells": 0, "existing": 0,
                                        "known": c.slug in known})
        a["cells"] += 1
        row = known.get(c.slug)
        dest, _ = cell_paths(c.slug, target[c.template])
        if (row is not None and (row["id"], target[c.template]) in images) or dest.exists():
            a["existing"] += 1
    rows = sorted(artists.values(), key=lambda a: a["slug"] != config.BASE_SLUG)  # stable: baseline first
    return {
        "path": str(pack.path), "name": pack.name, "created_at": pack.created_at, "version": pack.version,
        "settings_diff": settings_diff(pack.settings, settings.hash_material()),
        "templates": [asdict(a) for a in plan], "artists": rows, "cells": len(pack.cells),
        "existing": sum(a["existing"] for a in rows),
        "bytes": sum(pack.sizes[c.member] for c in pack.cells),
    }


def install_templates(pack: Pack, templates: list[Template]) -> list[TemplateAction]:
    """Write the templates the plan installs (primary false, enabled, name kept, no sort). Idempotent:
    a second run finds them by content and reuses them."""
    plan = plan_templates(pack, templates)
    config.TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    for a in plan:
        if a.action == "reuse":
            continue
        pt = pack.templates[a.id]
        path = config.TEMPLATES_DIR / f"{a.target}.txt"
        with path.open("x", encoding="utf-8", newline="\n") as fh:  # never clobbers, even in a race
            fh.write(template_text(pt.name, pt.body, pt.chars))
    return plan


@dataclass
class CellResult:
    template: str             # the pack's template id
    target: str | None        # the receiver's template id
    status: str               # imported | replaced | exists | rejected
    reason: str | None = None
    params_match: bool | None = None
    mismatch_note: str | None = None


def import_artist(conn: sqlite3.Connection, settings: Settings, templates: list[Template], pack: Pack, slug: str,
                  *, replace: bool = False) -> list[CellResult]:
    """Import one artist's cells (`_base` included). A cell that fails a check is `rejected` with a
    reason and nothing is written for it; the others go on."""
    cells = [c for c in pack.cells if c.slug == slug]
    if not cells:
        raise PackError(f"the pack has no artist {slug!r}")
    have = _by_content(templates)
    tmp = config.DATA_DIR / "tmp" / uuid.uuid4().hex
    tmp.mkdir(parents=True, exist_ok=True)
    out = []
    try:
        for c in cells:
            pt = pack.templates[c.template]
            target = have.get(pt.key)
            if target is None:
                out.append(CellResult(c.template, None, "rejected", "template not installed (run the templates step first)"))
                continue
            try:
                out.append(_import_cell(conn, settings, pack, c, pt, target, tmp / f"{c.template}.png", replace))
            except (PackError, OSError, ValueError, zipfile.BadZipFile, EOFError, Image.DecompressionBombError) as e:
                out.append(CellResult(c.template, target.id, "rejected", str(e)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def _import_cell(conn, settings: Settings, pack: Pack, c: PackCell, pt: PackTemplate, target: Template,
                 src: Path, replace: bool) -> CellResult:
    digest, _ = _read_member(pack.zf, c.member, MAX_PNG_BYTES, dest=src)
    if digest != c.sha256:
        raise PackError("sha256 differs from the manifest (corrupt or tampered)")
    with Image.open(src) as im:
        if im.format != "PNG":
            raise PackError(f"not a PNG ({im.format})")
        if im.width * im.height > MAX_PIXELS:
            raise PackError(f"{im.width}x{im.height} is larger than this tool accepts")
    comment = read_comment(src)
    if comment is None:
        raise PackError("no NovelAI metadata in the PNG")
    artists, body = strip_artists(base_caption(comment))
    if content_key(body, char_captions(comment)) != pt.key:
        raise PackError(f"its prompt does not match the pack's template {pt.id}")
    if config.combo_slug(artists) != c.slug:
        raise PackError(f"its prompt names {' + '.join(artists) or 'no artist'}, not {c.slug}")
    r = register_image(conn, settings, target, artists, src, comment, source="imported", exact=True, replace=replace)
    return CellResult(c.template, target.id, r.status, None, r.params_match, r.mismatch_note)

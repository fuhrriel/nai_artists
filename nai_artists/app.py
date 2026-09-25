"""FastAPI routes + static. `uv run nai serve` or `uvicorn nai_artists.app:app`.

Every route re-reads settings.toml and templates/ (they are small). DB: one sqlite connection
per request.

NovelAI's API docs: "all generation requests must be initiated by a human action". So: one request
to this app enqueues at most one generation job (POST /api/generate, one artist x one template), and
nothing else creates jobs. Adding artists only adds matrix rows. The queue never generates on startup.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db
from .importer import ImportResult, cell_paths, import_png, register_image
from .meta import read_comment
from .nai import NAIError, anlas_mode, anlas_short, battery_low
from .queue import Queue
from .refs import RefFetcher, eligible
from .templates import (
    Template,
    TemplateError,
    create_from_image,
    get_template,
    load_settings,
    load_templates,
    update_front_matter,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


# --- request bodies ----------------------------------------------------------

class AddArtistsBody(BaseModel):
    artists: list[str | list[str]] = Field(default_factory=list)  # "wlop", "artist:wlop", "a + b", or ["a", "b"]


class GenerateBody(BaseModel):
    """Exactly one cell. Missing / stale / file-gone cells are (re)generated in place; a fresh one only with replace."""
    artist: str  # slug, `_base` included
    template: str
    replace: bool = False  # fresh cell: delete the image first (the cell rating stays)
    force: bool = False


class ResumeBody(BaseModel):
    force: bool = False


class RatingBody(BaseModel):
    artist: str  # slug
    template: str | None = None
    value: int | None = None  # None deletes the rating
    note: str | None = None


class LabelsBody(BaseModel):
    labels: list[str] = Field(default_factory=list)  # replaces the artist's whole set


class RefsFetchBody(BaseModel):
    artists: list[str] | Literal["missing", "all"] = "missing"  # slugs; "missing" = never fetched
    override: str | None = None  # booru tag to search for (exactly one artist); "" clears it


class TemplatePatch(BaseModel):
    enabled: bool | None = None
    primary: bool | None = None
    sort: int | None = None
    clear_sort: bool = False
    name: str | None = None


# --- helpers ----------------------------------------------------------------

def _norm_artist(s: str) -> str:
    s = s.strip()
    return s[len("artist:"):].strip() if s.lower().startswith("artist:") else s


def parse_artist_entries(entries: list[str | list[str]]) -> list[list[str]]:
    """Each entry -> list of bare tags (a combo when >1). Blank entries are dropped; duplicates too."""
    out: list[list[str]] = []
    seen: set[str] = set()
    for e in entries:
        parts = e if isinstance(e, list) else e.split("+")
        tags = [_norm_artist(p) for p in parts if _norm_artist(p)]
        if not tags:
            continue
        slug = config.combo_slug(tags)  # ValueError -> 400 via handler
        if slug not in seen:
            seen.add(slug)
            out.append(tags)
    return out


def template_dict(t: Template) -> dict[str, Any]:
    return {"id": t.id, "name": t.name, "primary": t.primary, "enabled": t.enabled, "sort": t.sort,
            "hash": t.hash, "chars": len(t.chars), "path": str(t.path)}


def image_dict(row, template: Template | None, rating=None, job=None) -> dict[str, Any]:
    return {
        "image_id": row["id"], "path": row["path"], "thumb": row["thumb_path"], "seed": row["seed"],
        "source": row["source"], "params_match": bool(row["params_match"]), "mismatch_note": row["mismatch_note"],
        "template_hash": row["template_hash"],
        "stale": template is not None and row["template_hash"] != template.hash,
        "file_missing": not db.abs_path(row["path"]).is_file(),
        "created_at": row["created_at"], "meta": db.image_meta(row),
        "rating": rating_dict(rating), "job": job_dict(job) if job else None,
    }


def rating_dict(r) -> dict[str, Any] | None:
    return {"value": r["value"], "note": r["note"], "updated_at": r["updated_at"]} if r else None


def job_dict(j) -> dict[str, Any]:
    return dict(j)


def ref_dict(r) -> dict[str, Any]:
    return {k: r[k] for k in ("post_id", "rank", "score", "rating", "width", "height", "path", "thumb_path",
                              "post_url", "created_at")}


def ref_fetch_dict(r) -> dict[str, Any] | None:
    return {k: r[k] for k in ("override", "query", "fetched_at", "found", "error")} if r else None


def artist_extras(a, labels, refs, fetches) -> dict[str, Any]:
    """Labels + reference images of one artist row, shared by /api/matrix and /api/artists/{slug}."""
    return {"labels": labels.get(a["id"], []), "refs_eligible": eligible(a),
            "refs": [ref_dict(r) for r in refs.get(a["id"], [])], "refs_fetch": ref_fetch_dict(fetches.get(a["id"]))}


def import_result_dict(r: ImportResult) -> dict[str, Any]:
    return {
        "file": r.file.name, "status": r.status, "template_id": r.template_id, "artists": r.artists,
        "slug": r.slug, "exact": r.exact, "params_match": r.params_match, "mismatch_note": r.mismatch_note,
        "diffs": r.diffs, "path": db.rel_path(r.dest) if r.dest and r.dest.exists() else None,
        "inbox": db.rel_path(r.inbox) if r.inbox else None,
    }


async def battery_gate(q: Queue, settings, force: bool) -> dict[str, Any]:
    """Before a generation click / resume, fetch the subscription and refuse (409) when low unless forced.

    An empty battery with less than `anlas_per_image` Anlas is refused even when forced
    (`forceable: false`); the UI must not offer to override that one.
    """
    try:
        sub = await q.subscription_async(fresh=True)
    except NAIError as e:
        raise HTTPException(502, f"subscription check failed: {e}") from None
    except RuntimeError as e:  # no API key
        raise HTTPException(500, str(e)) from None
    b = settings.battery
    detail = {"battery_percent": sub["battery_percent"], "is_negative": sub["is_negative"], "anlas": sub["anlas"],
              "anlas_mode": anlas_mode(sub), "min_percent": b.min_percent, "anlas_per_image": b.anlas_per_image}
    if anlas_short(sub, b.anlas_per_image):
        raise HTTPException(409, {"error": "not enough anlas", "forceable": False, **detail})
    if battery_low(sub, b.min_percent) and not force:
        raise HTTPException(409, {"error": "battery low", "forceable": True, **detail})
    return sub


def delete_cell_files(row) -> None:
    for rel in (row["path"], row["thumb_path"]):
        p = db.abs_path(rel)
        if p.is_file():
            p.unlink()


# --- app --------------------------------------------------------------------

def create_app(client_factory=None, booru_factory=None) -> FastAPI:
    """client_factory / booru_factory: inject fake NovelAI / Gelbooru clients (tests)."""
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config.FULL_DIR.mkdir(parents=True, exist_ok=True)
        config.THUMBS_DIR.mkdir(parents=True, exist_ok=True)
        config.REFS_DIR.mkdir(parents=True, exist_ok=True)
        q = Queue(client_factory)
        app.state.queue = q
        q.refs = app.state.refs = RefFetcher(booru_factory, on_change=q.broadcast)
        q.start()
        q.refs.start()
        try:
            yield
        finally:
            await q.refs.stop()
            await q.stop()

    app = FastAPI(title="nai_artists", lifespan=lifespan)

    @app.exception_handler(TemplateError)
    async def _template_error(_: Request, e: TemplateError):
        return JSONResponse({"detail": str(e)}, status_code=400)

    @app.exception_handler(ValueError)
    async def _value_error(_: Request, e: ValueError):
        return JSONResponse({"detail": str(e)}, status_code=400)

    @app.exception_handler(NAIError)
    async def _nai_error(_: Request, e: NAIError):
        return JSONResponse({"detail": str(e), "status": e.status}, status_code=502)

    def queue(request: Request) -> Queue:
        return request.app.state.queue

    def artist_or_404(conn, slug: str):
        a = db.get_artist_by_slug(conn, slug)
        if a is None:
            raise HTTPException(404, "no such artist")
        return a

    # --- read side ---------------------------------------------------------

    @app.get("/api/matrix")
    async def matrix(all_templates: bool = Query(False, alias="all")):
        settings = load_settings()
        templates = [t for t in load_templates(settings) if all_templates or t.enabled]
        by_id = {t.id: t for t in templates}
        conn = db.connect()
        images = db.list_images_with_hash(conn)
        ratings = db.list_ratings(conn)
        pending = db.pending_cells(conn)
        labels, refs, fetches = db.list_labels(conn), db.list_refs(conn), db.list_ref_fetches(conn)
        artists = []
        for a in db.list_artists(conn):
            cells = {}
            for t in templates:
                key = (a["id"], t.id)
                img = images.get(key)
                if img is not None:
                    cells[t.id] = image_dict(img, t, ratings.get(key), pending.get(key))
                elif key in pending:
                    cells[t.id] = {"image_id": None, "job": job_dict(pending[key])}
            artists.append({
                "id": a["id"], "tag": a["tag"], "slug": a["slug"], "is_combo": bool(a["is_combo"]),
                "tags": db.artist_tags(a), "note": a["note"], "created_at": a["created_at"],
                "rating": rating_dict(ratings.get((a["id"], None))), "cells": cells,
                **artist_extras(a, labels, refs, fetches),
            })
        return {"templates": [template_dict(t) for t in templates], "artists": artists,
                "base_slug": config.BASE_SLUG, "queue": queue_summary(conn),
                "labels": db.label_counts(conn), "refs": app.state.refs.state()}

    def queue_summary(conn) -> dict[str, Any]:
        q: Queue = app.state.queue
        return {"paused": q.paused, "pause_reason": q.pause_reason, "forced": q.forced, "current": q.current,
                "waiting": q.waiting, "counts": db.job_counts(conn)}

    @app.get("/api/artists/{slug}")
    async def artist(slug: str):
        settings = load_settings()
        templates = {t.id: t for t in load_templates(settings)}
        conn = db.connect()
        a = artist_or_404(conn, slug)
        ratings = db.list_ratings(conn)
        pending = db.pending_cells(conn)
        cells = {r["template_id"]: image_dict(r, templates.get(r["template_id"]), ratings.get((a["id"], r["template_id"])),
                                              pending.get((a["id"], r["template_id"])))
                 for r in db.list_images(conn, artist_id=a["id"])}
        return {"id": a["id"], "tag": a["tag"], "slug": a["slug"], "is_combo": bool(a["is_combo"]),
                "tags": db.artist_tags(a), "note": a["note"], "created_at": a["created_at"],
                "rating": rating_dict(ratings.get((a["id"], None))), "cells": cells,
                **artist_extras(a, db.list_labels(conn), db.list_refs(conn, a["id"]), db.list_ref_fetches(conn))}

    @app.put("/api/artists/{slug}/labels")
    async def artist_labels(slug: str, body: LabelsBody):
        conn = db.connect()
        a = artist_or_404(conn, slug)
        if a["slug"] == config.BASE_SLUG:
            raise HTTPException(400, "the baseline row takes no labels")
        return {"slug": slug, "labels": db.set_labels(conn, a["id"], body.labels), "all": db.label_counts(conn)}

    # --- reference images (Gelbooru) ---------------------------------------

    @app.post("/api/refs/fetch")
    async def refs_fetch(request: Request, body: RefsFetchBody):
        """Queue artists for the background ref fetcher. Explicit user action only."""
        f: RefFetcher = request.app.state.refs
        if not f.configured:
            raise HTTPException(400, "Gelbooru is not configured: set GELBOORU_API_KEY and GELBOORU_USER_ID in .env")
        conn = db.connect()
        if isinstance(body.artists, list):
            rows = [artist_or_404(conn, s) for s in body.artists]
            bad = [r["slug"] for r in rows if not eligible(r)]
            if bad:
                raise HTTPException(400, f"refs are for single artists only, not {', '.join(bad)}")
        else:
            fetched = {i for i, r in db.list_ref_fetches(conn).items() if r["fetched_at"]}
            rows = [r for r in db.list_artists(conn)
                    if eligible(r) and (body.artists == "all" or r["id"] not in fetched)]
        if body.override is not None:
            if len(rows) != 1:
                raise HTTPException(400, "override needs exactly one artist")
            db.set_ref_override(conn, rows[0]["id"], body.override.strip())
        added = f.add([r["id"] for r in rows])
        return {"added": added, "refs": f.state()}

    @app.get("/api/images/{image_id}/meta")
    async def image_meta(image_id: int):
        """Full PNG Comment (prompt, uc, v4_*): read from the file, never from the DB."""
        row = db.get_image_by_id(db.connect(), image_id)
        if row is None:
            raise HTTPException(404, "no such image")
        path = db.abs_path(row["path"])
        if not path.is_file():
            raise HTTPException(410, f"{row['path']} is missing on disk")
        comment = await asyncio.to_thread(read_comment, path)
        return {"image_id": image_id, "path": row["path"], "comment": comment}

    @app.get("/api/templates")
    async def templates_list():
        settings = load_settings()
        conn = db.connect()
        images = db.list_images_with_hash(conn)
        out = []
        for t in load_templates(settings):
            rows = [r for (_, tid), r in images.items() if tid == t.id]
            d = template_dict(t)
            d["images"] = len(rows)
            d["stale"] = sum(1 for r in rows if r["template_hash"] != t.hash)
            out.append(d)
        return {"templates": out, "dir": str(config.TEMPLATES_DIR)}

    @app.get("/api/settings")
    async def settings_view():
        s = load_settings()
        return {"file": str(config.SETTINGS_FILE), "settings": s.raw, "hash_material": s.hash_material()}

    @app.get("/api/subscription")
    async def subscription(request: Request, fresh: bool = False):
        try:
            return await queue(request).subscription_async(fresh=fresh)
        except RuntimeError as e:
            raise HTTPException(500, str(e)) from None

    @app.get("/api/suggest")
    async def suggest(request: Request, q: str = Query(..., min_length=1), model: str | None = None):
        settings = load_settings()
        prompt = q if q.lower().startswith("artist:") else f"artist:{q}"
        tags = await asyncio.to_thread(queue(request).client.suggest_tags, prompt, model or settings.model)
        artists = [t for t in tags if str(t.get("tag", "")).lower().startswith("artist:")]
        return {"q": q, "tags": artists if artists else tags}

    # --- artists + generation ----------------------------------------------

    @app.post("/api/artists")
    async def add_artists(body: AddArtistsBody):
        """Matrix rows only: never enqueues a job, never talks to NovelAI. Their cells show up as missing."""
        combos = parse_artist_entries(body.artists)
        if not combos:
            raise HTTPException(400, "no artists given")
        conn = db.connect()
        added, existing = [], []
        for tags in combos:
            known = db.get_artist_by_slug(conn, config.combo_slug(tags)) is not None
            a = db.get_or_create_artist(conn, tags)
            (existing if known else added).append(a["slug"])
        return {"added": added, "existing": existing}

    @app.post("/api/generate")
    async def generate(request: Request, body: GenerateBody):
        """One click = one cell = at most one job. A cell that already has a pending job returns that job."""
        settings = load_settings()
        template = get_template(body.template, settings)
        q = queue(request)
        conn = db.connect()
        a = artist_or_404(conn, body.artist)
        pending = db.pending_cells(conn).get((a["id"], template.id))
        if pending is not None:
            return {"job": job_dict(pending), "created": False, "queue": queue_summary(conn)}
        row = db.get_image(conn, a["id"], template.id)
        fresh = row is not None and row["template_hash"] == template.hash and db.abs_path(row["path"]).is_file()
        if fresh and not body.replace:
            raise HTTPException(409, "the cell is up to date; send replace: true to regenerate it")
        dest, _thumb = cell_paths(a["slug"], template.id)
        if row is None and dest.exists():
            raise HTTPException(409, f"{db.rel_path(dest)} exists on disk but is not registered; import or delete it")
        sub = await battery_gate(q, settings, body.force)
        if fresh:
            db.delete_image(conn, row["id"])
            delete_cell_files(row)
        job = q.enqueue(conn, a["id"], template.id)
        if body.force:
            q.force()
        return {"job": job_dict(job), "created": True, "battery": sub, "queue": queue_summary(conn)}

    # --- queue -------------------------------------------------------------

    @app.get("/api/jobs")
    async def jobs(request: Request):
        return queue(request).state()

    @app.get("/api/jobs/stream")
    async def jobs_stream(request: Request):
        return StreamingResponse(queue(request).stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/jobs/pause")
    async def jobs_pause(request: Request):
        q = queue(request)
        q.pause()
        return q.state()

    @app.post("/api/jobs/resume")
    async def jobs_resume(request: Request, body: ResumeBody | None = None):
        q = queue(request)
        settings = load_settings()
        force = bool(body and body.force)
        await battery_gate(q, settings, force)
        q.resume(force)
        return q.state()

    @app.post("/api/jobs/clear")
    async def jobs_clear(request: Request):
        q = queue(request)
        n = q.clear(db.connect())
        return {"cleared": n, **q.state()}

    # --- import / templates ------------------------------------------------

    @app.post("/api/import")
    async def import_files(files: list[UploadFile] = File(...), template: str | None = Form(None),
                           replace: bool = Form(False), dry_run: bool = Form(False)):
        settings = load_settings()
        templates = load_templates(settings)
        force_t = get_template(template, settings) if template else None
        tmp = config.DATA_DIR / "tmp" / uuid.uuid4().hex
        tmp.mkdir(parents=True, exist_ok=True)
        results = []
        try:
            for up in files:
                name = Path(up.filename or "upload.png").name
                dest = tmp / name
                with dest.open("wb") as fh:
                    shutil.copyfileobj(up.file, fh)
                # sqlite connections are thread-bound: open it inside the worker thread
                r = await asyncio.to_thread(
                    lambda: import_png(db.connect(), settings, templates, dest,
                                       force_template=force_t, replace=replace, dry_run=dry_run)
                )
                results.append(import_result_dict(r))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return {"results": results}

    @app.post("/api/templates/from-image")
    async def template_from_image(id: str = Form(...), name: str | None = Form(None), primary: bool = Form(False),
                                  force: bool = Form(False), image_id: int | None = Form(None),
                                  file: UploadFile | None = File(None)):
        settings = load_settings()
        conn = db.connect()
        tmp: Path | None = None
        if image_id is not None:
            row = db.get_image_by_id(conn, image_id)
            if row is None:
                raise HTTPException(404, "no such image")
            src = db.abs_path(row["path"])
        elif file is not None:
            tmp = config.DATA_DIR / "tmp" / uuid.uuid4().hex
            tmp.mkdir(parents=True, exist_ok=True)
            src = tmp / Path(file.filename or "upload.png").name
            with src.open("wb") as fh:
                shutil.copyfileobj(file.file, fh)
        else:
            raise HTTPException(400, "give image_id or file")
        try:
            comment = await asyncio.to_thread(read_comment, src)
            if comment is None:
                raise HTTPException(400, "the image carries no NovelAI metadata")
            t, artists = create_from_image(comment, id, name or None, primary=primary, force=force, settings=settings)
            r = await asyncio.to_thread(
                lambda: register_image(db.connect(), settings, t, artists, src, comment,
                                       source="imported", exact=True, replace=force)
            )
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
        return {"template": template_dict(t), "artists": artists, "cell": import_result_dict(r)}

    @app.patch("/api/templates/{template_id}")
    async def template_patch(template_id: str, body: TemplatePatch):
        settings = load_settings()
        t = get_template(template_id, settings)
        changes: dict[str, Any] = {k: v for k, v in body.model_dump(exclude={"clear_sort"}).items() if v is not None}
        if body.clear_sort:
            changes["sort"] = None
        if not changes:
            raise HTTPException(400, "nothing to change")
        update_front_matter(t.path, changes)
        return template_dict(get_template(template_id, settings))

    # --- ratings -----------------------------------------------------------

    @app.put("/api/ratings")
    async def rate(body: RatingBody):
        conn = db.connect()
        a = artist_or_404(conn, body.artist)
        if a["slug"] == config.BASE_SLUG:
            raise HTTPException(400, "the baseline row is not ratable")
        if body.template is not None:
            get_template(body.template, load_settings())  # 400 if unknown
        if body.value is None:
            db.delete_rating(conn, a["id"], body.template)
            return {"artist": body.artist, "template": body.template, "rating": None}
        db.set_rating(conn, a["id"], body.template, body.value, body.note)
        r = db.list_ratings(conn)[(a["id"], body.template)]
        return {"artist": body.artist, "template": body.template, "rating": rating_dict(r)}

    # --- static ------------------------------------------------------------

    # check_dir=False: the dirs are created in lifespan, not at import time
    app.mount("/full", StaticFiles(directory=config.FULL_DIR, check_dir=False), name="full")
    app.mount("/thumbs", StaticFiles(directory=config.THUMBS_DIR, check_dir=False), name="thumbs")
    app.mount("/refs", StaticFiles(directory=config.REFS_DIR, check_dir=False), name="refs")

    @app.get("/", include_in_schema=False)
    async def index():
        page = STATIC_DIR / "index.html"
        if page.is_file():
            return FileResponse(page)
        return JSONResponse({"app": "nai_artists", "frontend": "missing", "api": "/docs"})

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app


app = create_app()

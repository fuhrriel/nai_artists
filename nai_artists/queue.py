"""Single asyncio worker over the `jobs` table.

Every job is one cell somebody clicked: the routes enqueue at most one job per request, the queue
only serializes them (NovelAI runs one generation at a time per account). One job at a time, in id
order. The network call and the PNG bookkeeping run in a thread (httpx is sync); pacing sleeps are
asyncio sleeps so pause / clear stay responsive.

State machine, all on the event loop thread:

- paused + pause_reason: set by the user, by a 429/402 (never retried), or by the battery guard
  (`battery: N%` / `anlas: …`). `resume()` clears it; the battery re-check on resume lives in app.py.
- startup: nothing generates on startup. Jobs left `running` or `queued` by the previous run become
  `error`: a click from a past session never fires a request in this one.
- forced: set by a forced resume / enqueue. While set the `min_percent` threshold is ignored, for as
  long as there is work queued; it drops when the queue runs dry, on any pause / suspension (resume
  sets it again from its own `force`), or on clear. The Anlas floor (`anlas_short`) is never forced.
- pacing: after every request `Pacer.next_delay()` sets `not_before` (a fixed `[pacing].gap_ms`, to keep
  load on NovelAI low); the next job waits for it even if it was clicked later.
- battery guard: every `[battery].check_every` generated images the subscription is re-fetched
  (sooner once paying in Anlas: `images_until_check`). Any fetch resets the counter. At/below
  `min_percent` (or negative) the queue suspends with the remaining jobs still queued, unless forced;
  with an empty battery and fewer than `anlas_per_image` Anlas it suspends even when forced.

Worker rules for a cell whose job comes up:
- image row with the current template hash and its file on disk -> `skipped` (already fresh)
- no row but the file exists on disk -> `skipped` (unregistered file; import or delete it)
- otherwise generate, write full/<slug>/<template>.png, register (replace=True: a stale cell is overwritten)
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from . import config, db
from .importer import ImportResult, cell_paths, register_image
from .meta import read_comment
from .nai import NAIClient, NAIError, anlas_short, battery_low, battery_summary, build_body, images_until_check
from .pacing import Pacer
from .templates import Settings, TemplateError, get_template, load_settings

SUBSCRIPTION_TTL = 60.0
SSE_PING_SECONDS = 15.0


class Skip(Exception):
    """The job needs no generation; recorded as status 'skipped' with the reason."""


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class Queue:
    def __init__(self, client_factory: Callable[[], NAIClient] | None = None):
        self._client_factory = client_factory or NAIClient
        self._client: NAIClient | None = None
        self.paused = False
        self.pause_reason: str | None = None
        self.current: int | None = None  # running job id
        self.waiting: dict[str, Any] | None = None  # {"seconds", "reason", "until"} during a pacing sleep
        self.battery: dict[str, Any] | None = None  # last battery_summary()
        self._battery_at = 0.0
        self.forced = False  # battery threshold overridden until the queue runs dry
        self.refs = None  # refs.RefFetcher, attached by the app; its state rides along on the SSE stream
        self.pacer: Pacer | None = None
        self.since_check = 0  # images generated since the last subscription fetch
        self._not_before = 0.0
        self._wait_reason = "gap"
        self._wake = asyncio.Event()
        self._subs: set[asyncio.Queue[str]] = set()
        self._task: asyncio.Task | None = None

    # --- lifecycle -----------------------------------------------------------

    @property
    def client(self) -> NAIClient:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def start(self) -> None:
        conn = db.connect()
        db.fail_jobs(conn, "running", "interrupted by server restart")
        db.fail_jobs(conn, "queued", "dropped by server restart; click the cell again")
        self._task = asyncio.create_task(self._run(), name="nai-queue")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._client is not None:
            self._client.close()
            self._client = None

    # --- subscription (cached) -----------------------------------------------

    def subscription(self, fresh: bool = False) -> dict[str, Any]:
        """Blocking. Cached for SUBSCRIPTION_TTL unless fresh."""
        if fresh or self.battery is None or time.monotonic() - self._battery_at > SUBSCRIPTION_TTL:
            self.battery = battery_summary(self.client.subscription())
            self.battery["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._battery_at = time.monotonic()
            self.since_check = 0  # a fresh reading starts a new check window
        return self.battery

    async def subscription_async(self, fresh: bool = False) -> dict[str, Any]:
        return await asyncio.to_thread(self.subscription, fresh)

    # --- control -------------------------------------------------------------

    def kick(self) -> None:
        self._wake.set()

    def enqueue(self, conn, artist_id: int, template_id: str):
        job = db.enqueue_job(conn, artist_id, template_id)
        self.kick()
        return job

    def pause(self, reason: str = "paused by user") -> None:
        self.paused = True
        self.pause_reason = reason
        self.forced = False
        self.broadcast()

    def resume(self, force: bool = False) -> None:
        self.paused = False
        self.pause_reason = None
        self.forced = force
        self.kick()
        self.broadcast()

    def force(self) -> None:
        """A forced enqueue: the battery threshold stays overridden until the queue runs dry."""
        if not self.forced:
            self.forced = True
            self.broadcast()

    def clear(self, conn) -> int:
        n = db.clear_queued_jobs(conn)
        self.forced = False
        self.broadcast()
        return n

    def _suspend(self, reason: str) -> None:
        self.paused = True
        self.pause_reason = reason
        self.forced = False
        self.broadcast()

    # --- state / SSE ---------------------------------------------------------

    def state(self, conn=None) -> dict[str, Any]:
        conn = conn or db.connect()
        return {
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "forced": self.forced,
            "current": self.current,
            "waiting": self.waiting,
            "battery": self.battery,
            "counts": db.job_counts(conn),
            "jobs": [dict(r) for r in db.list_jobs(conn)],
            "refs": self.refs.state() if self.refs is not None else None,
        }

    def broadcast(self) -> None:
        if not self._subs:
            return
        msg = _sse("state", self.state())
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # slow consumer; it gets the next snapshot

    async def stream(self) -> AsyncIterator[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
        self._subs.add(q)
        try:
            yield _sse("state", self.state())
            while True:
                try:
                    yield await asyncio.wait_for(q.get(), SSE_PING_SECONDS)
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            self._subs.discard(q)

    # --- worker --------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            if self.paused:
                await self._wake.wait()
                continue
            conn = db.connect()
            job = db.next_queued_job(conn)
            if job is None:
                if self.forced:  # the forced batch is done; the next one asks again
                    self.forced = False
                    self.broadcast()
                await self._wake.wait()
                continue
            try:
                settings = load_settings()
            except (TemplateError, OSError) as e:
                self._suspend(f"settings.toml: {e}")
                continue
            if self.pacer is None or self.pacer.pacing != settings.pacing:
                self.pacer = Pacer(settings.pacing)

            remaining = self._not_before - time.monotonic()
            if remaining > 0:
                self.waiting = {"seconds": round(remaining, 1), "reason": self._wait_reason,
                                "until": time.time() + remaining}
                self.broadcast()
                await asyncio.sleep(remaining)
                self.waiting = None
                continue  # re-check paused / queue after the sleep

            b = settings.battery
            if self.battery is None or self.since_check >= images_until_check(self.battery, b):
                try:
                    sub = await self.subscription_async(fresh=True)  # resets since_check
                except NAIError as e:
                    self._suspend(f"battery check failed: {e}")
                    continue
                if anlas_short(sub, b.anlas_per_image):
                    self._suspend(f"anlas: battery empty, {sub['anlas']} Anlas left (< {b.anlas_per_image} per image)")
                    continue
                if battery_low(sub, b.min_percent) and not self.forced:
                    self._suspend(f"battery: {sub['battery_percent']}%")
                    continue

            db.set_job_status(conn, job["id"], "running")
            self.current = job["id"]
            self.broadcast()
            status, error = "done", None
            try:
                await asyncio.to_thread(self._generate_cell, settings, job["id"])
            except Skip as e:
                status, error = "skipped", str(e)
            except NAIError as e:
                status, error = "error", str(e)
                self._touched_api()
                if e.fatal:
                    self.paused = True
                    self.pause_reason = f"NovelAI {e} (never retried; resume when ready)"
                    self.forced = False
            except Exception as e:  # noqa: BLE001 - a bad job must not kill the worker
                status, error = "error", f"{type(e).__name__}: {e}"
                self._touched_api()
            else:
                self._touched_api()
            db.set_job_status(conn, job["id"], status, error)
            self.current = None
            self.broadcast()

    def _touched_api(self) -> None:
        self.since_check += 1
        assert self.pacer is not None
        delay, self._wait_reason = self.pacer.next_delay()
        self._not_before = time.monotonic() + delay

    def _generate_cell(self, settings: Settings, job_id: int) -> ImportResult:
        """Runs in a worker thread: own DB connection, blocking HTTP."""
        conn = db.connect()
        job = db.get_job(conn, job_id)
        assert job is not None
        template = get_template(job["template_id"], settings)
        artist = db.get_artist(conn, job["artist_id"])
        assert artist is not None
        tags = db.artist_tags(artist)
        dest, _thumb = cell_paths(artist["slug"], template.id)
        existing = db.get_image(conn, artist["id"], template.id)
        if existing is not None and existing["template_hash"] == template.hash and dest.is_file():
            raise Skip("cell already exists with the current template hash")
        if existing is None and dest.exists():
            raise Skip(f"{dest.relative_to(config.DATA_DIR)} exists on disk but is not registered; import or delete it")

        body = build_body(settings, template, tags)
        png, seed = self.client.generate(body)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(png)
        comment = read_comment(dest) or self._synthetic_comment(body, seed)
        return register_image(conn, settings, template, tags, dest, comment, source="generated", exact=True, replace=True)

    @staticmethod
    def _synthetic_comment(body: dict[str, Any], seed: int) -> dict[str, Any]:
        """NAI always writes a Comment; if it ever doesn't, record what we asked for."""
        p = body["parameters"]
        keys = ("width", "height", "steps", "scale", "sampler", "noise_schedule", "cfg_rescale",
                "uncond_scale", "sm", "sm_dyn", "dynamic_thresholding", "prefer_brownian")
        return {"seed": seed, "uc": p["negative_prompt"], **{k: p[k] for k in keys}}

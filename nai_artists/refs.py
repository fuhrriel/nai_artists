"""Reference images: an artist's top-scored Gelbooru posts, to check whether a generated style really is
that artist's or just what a random tag embedding does to the picture.

- Files: data/refs/full/<slug>/<post_id>.<ext> (as downloaded), data/refs/thumbs/<slug>/<post_id>.webp.
- DB: `refs` (kept posts, rank 0 = best score) and `ref_fetches` (per artist: search override, last query,
  when, how many were kept, last error), so "fetched, found nothing" is remembered.
- Single artists only: a combo or the baseline has no "actual art" to compare against.
- Network only on an explicit user action (UI button, CLI `refs`). Never on startup, never in tests
  (inject a fake client). Gelbooru needs GELBOORU_API_KEY + GELBOORU_USER_ID; without them it answers 401.
- Pacing: every request (API search and image download alike) goes through one `Pace`, at most
  `[refs].per_second` (8) per second, evenly spaced. Gelbooru allows 10 API requests per second.
- 401 / 403 / 429 stop the fetcher and drop what is pending: never retried. Anything else is recorded on
  that artist and the fetcher moves on.
- No rating filter by default: the top posts of a mostly-SFW artist are often NSFW, and they are still the
  best style reference. The UI blurs questionable / explicit refs; `[refs].extra_tags` can exclude them at
  search time instead (e.g. "-rating:explicit").
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

import httpx

from . import USER_AGENT, config, db
from .templates import Refs, TemplateError, load_settings
from .thumbs import make_thumb

GELBOORU = "https://gelbooru.com/index.php"
POST_URL = "https://gelbooru.com/index.php?page=post&s=view&id={id}"
# The image CDN is hotlink-protected: without this Referer it answers 200 with the post's HTML page.
REFERER = "https://gelbooru.com/"
MAX_DOWNLOAD_FAILURES = 3  # in a row, per artist: give up instead of burning requests on every candidate
IMAGE_EXTS = {"jpg", "jpeg", "png", "webp"}
RATINGS = {"general", "sensitive", "questionable", "explicit"}
OLD_RATINGS = {"safe": "general", "s": "general", "q": "questionable", "e": "explicit"}


class BooruError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status

    @property
    def fatal(self) -> bool:
        """Bad credentials or rate limited: stop fetching, never retry."""
        return self.status in (401, 403, 429)


class Pace:
    """At most `per_second` calls per second, evenly spaced: each wait() returns no sooner than
    1 / per_second after the previous one. Thread-safe; the lock is held while sleeping, so callers queue up."""

    def __init__(self, per_second: float, clock=time.monotonic, sleep=time.sleep):
        self.interval = 1.0 / per_second
        self._clock, self._sleep = clock, sleep
        self._last = float("-inf")
        self._lock = threading.Lock()

    def set_rate(self, per_second: float) -> None:
        self.interval = 1.0 / per_second

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            due = self._last + self.interval
            if due > now:
                self._sleep(due - now)
                now = due
            self._last = now


class GelbooruClient:
    def __init__(self, credentials: tuple[str, str] | None = None, timeout: float = 60.0, per_second: float = 8.0):
        creds = credentials or config.gelbooru_credentials()
        if creds is None:
            raise RuntimeError("GELBOORU_API_KEY and GELBOORU_USER_ID are not set (put them in .env)")
        self._auth = {"api_key": creds[0], "user_id": creds[1]}
        self._client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True)
        self.pace = Pace(per_second)

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _scrub(self, text: str) -> str:
        """The credentials travel in the query string; keep them out of error messages."""
        for v in self._auth.values():
            text = text.replace(v, "***")
        return text

    def _get(self, url: str, **kw) -> httpx.Response:
        self.pace.wait()
        try:
            r = self._client.get(url, **kw)
        except httpx.HTTPError as e:
            raise BooruError(0, self._scrub(f"{type(e).__name__}: {e}")) from None
        if r.status_code >= 400:
            raise BooruError(r.status_code, self._scrub(r.text[:200].strip() or r.reason_phrase))
        return r

    def search(self, tags: str, limit: int) -> list[dict[str, Any]]:
        r = self._get(GELBOORU, params={"page": "dapi", "s": "post", "q": "index", "json": 1,
                                        "tags": tags, "limit": limit, **self._auth})
        try:
            data = r.json()
        except ValueError:
            raise BooruError(r.status_code, f"not JSON: {self._scrub(r.text[:200])}") from None
        posts = data.get("post", []) if isinstance(data, dict) else data  # no "post" key = no results
        if isinstance(posts, dict):
            posts = [posts]
        return [p for p in posts or [] if isinstance(p, dict)]

    def download(self, url: str) -> bytes:
        r = self._get(url, headers={"Referer": REFERER})
        ctype = r.headers.get("content-type", "")
        if not ctype.startswith("image/"):
            raise BooruError(r.status_code, f"expected an image, got {ctype or 'no content-type'} (hotlink protection?)")
        return r.content


def booru_tag(artist_tag: str) -> str:
    """NAI / danbooru prompt spelling -> booru tag: prompt escapes dropped, spaces become underscores."""
    return "_".join(artist_tag.replace("\\", "").strip().lower().split())


def _ext(url: str) -> str:
    return PurePosixPath(urlparse(url).path).suffix.lstrip(".").lower()


def _rating(v: Any) -> str | None:
    v = str(v or "").lower()
    return v if v in RATINGS else OLD_RATINGS.get(v)


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def search_query(conn, artist, rs: Refs) -> str:
    tags = db.artist_tags(artist)
    if len(tags) != 1:
        raise ValueError(f"{artist['slug']}: refs are fetched for single artists only")
    status = db.get_ref_fetch(conn, artist["id"])
    tag = (status["override"] if status is not None else None) or booru_tag(tags[0])
    return " ".join(x for x in (tag, "sort:score:desc", rs.extra_tags.strip()) if x)


@dataclass
class FetchResult:
    slug: str
    query: str
    kept: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def fetch_artist(conn, client, artist, rs: Refs) -> FetchResult:
    """Search, download the top `rs.count` images, thumb them, swap the artist's ref rows.

    A failed search raises BooruError after recording it (existing refs stay). A failed download skips
    that post and tries the next candidate, until MAX_DOWNLOAD_FAILURES in a row; fatal ones
    (401/403/429) raise.
    """
    query = search_query(conn, artist, rs)
    res = FetchResult(artist["slug"], query)
    failed_in_a_row = 0
    try:
        posts = client.search(query, limit=min(100, rs.count * 4 + 4))  # over-fetch: videos etc. get dropped
        for p in posts:
            if len(res.kept) >= rs.count:
                break
            if failed_in_a_row >= MAX_DOWNLOAD_FAILURES:
                res.errors.append(f"gave up after {failed_in_a_row} failed downloads in a row")
                break
            url = (p.get("sample_url") if rs.use_sample else None) or p.get("file_url") or ""
            pid = _int(p.get("id"))
            ext = _ext(url)
            if pid is None or ext not in IMAGE_EXTS:
                continue
            dest = config.REFS_DIR / "full" / res.slug / f"{pid}.{ext}"
            thumb = config.REFS_DIR / "thumbs" / res.slug / f"{pid}.webp"
            try:
                if not dest.is_file():
                    data = client.download(url)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    part = dest.with_name(dest.name + ".part")
                    part.write_bytes(data)
                    part.replace(dest)
                if not thumb.is_file():
                    make_thumb(dest, thumb)
            except BooruError as e:
                if e.fatal:
                    raise
                res.errors.append(f"post {pid}: {e}")
                failed_in_a_row += 1
                continue
            except OSError as e:  # PIL can't read it (UnidentifiedImageError is an OSError)
                dest.unlink(missing_ok=True)
                res.errors.append(f"post {pid}: {type(e).__name__}: {e}")
                failed_in_a_row += 1
                continue
            failed_in_a_row = 0
            res.kept.append({
                "post_id": pid, "rank": len(res.kept), "score": _int(p.get("score")), "rating": _rating(p.get("rating")),
                "width": _int(p.get("width")), "height": _int(p.get("height")),
                "path": db.rel_path(dest), "thumb_path": db.rel_path(thumb), "post_url": POST_URL.format(id=pid),
            })
    except BooruError as e:
        db.set_ref_fetch(conn, artist["id"], query=query, found=None, error=str(e))
        raise
    old = db.replace_refs(conn, artist["id"], res.kept)
    keep = {k["path"] for k in res.kept} | {k["thumb_path"] for k in res.kept}
    for r in old:
        for rel in (r["path"], r["thumb_path"]):
            if rel not in keep:
                db.abs_path(rel).unlink(missing_ok=True)
    db.set_ref_fetch(conn, artist["id"], query=query, found=len(res.kept), error="; ".join(res.errors) or None)
    return res


def eligible(artist) -> bool:
    return artist["slug"] != config.BASE_SLUG and not artist["is_combo"]


class RefFetcher:
    """Background fetcher for the web app: one artist at a time, paced by the client (`[refs].per_second`).

    Its state rides on the queue's SSE stream (Queue.refs); `done` only ever grows, so the UI reloads
    the matrix when it moves.
    """

    def __init__(self, client_factory: Callable[[], Any] | None = None, on_change: Callable[[], None] = lambda: None,
                 configured: Callable[[], bool] | None = None):
        self._client_factory = client_factory or GelbooruClient
        self._configured = configured or (lambda: config.gelbooru_credentials() is not None)
        self._client = None
        self._on_change = on_change
        self.pending: deque[int] = deque()
        self.current: str | None = None
        self.done = 0
        self.last_error: str | None = None
        self.stopped: str | None = None  # fatal error; cleared by the next add()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def configured(self) -> bool:
        return self._configured()

    def state(self) -> dict[str, Any]:
        return {"configured": self.configured, "pending": len(self.pending), "current": self.current,
                "done": self.done, "last_error": self.last_error, "stopped": self.stopped}

    def add(self, artist_ids: list[int]) -> int:
        self.stopped = None
        n = 0
        for i in artist_ids:
            if i not in self.pending:
                self.pending.append(i)
                n += 1
        self._wake.set()
        self._on_change()
        return n

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="refs")

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

    def _fetch_one(self, artist_id: int, rs: Refs) -> FetchResult | None:
        """Worker thread: own DB connection, blocking HTTP."""
        conn = db.connect()
        artist = db.get_artist(conn, artist_id)
        if artist is None or not eligible(artist):
            return None
        if self._client is None:
            self._client = self._client_factory()
        if isinstance(self._client, GelbooruClient):
            self._client.pace.set_rate(rs.per_second)  # settings.toml is re-read per artist, like everywhere else
        return fetch_artist(conn, self._client, artist, rs)

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            if not self.pending:
                await self._wake.wait()
                continue
            try:
                rs = load_settings().refs
            except (TemplateError, OSError) as e:
                self._halt(f"settings.toml: {e}")
                continue
            artist_id = self.pending.popleft()
            conn = db.connect()
            a = db.get_artist(conn, artist_id)
            self.current = a["slug"] if a is not None else str(artist_id)
            self._on_change()
            try:
                res = await asyncio.to_thread(self._fetch_one, artist_id, rs)
                if res is not None and res.errors:
                    self.last_error = f"{res.slug}: {'; '.join(res.errors)}"
            except BooruError as e:
                self.last_error = f"{self.current}: {e}"
                if e.fatal:
                    self._halt(f"Gelbooru {e} (never retried)")
            except RuntimeError as e:  # no credentials
                self._halt(str(e))
            except Exception as e:  # noqa: BLE001 - one bad artist must not kill the fetcher
                self.last_error = f"{self.current}: {type(e).__name__}: {e}"
            self.done += 1
            self.current = None
            self._on_change()

    def _halt(self, reason: str) -> None:
        self.stopped = reason
        self.last_error = reason
        self.pending.clear()
        self._on_change()

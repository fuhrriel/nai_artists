"""Reference images (refs.py) against a fake Gelbooru client. Nothing here touches the network."""

from __future__ import annotations

import io
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from nai_artists import config, db
from nai_artists.app import create_app
from nai_artists.refs import BooruError, GelbooruClient, Pace, booru_tag, fetch_artist
from nai_artists.templates import Refs
from tests.test_app import FakeNAI, sandbox  # noqa: F401 - fixture


def img_bytes(fmt: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (0, 0, 200)).save(buf, fmt)
    return buf.getvalue()


def post(pid: int, ext: str = "jpg", score: int = 100, rating: str = "general") -> dict:
    return {"id": pid, "score": score, "rating": rating, "width": 40, "height": 30,
            "file_url": f"https://img3.gelbooru.com/images/aa/bb/{pid}.{ext}",
            "sample_url": f"https://img3.gelbooru.com/samples/aa/bb/sample_{pid}.jpg"}


class FakeBooru:
    def __init__(self):
        self.posts: dict[str, list[dict]] = {}
        self.searches: list[str] = []
        self.downloads: list[str] = []
        self.fail_search: tuple[int, str] | None = None
        self.fail_download: dict[int, int] = {}  # post id -> status

    def search(self, tags, limit):
        self.searches.append(tags)
        if self.fail_search:
            raise BooruError(*self.fail_search)
        return self.posts.get(tags.split()[0], [])

    def download(self, url):
        self.downloads.append(url)
        for pid, status in self.fail_download.items():
            if f"/{pid}." in url:
                raise BooruError(status, "nope")
        if "broken" in url:
            return b"not an image"
        return img_bytes("PNG" if url.endswith(".png") else "JPEG")

    def close(self):
        pass


@pytest.fixture
def booru():
    b = FakeBooru()
    b.posts["fuhrriel"] = [post(1, "webm"), post(2, "jpg", 900, "explicit"), post(3, "png", 800),
                       {**post(4), "file_url": "https://img3.gelbooru.com/images/broken.jpg"}, post(5, score=500), post(6)]
    return b


class FakeClock:
    def __init__(self):
        self.t = 100.0
        self.slept: list[float] = []

    def __call__(self):
        return self.t

    def sleep(self, d):
        self.slept.append(round(d, 6))
        self.t += d


def test_pace_eight_per_second():
    clk = FakeClock()
    pace = Pace(8, clock=clk, sleep=clk.sleep)
    stamps = []
    for _ in range(20):
        pace.wait()
        stamps.append(clk.t)
    gaps = [round(b - a, 6) for a, b in zip(stamps, stamps[1:])]
    assert gaps == [0.125] * 19 and len(clk.slept) == 19  # first call is free, then evenly spaced
    assert max(sum(1 for t in stamps if s <= t < s + 1.0) for s in stamps) == 8  # never more than 8 in any second
    clk.t += 5  # idle for a while: no catch-up burst, no sleep needed
    n = len(clk.slept)
    pace.wait()
    assert len(clk.slept) == n
    pace.set_rate(4)
    pace.wait()
    assert clk.slept[-1] == 0.25


def test_client_paces_every_request_and_sends_referer(monkeypatch):
    c = GelbooruClient(credentials=("secretkey", "1"))
    calls, html = [], {"on": False}

    def get(url, **kw):
        calls.append(("get", (kw.get("headers") or {}).get("Referer")))
        req = httpx.Request("GET", url)
        if "index.php" in url:
            return httpx.Response(200, json={"post": []}, request=req)
        if html["on"]:  # what the CDN does without a Referer: 200 with the post page
            return httpx.Response(200, text="<!DOCTYPE html>", headers={"content-type": "text/html"}, request=req)
        return httpx.Response(200, content=img_bytes("JPEG"), headers={"content-type": "image/jpeg"}, request=req)

    monkeypatch.setattr(c.pace, "wait", lambda: calls.append(("wait", None)))
    monkeypatch.setattr(c._client, "get", get)
    assert c.search("x", 1) == []
    assert c.download("https://img3.gelbooru.com/images/a.jpg")[:2] == b"\xff\xd8"
    assert calls == [("wait", None), ("get", None), ("wait", None), ("get", "https://gelbooru.com/")]
    html["on"] = True
    with pytest.raises(BooruError, match="expected an image, got text/html") as e:
        c.download("https://img3.gelbooru.com/images/a.jpg")
    assert not e.value.fatal and "secretkey" not in str(e.value)
    c.close()


def test_booru_tag():
    assert booru_tag("fake artist") == "fake_artist"
    assert booru_tag("  Sho \\(sho lwlw\\) ") == "sho_(sho_lwlw)"


def test_fetch_artist_keeps_top_images_and_swaps(sandbox, booru):
    conn = db.connect()
    a = db.get_or_create_artist(conn, ["fuhrriel"])
    res = fetch_artist(conn, booru, a, Refs())
    assert res.query == "fuhrriel sort:score:desc -video -animated -comic"
    assert [k["post_id"] for k in res.kept] == [2, 3, 5]  # webm dropped, broken one skipped
    assert len(res.errors) == 1 and res.errors[0].startswith("post 4:")
    assert not (config.REFS_DIR / "full" / "fuhrriel" / "4.jpg").exists()
    rows = db.list_refs(conn)[a["id"]]
    assert [(r["rank"], r["rating"], r["score"]) for r in rows] == [(0, "explicit", 900), (1, "general", 800), (2, "general", 500)]
    for r in rows:
        assert db.abs_path(r["path"]).is_file() and db.abs_path(r["thumb_path"]).is_file()
    assert rows[1]["path"] == "refs/full/fuhrriel/3.png" and rows[1]["thumb_path"] == "refs/thumbs/fuhrriel/3.webp"
    f = db.get_ref_fetch(conn, a["id"])
    assert f["found"] == 3 and f["error"].startswith("post 4:") and f["fetched_at"]

    # re-fetch: new top posts; files of dropped refs go, kept ones are not downloaded again
    booru.posts["fuhrriel"] = [post(5, score=999), post(7), post(8)]
    booru.downloads.clear()
    res = fetch_artist(conn, booru, a, Refs())
    assert [k["post_id"] for k in res.kept] == [5, 7, 8] and len(booru.downloads) == 2
    assert not (config.REFS_DIR / "full" / "fuhrriel" / "2.jpg").exists()
    assert not (config.REFS_DIR / "thumbs" / "fuhrriel" / "3.webp").exists()
    assert db.get_ref_fetch(conn, a["id"])["error"] is None

    # override + sample images
    db.set_ref_override(conn, a["id"], "fuhrriel_(artist)")
    booru.posts["fuhrriel_(artist)"] = [post(9)]
    res = fetch_artist(conn, booru, a, Refs(count=1, use_sample=True, extra_tags=""))
    assert res.query == "fuhrriel_(artist) sort:score:desc" and booru.downloads[-1].endswith("sample_9.jpg")

    # nothing found is remembered; a failed search keeps the refs and records the error
    booru.posts["fuhrriel_(artist)"] = []
    assert fetch_artist(conn, booru, a, Refs()).kept == [] and db.get_ref_fetch(conn, a["id"])["found"] == 0
    booru.posts["fuhrriel_(artist)"] = [post(9)]
    fetch_artist(conn, booru, a, Refs())
    booru.fail_search = (500, "boom")
    with pytest.raises(BooruError):
        fetch_artist(conn, booru, a, Refs())
    f = db.get_ref_fetch(conn, a["id"])
    assert f["found"] == 1 and "500" in f["error"] and len(db.list_refs(conn)[a["id"]]) == 1

    combo = db.get_or_create_artist(conn, ["a", "b"])
    with pytest.raises(ValueError):
        fetch_artist(conn, booru, combo, Refs())


def test_gives_up_after_failed_downloads_in_a_row(sandbox, booru):
    conn = db.connect()
    a = db.get_or_create_artist(conn, ["fuhrriel"])
    booru.posts["fuhrriel"] = [post(i) for i in range(10, 20)]
    booru.fail_download = {i: 500 for i in range(10, 20)}
    res = fetch_artist(conn, booru, a, Refs())
    assert res.kept == [] and len(booru.downloads) == 3 and res.errors[-1] == "gave up after 3 failed downloads in a row"
    # a success resets the streak
    booru.downloads.clear()
    booru.fail_download = {10: 500, 11: 500, 13: 500, 14: 500}
    res = fetch_artist(conn, booru, a, Refs())
    assert [k["post_id"] for k in res.kept] == [12, 15, 16]


def test_fatal_download_aborts(sandbox, booru):
    conn = db.connect()
    a = db.get_or_create_artist(conn, ["fuhrriel"])
    booru.fail_download = {3: 429}
    with pytest.raises(BooruError) as e:
        fetch_artist(conn, booru, a, Refs())
    assert e.value.fatal and "429" in db.get_ref_fetch(conn, a["id"])["error"]
    assert a["id"] not in db.list_refs(conn)


@pytest.fixture
def app_client(sandbox, booru, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "gelbooru_credentials", lambda: ("k", "1"))
    with TestClient(create_app(lambda: FakeNAI(), lambda: booru)) as c:
        yield c


def wait_refs(c: TestClient, timeout: float = 10.0) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = c.get("/api/jobs").json()["refs"]
        if st["pending"] == 0 and st["current"] is None:
            return st
        time.sleep(0.02)
    raise AssertionError("ref fetcher never went idle")


def test_refs_routes(app_client, booru):
    c = app_client
    c.post("/api/artists", json={"artists": ["fuhrriel", "fake artist", "a + b"]})
    assert c.get("/api/matrix").json()["refs"]["configured"] is True

    assert c.post("/api/refs/fetch", json={"artists": ["a+b"]}).status_code == 400
    assert c.post("/api/refs/fetch", json={"artists": ["_base"]}).status_code == 400
    assert c.post("/api/refs/fetch", json={"artists": ["nope"]}).status_code == 404
    assert c.post("/api/refs/fetch", json={"artists": "missing", "override": "x"}).status_code == 400

    r = c.post("/api/refs/fetch", json={"artists": ["fuhrriel"]}).json()
    assert r["added"] == 1
    st = wait_refs(c)
    assert st["done"] == 1 and st["last_error"].startswith("fuhrriel: post 4:")
    m = c.get("/api/matrix").json()
    by = {a["slug"]: a for a in m["artists"]}
    assert [x["post_id"] for x in by["fuhrriel"]["refs"]] == [2, 3, 5] and by["fuhrriel"]["refs_fetch"]["found"] == 3
    assert by["a+b"]["refs_eligible"] is False and by["_base"]["refs_eligible"] is False
    assert by["fake_artist"]["refs"] == [] and by["fake_artist"]["refs_fetch"] is None
    assert c.get("/" + by["fuhrriel"]["refs"][0]["thumb_path"]).headers["content-type"] == "image/webp"
    assert c.get("/" + by["fuhrriel"]["refs"][0]["path"]).status_code == 200

    # "missing" = single artists never fetched: only ilya
    r = c.post("/api/refs/fetch", json={"artists": "missing"}).json()
    assert r["added"] == 1
    wait_refs(c)
    assert booru.searches[-1].startswith("fake_artist ")
    assert c.post("/api/refs/fetch", json={"artists": "missing"}).json()["added"] == 0

    # override for one artist is stored and used
    booru.posts["fake_artist_(artist)"] = [post(11)]
    c.post("/api/refs/fetch", json={"artists": ["fake_artist"], "override": "fake_artist_(artist)"})
    wait_refs(c)
    a = c.get("/api/artists/fake_artist").json()
    assert a["refs_fetch"]["override"] == "fake_artist_(artist)" and [x["post_id"] for x in a["refs"]] == [11]

    # 429: the fetcher stops and drops what is pending
    booru.fail_search = (429, "slow down")
    r = c.post("/api/refs/fetch", json={"artists": "all"}).json()
    assert r["added"] == 2
    st = wait_refs(c)
    assert st["stopped"].startswith("Gelbooru HTTP 429") and st["pending"] == 0
    assert len(booru.searches) == 4  # one attempt, never retried, the other artist never tried


def test_refs_not_configured(sandbox, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "gelbooru_credentials", lambda: None)
    with TestClient(create_app(lambda: FakeNAI())) as c:
        assert c.get("/api/matrix").json()["refs"]["configured"] is False
        r = c.post("/api/refs/fetch", json={"artists": "missing"})
        assert r.status_code == 400 and "GELBOORU_API_KEY" in r.json()["detail"]

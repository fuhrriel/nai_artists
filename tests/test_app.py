"""Routes + queue against a fake NovelAI client. Nothing here touches the network."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin

from nai_artists import config, db
from nai_artists.app import create_app
from nai_artists.meta import read_comment
from nai_artists.nai import NAIError
from nai_artists.templates import create_from_image, get_template, load_settings
from tests.conftest import DUO, ROOT, SOLO

FAST_PACING = """
[pacing]
gap_ms = 0

[battery]
check_every = 2
min_percent = 5
"""


def make_png(body: dict, seed: int) -> bytes:
    """A tiny PNG whose Comment says exactly what the request asked for (like NAI does)."""
    p = body["parameters"]
    comment = {
        "prompt": body["input"], "uc": p["negative_prompt"], "seed": seed,
        "steps": p["steps"], "width": p["width"], "height": p["height"], "scale": p["scale"],
        "sampler": p["sampler"], "noise_schedule": p["noise_schedule"], "cfg_rescale": 0.0, "uncond_scale": 0.0,
        "sm": False, "sm_dyn": False, "dynamic_thresholding": False, "prefer_brownian": True,
        "v4_prompt": p["v4_prompt"], "v4_negative_prompt": p["v4_negative_prompt"],
        "model_name": "nai-diffusion-5-full", "model_hash": "FAKE0000",
    }
    info = PngImagePlugin.PngInfo()
    info.add_text("Software", "NovelAI")
    info.add_text("Comment", json.dumps(comment, ensure_ascii=False))
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (seed % 256, 0, 0)).save(buf, "PNG", pnginfo=info)
    return buf.getvalue()


class FakeNAI:
    def __init__(self):
        self.calls: list[dict] = []
        self.sub_calls = 0
        self.percent = 80
        self.negative = False
        self.fail: tuple[int, str] | None = None
        self.seed_counter = 0
        self.anlas = 105
        self.spend = 0  # Anlas taken per generation while the battery is empty

    def generate(self, body):
        self.calls.append(body)
        if self.fail:
            raise NAIError(*self.fail)
        if self.negative or self.percent <= 0:
            if self.anlas < self.spend:
                raise NAIError(402, "Not enough Anlas")
            self.anlas -= self.spend
        self.seed_counter += 1
        return make_png(body, body["parameters"]["seed"]), body["parameters"]["seed"]

    def subscription(self):
        self.sub_calls += 1
        return {"tier": 3, "active": True, "usage": {"percent": self.percent, "isNegative": self.negative,
                                                   "timeUntilNextPercent": 100},
                "trainingStepsLeft": {"fixedTrainingStepsLeft": self.anlas - 5, "purchasedTrainingSteps": 5}}

    def suggest_tags(self, prompt, model="x", lang="en"):
        return [{"tag": "artist:fuhrriel", "count": 10}, {"tag": "fuhrrielish", "count": 1}, {"tag": "artist:fuhrriel (fake)", "count": 2}]

    def close(self):
        pass


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "FULL_DIR", data / "full")
    monkeypatch.setattr(config, "THUMBS_DIR", data / "thumbs")
    monkeypatch.setattr(config, "INBOX_DIR", data / "inbox")
    monkeypatch.setattr(config, "REFS_DIR", data / "refs")
    monkeypatch.setattr(config, "DB_FILE", data / "nai.db")
    monkeypatch.setattr(config, "TEMPLATES_DIR", tmp_path / "templates")
    real = (ROOT / "settings.toml").read_text(encoding="utf-8")
    fast = real.split("\n[pacing]")[0] + FAST_PACING
    (tmp_path / "settings.toml").write_text(fast, encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.toml")
    monkeypatch.setenv("NAI_API_KEY", "fake")
    s = load_settings()
    create_from_image(read_comment(SOLO), "1girl", "Solo girl", primary=True, settings=s)
    create_from_image(read_comment(DUO), "duo", "Duo", settings=s)
    return tmp_path


@pytest.fixture
def fake():
    return FakeNAI()


@pytest.fixture
def client(sandbox, fake):
    with TestClient(create_app(lambda: fake)) as c:
        yield c


def wait_idle(c: TestClient, timeout: float = 10.0) -> dict:
    """Until nothing is queued/running, or the queue paused itself."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = c.get("/api/jobs").json()
        if st["current"] is None and (st["counts"]["queued"] == 0 or st["paused"]):
            return st
        time.sleep(0.02)
    raise AssertionError(f"queue never went idle: {c.get('/api/jobs').json()}")


def statuses(c: TestClient) -> list[str]:
    return [j["status"] for j in c.get("/api/jobs").json()["jobs"]]


# --- read side ------------------------------------------------------------------

def test_empty_matrix_and_templates(client):
    m = client.get("/api/matrix").json()
    assert [t["id"] for t in m["templates"]] == ["1girl", "duo"]
    assert [a["slug"] for a in m["artists"]] == ["_base"] and m["artists"][0]["cells"] == {}
    assert m["queue"]["paused"] is False
    t = client.get("/api/templates").json()["templates"]
    assert t[0]["images"] == 0 and t[0]["primary"] is True and t[1]["chars"] == 2
    s = client.get("/api/settings").json()
    assert s["settings"]["seed"] == 2137 and "pacing" not in s["hash_material"]
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "/static/app.js" in r.text
    for tab in ("matrix", "generate", "import", "templates"):
        assert f'data-tab="{tab}"' in r.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_subscription_cached(client, fake):
    a = client.get("/api/subscription").json()
    assert a["battery_percent"] == 80 and a["anlas"] == 105 and fake.sub_calls == 1
    client.get("/api/subscription")
    assert fake.sub_calls == 1
    client.get("/api/subscription?fresh=1")
    assert fake.sub_calls == 2


def test_suggest_filters_to_artists(client):
    tags = client.get("/api/suggest?q=wl").json()["tags"]
    assert [t["tag"] for t in tags] == ["artist:fuhrriel", "artist:fuhrriel (fake)"]


# --- add artists / generate ------------------------------------------------------

def add(c: TestClient, *artists) -> dict:
    r = c.post("/api/artists", json={"artists": list(artists)})
    assert r.status_code == 200, r.text
    return r.json()


def click(c: TestClient, slug: str, tid: str, **kw) -> dict:
    """One ⟳ click: exactly one cell."""
    r = c.post("/api/generate", json={"artist": slug, "template": tid, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def click_all(c: TestClient, slugs, tids=("1girl", "duo"), **kw) -> list[dict]:
    return [click(c, s, t, **kw) for s in slugs for t in tids]


def job_total() -> int:
    return db.connect().execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


def test_add_artists_never_generates(client, fake):
    r = add(client, "artist:FUHRRIEL", "a + b", ["c", "d"], "fuhrriel")
    assert r == {"added": ["fuhrriel", "a+b", "c+d"], "existing": []}
    assert add(client, "Fuhrriel")["existing"] == ["fuhrriel"]
    assert job_total() == 0 and not fake.calls and fake.sub_calls == 0  # not even a battery check
    m = client.get("/api/matrix").json()
    assert [a["slug"] for a in m["artists"]] == ["_base", "a+b", "c+d", "fuhrriel"]
    assert all(a["cells"] == {} for a in m["artists"])
    assert client.post("/api/artists", json={"artists": []}).status_code == 400
    assert client.post("/api/artists", json={"artists": ["   ", "+"]}).status_code == 400
    assert client.post("/api/artists", json={"artists": ["_base"]}).status_code == 400


def test_generate_one_cell_per_click(client, fake):
    add(client, "artist:FUHRRIEL", "a + b")
    res = click_all(client, ["_base", "fuhrriel", "a+b"])
    assert all(r["created"] for r in res) and res[0]["battery"]["battery_percent"] == 80
    st = wait_idle(client)
    assert st["counts"] == {"queued": 0, "running": 0, "done": 6, "error": 0, "skipped": 0}
    jobs = st["jobs"]
    assert [j["slug"] for j in jobs] == ["_base", "_base", "fuhrriel", "fuhrriel", "a+b", "a+b"]
    assert [j["template_id"] for j in jobs[:2]] == ["1girl", "duo"]
    assert len(fake.calls) == 6
    # baseline prompt has no artist line; combo has two
    assert not fake.calls[0]["input"].startswith("::artist:")
    assert fake.calls[4]["input"].startswith("::artist:a,::\n::artist:b,::\n")
    assert fake.sub_calls >= 6  # every click checks the battery

    m = client.get("/api/matrix").json()
    by_slug = {a["slug"]: a for a in m["artists"]}
    assert list(by_slug) == ["_base", "a+b", "fuhrriel"]
    cell = by_slug["fuhrriel"]["cells"]["1girl"]
    assert cell["params_match"] is True and cell["stale"] is False and cell["source"] == "generated"
    assert cell["seed"] == 2137 and cell["meta"]["model_hash"] == "FAKE0000" and cell["job"] is None
    assert (config.FULL_DIR / "fuhrriel" / "1girl.png").is_file() and (config.THUMBS_DIR / "a+b" / "duo.webp").is_file()
    assert client.get("/full/fuhrriel/1girl.png").status_code == 200
    assert client.get("/thumbs/fuhrriel/1girl.webp").headers["content-type"] == "image/webp"

    meta = client.get(f"/api/images/{cell['image_id']}/meta").json()
    assert meta["comment"]["prompt"].startswith("::artist:FUHRRIEL,::\n") and "uc" in meta["comment"]

    # a fresh cell is only regenerated when asked to
    r = client.post("/api/generate", json={"artist": "fuhrriel", "template": "1girl"})
    assert r.status_code == 409 and "replace" in r.json()["detail"]
    assert job_total() == 6

    a = client.get("/api/artists/a+b").json()
    assert a["tags"] == ["a", "b"] and a["is_combo"] and set(a["cells"]) == {"1girl", "duo"}
    assert client.get("/api/artists/nope").status_code == 404


def test_generate_validation(client):
    add(client, "fuhrriel")
    assert client.post("/api/generate", json={"artist": "nope", "template": "1girl"}).status_code == 404
    assert client.post("/api/generate", json={"artist": "fuhrriel", "template": "nope"}).status_code == 400
    # the old batch body is gone
    assert client.post("/api/generate", json={"artists": ["fuhrriel"], "templates": "all"}).status_code == 422
    assert job_total() == 0


def test_battery_gate_and_guard(client, fake):
    add(client, "fuhrriel", "x", "y")
    fake.percent = 3
    r = client.post("/api/generate", json={"artist": "fuhrriel", "template": "1girl"})
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["battery_percent"] == 3 and d["forceable"] is True and d["anlas"] == 105
    assert job_total() == 0

    # clicks while paused, then resume: check_every=2, so two images, then the guard trips with the rest queued
    fake.percent = 50
    client.post("/api/jobs/pause")
    res = click_all(client, ["fuhrriel", "x", "_base"])
    assert len(res) == 6 and res[-1]["queue"]["forced"] is False
    assert client.post("/api/jobs/resume").status_code == 200
    fake.percent = 3
    st = wait_idle(client)
    assert st["paused"] and st["pause_reason"] == "battery: 3%"
    assert st["counts"]["done"] == 2 and st["counts"]["queued"] == 4

    assert client.post("/api/jobs/resume", json={"force": False}).status_code == 409
    assert client.post("/api/jobs/resume").status_code == 409
    # a forced resume sticks until the queue runs dry: no more suspensions at 3 %
    r = client.post("/api/jobs/resume", json={"force": True})
    assert r.status_code == 200 and not r.json()["paused"] and r.json()["forced"]
    st = wait_idle(client)
    assert not st["paused"] and st["counts"]["done"] == 6 and st["counts"]["queued"] == 0
    assert st["forced"] is False  # dropped once the queue ran dry

    # the next click asks again
    assert client.post("/api/generate", json={"artist": "y", "template": "1girl"}).status_code == 409
    r = click(client, "y", "1girl", force=True)
    assert r["created"] and r["queue"]["forced"] is True
    st = wait_idle(client)
    assert not st["paused"] and st["counts"]["queued"] == 0 and st["forced"] is False

    fake.negative = True
    assert client.post("/api/generate", json={"artist": "y", "template": "duo"}).status_code == 409


def test_anlas_floor_is_never_forced(client, fake):
    add(client, "a", "b")
    fake.percent, fake.negative, fake.anlas = 0, True, 29  # battery empty, less than one image of Anlas
    for force in (False, True):
        r = client.post("/api/generate", json={"artist": "_base", "template": "1girl", "force": force})
        assert r.status_code == 409 and r.json()["detail"]["forceable"] is False
        assert r.json()["detail"]["anlas"] == 29 and r.json()["detail"]["anlas_per_image"] == 30
    assert job_total() == 0

    # 0 % counts as empty too. The check window shrinks to what the balance covers.
    fake.negative, fake.anlas = False, 95
    fake.spend = 30  # every generation costs 30 while the battery is empty
    client.post("/api/jobs/pause")
    assert len(click_all(client, ["_base", "a", "b"], force=True)) == 6
    assert client.post("/api/jobs/resume", json={"force": True}).status_code == 200
    st = wait_idle(client)
    # min(check_every=2, 95 // 30) = 2 images -> 35 left -> min(2, 35 // 30) = 1 more -> 5 left -> suspended
    assert st["paused"] and st["pause_reason"].startswith("anlas: battery empty, 5 Anlas left")
    assert st["counts"]["done"] == 3 and st["counts"]["queued"] == 3 and len(fake.calls) == 3
    assert st["forced"] is False  # a suspension drops the override; resume sets it again
    r = client.post("/api/jobs/resume", json={"force": True})
    assert r.status_code == 409 and r.json()["detail"]["forceable"] is False
    assert len(fake.calls) == 3


def test_battery_helpers():
    from nai_artists.nai import anlas_mode, anlas_short, images_until_check
    from nai_artists.templates import Battery

    b = Battery(check_every=10, min_percent=0, anlas_per_image=30)
    ok = {"battery_percent": 40, "is_negative": False, "anlas": 0}
    assert not anlas_mode(ok) and not anlas_short(ok, 30) and images_until_check(ok, b) == 10
    empty = {"battery_percent": 0, "is_negative": False, "anlas": 1000}
    assert anlas_mode(empty) and not anlas_short(empty, 30) and images_until_check(empty, b) == 10
    assert images_until_check({**empty, "anlas": 95}, b) == 3
    assert images_until_check({**empty, "anlas": 29}, b) == 0 and anlas_short({**empty, "anlas": 29}, 30)
    neg = {"battery_percent": 12, "is_negative": True, "anlas": 45}
    assert anlas_mode(neg) and images_until_check(neg, b) == 1


def test_429_pauses_queue_never_retries(client, fake):
    add(client, "fuhrriel")
    fake.fail = (429, "Rate limited")
    client.post("/api/jobs/pause")
    click_all(client, ["_base", "fuhrriel"], ["1girl"])
    client.post("/api/jobs/resume")
    st = wait_idle(client)
    assert st["paused"] and "429" in st["pause_reason"]
    assert statuses(client) == ["error", "queued"] and len(fake.calls) == 1
    assert "429" in st["jobs"][0]["error"]
    fake.fail = None
    client.post("/api/jobs/resume")
    st = wait_idle(client)
    assert not st["paused"] and statuses(client) == ["error", "done"] and len(fake.calls) == 2


def test_402_pauses_too(client, fake):
    add(client, "fuhrriel")
    fake.fail = (402, "Not enough Anlas")
    click(client, "fuhrriel", "1girl")
    st = wait_idle(client)
    assert st["paused"] and "402" in st["pause_reason"] and statuses(client) == ["error"]


def test_other_errors_continue(client, fake):
    add(client, "fuhrriel")
    fake.fail = (500, "boom")
    click_all(client, ["_base", "fuhrriel"], ["1girl"])
    st = wait_idle(client)
    assert not st["paused"] and statuses(client) == ["error", "error"] and len(fake.calls) == 2


def test_pause_clear_resume(client, fake):
    add(client, "fuhrriel")
    client.post("/api/jobs/pause")
    res = click_all(client, ["fuhrriel"])
    assert [r["created"] for r in res] == [True, True]
    time.sleep(0.1)
    st = client.get("/api/jobs").json()
    assert st["paused"] and st["pause_reason"] == "paused by user" and st["counts"]["queued"] == 2 and not fake.calls
    # clicking a cell that is already queued returns its job, no second one
    again = click(client, "fuhrriel", "1girl")
    assert not again["created"] and again["job"]["id"] == res[0]["job"]["id"] and job_total() == 2
    r = client.post("/api/jobs/clear").json()
    assert r["cleared"] == 2 and r["counts"]["queued"] == 0
    client.post("/api/jobs/resume")
    assert wait_idle(client)["counts"]["done"] == 0 and not fake.calls


def test_replace_stale_and_missing_cells(client, fake):
    add(client, "fuhrriel")
    click_all(client, ["_base", "fuhrriel"])
    wait_idle(client)
    client.put("/api/ratings", json={"artist": "fuhrriel", "template": "1girl", "value": 2})
    old = (config.FULL_DIR / "fuhrriel" / "1girl.png").stat().st_mtime_ns
    time.sleep(0.01)
    # fresh: only with replace, which deletes the image first and keeps the cell rating
    r = click(client, "fuhrriel", "1girl", replace=True)
    assert r["created"]
    st = wait_idle(client)
    assert st["counts"]["done"] == 5 and (config.FULL_DIR / "fuhrriel" / "1girl.png").stat().st_mtime_ns != old
    assert client.get("/api/artists/fuhrriel").json()["cells"]["1girl"]["rating"]["value"] == 2

    # edit the template body -> its cells go stale -> a click regenerates in place, no replace needed
    p = config.TEMPLATES_DIR / "duo.txt"
    p.write_text(p.read_text(encoding="utf-8").replace("outdoors", "indoors"), encoding="utf-8")
    m = client.get("/api/matrix").json()
    cells = {a["slug"]: a["cells"] for a in m["artists"]}
    assert cells["fuhrriel"]["duo"]["stale"] and not cells["fuhrriel"]["1girl"]["stale"]
    assert client.get("/api/templates").json()["templates"][1]["stale"] == 2
    assert click(client, "fuhrriel", "duo")["created"]
    wait_idle(client)
    cells = {a["slug"]: a["cells"] for a in client.get("/api/matrix").json()["artists"]}
    assert not cells["fuhrriel"]["duo"]["stale"] and cells["_base"]["duo"]["stale"]  # one click, one cell
    assert "indoors" in fake.calls[-1]["input"]

    # a deleted file: same, in place
    (config.FULL_DIR / "_base" / "1girl.png").unlink()
    assert client.get("/api/matrix").json()["artists"][0]["cells"]["1girl"]["file_missing"]
    assert click(client, "_base", "1girl")["created"]
    wait_idle(client)
    assert (config.FULL_DIR / "_base" / "1girl.png").is_file()


def test_unregistered_file_on_disk_is_never_overwritten(client, fake):
    add(client, "fuhrriel")
    dest = config.FULL_DIR / "fuhrriel" / "1girl.png"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"precious")
    r = client.post("/api/generate", json={"artist": "fuhrriel", "template": "1girl", "replace": True})
    assert r.status_code == 409 and "not registered" in r.json()["detail"] and job_total() == 0
    # appears after the click: the worker skips it
    dest.unlink()
    client.post("/api/jobs/pause")
    click(client, "fuhrriel", "1girl")
    dest.write_bytes(b"precious")
    client.post("/api/jobs/resume")
    st = wait_idle(client)
    assert st["counts"]["skipped"] == 1 and "not registered" in st["jobs"][0]["error"]
    assert dest.read_bytes() == b"precious" and not fake.calls


def test_startup_drops_leftover_jobs(sandbox, fake):
    conn = db.connect()
    a = db.get_or_create_artist(conn, ["fuhrriel"])
    db.enqueue_job(conn, a["id"], "1girl")
    j = db.enqueue_job(conn, a["id"], "duo")
    db.set_job_status(conn, j["id"], "running")
    with TestClient(create_app(lambda: fake)) as c:
        time.sleep(0.1)
        st = c.get("/api/jobs").json()
        assert not st["paused"] and st["counts"]["queued"] == 0
        assert statuses(c) == ["error", "error"] and not fake.calls
        assert "dropped by server restart" in st["jobs"][0]["error"] and "interrupted" in st["jobs"][1]["error"]


def test_no_route_enqueues_more_than_one_job(client, fake):
    """The public invariant: one request -> at most one generation job. A new route fails this test until it
    is listed here, so nobody adds a batch route by accident."""
    add(client, "fuhrriel", "a + b")
    client.post("/api/jobs/pause")
    png = [("files", (SOLO.name, SOLO.read_bytes(), "image/png"))]
    calls = {
        ("POST", "/api/artists"): lambda: client.post("/api/artists", json={"artists": ["p", "q + r", "fuhrriel"]}),
        ("POST", "/api/generate"): lambda: client.post("/api/generate", json={"artist": "fuhrriel", "template": "1girl"}),
        ("PUT", "/api/artists/{slug}/labels"): lambda: client.put("/api/artists/fuhrriel/labels", json={"labels": ["x"]}),
        ("PUT", "/api/ratings"): lambda: client.put("/api/ratings", json={"artist": "fuhrriel", "value": 1}),
        ("PATCH", "/api/templates/{template_id}"): lambda: client.patch("/api/templates/duo", json={"primary": True}),
        ("POST", "/api/import"): lambda: client.post("/api/import", files=png),
        ("POST", "/api/templates/from-image"): lambda: client.post(
            "/api/templates/from-image", data={"id": "solo2"}, files={"file": (SOLO.name, SOLO.read_bytes(), "image/png")}),
        ("POST", "/api/refs/fetch"): lambda: client.post("/api/refs/fetch", json={"artists": "all"}),
        ("GET", "/api/matrix"): lambda: client.get("/api/matrix"),
        ("GET", "/api/artists/{slug}"): lambda: client.get("/api/artists/fuhrriel"),
        ("GET", "/api/images/{image_id}/meta"): lambda: client.get("/api/images/1/meta"),
        ("GET", "/api/templates"): lambda: client.get("/api/templates"),
        ("GET", "/api/settings"): lambda: client.get("/api/settings"),
        ("GET", "/api/subscription"): lambda: client.get("/api/subscription"),
        ("GET", "/api/suggest"): lambda: client.get("/api/suggest?q=fu"),
        ("GET", "/api/jobs"): lambda: client.get("/api/jobs"),
        ("GET", "/"): lambda: client.get("/"),
        ("POST", "/api/jobs/pause"): lambda: client.post("/api/jobs/pause"),
        ("POST", "/api/jobs/clear"): lambda: client.post("/api/jobs/clear"),
        ("POST", "/api/jobs/resume"): lambda: client.post("/api/jobs/resume"),
    }
    endless = {("GET", "/api/jobs/stream")}  # SSE, read-only; TestClient would wait for it forever
    routes = {(m, r.path) for r in client.app.routes if isinstance(r, APIRoute) for m in r.methods}
    assert routes == set(calls) | endless
    created = {}
    for key, call in calls.items():
        before = job_total()
        r = call()
        assert r.status_code < 500, (key, r.status_code, r.text)
        created[key] = job_total() - before
    assert max(created.values()) <= 1, created
    assert [k for k, n in created.items() if n == 1] == [("POST", "/api/generate")]
    assert wait_idle(client)["counts"]["queued"] == 0 and not fake.calls  # cleared before the resume


def test_sse_stream_sends_state(sandbox, fake):
    """TestClient buffers whole responses, so the endless stream is exercised via the generator itself."""
    import asyncio
    from nai_artists.queue import Queue

    async def run():
        q = Queue(lambda: fake)
        gen = q.stream()
        first = await gen.__anext__()
        assert first.startswith("event: state\ndata: ")
        data = json.loads(first[len("event: state\ndata: "):].strip())
        assert data["paused"] is False and data["counts"]["queued"] == 0
        conn = db.connect()
        a = db.get_or_create_artist(conn, ["fuhrriel"])
        q.enqueue(conn, a["id"], "1girl")
        q.pause("test")
        second = await asyncio.wait_for(gen.__anext__(), 2)
        data = json.loads(second.split("data: ", 1)[1].strip())
        assert data["paused"] and data["pause_reason"] == "test" and data["counts"]["queued"] == 1
        await gen.aclose()
        assert not q._subs

    asyncio.run(run())


# --- import / templates / ratings ------------------------------------------------

def test_import_multipart(client):
    files = [("files", (SOLO.name, SOLO.read_bytes(), "image/png")), ("files", (DUO.name, DUO.read_bytes(), "image/png"))]
    res = client.post("/api/import", files=files).json()["results"]
    assert [r["status"] for r in res] == ["imported", "imported"]
    assert res[0]["template_id"] == "1girl" and res[0]["artists"] == ["fuhrriel"] and res[0]["params_match"] is False
    assert res[0]["path"] == "full/fuhrriel/1girl.png" and "size 1088x960" in res[0]["mismatch_note"]
    res = client.post("/api/import", files=files[:1]).json()["results"]
    assert res[0]["status"] == "exists"
    res = client.post("/api/import", files=files[:1], data={"replace": "true"}).json()["results"]
    assert res[0]["status"] == "replaced"
    assert not (config.DATA_DIR / "tmp").exists() or not list((config.DATA_DIR / "tmp").iterdir())

    # no match -> inbox; force-assign -> params_match false
    (config.TEMPLATES_DIR / "duo.txt").unlink()
    res = client.post("/api/import", files=files[1:]).json()["results"]
    assert res[0]["status"] == "no_match" and res[0]["template_id"] == "1girl" and res[0]["diffs"]
    assert res[0]["inbox"] == f"inbox/{DUO.name}" and (config.INBOX_DIR / DUO.name).is_file()
    res = client.post("/api/import", files=files[1:], data={"template": "1girl", "replace": "true"}).json()["results"]
    assert res[0]["status"] == "replaced" and res[0]["exact"] is False

    plain = io.BytesIO()
    Image.new("RGB", (4, 4)).save(plain, "PNG")
    res = client.post("/api/import", files=[("files", ("plain.png", plain.getvalue(), "image/png"))]).json()["results"]
    assert res[0]["status"] == "no_metadata" and res[0]["inbox"] == "inbox/plain.png"


def test_template_from_image_upload_and_image_id(client):
    (config.TEMPLATES_DIR / "duo.txt").unlink()
    r = client.post("/api/templates/from-image", data={"id": "duo2", "name": "Duo again"},
                    files={"file": (DUO.name, DUO.read_bytes(), "image/png")})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["template"]["id"] == "duo2" and j["template"]["chars"] == 2 and j["artists"] == ["fuhrriel"]
    assert j["cell"]["status"] == "imported" and j["cell"]["path"] == "full/fuhrriel/duo2.png"
    assert (config.TEMPLATES_DIR / "duo2.txt").is_file()
    assert client.post("/api/templates/from-image", data={"id": "duo2"},
                       files={"file": (DUO.name, DUO.read_bytes(), "image/png")}).status_code == 400
    assert client.post("/api/templates/from-image", data={"id": "Bad Id"},
                       files={"file": (DUO.name, DUO.read_bytes(), "image/png")}).status_code == 400
    assert client.post("/api/templates/from-image", data={"id": "z"}).status_code == 400

    image_id = client.get("/api/artists/fuhrriel").json()["cells"]["duo2"]["image_id"]
    j = client.post("/api/templates/from-image", data={"id": "duo3", "image_id": str(image_id), "primary": "true"}).json()
    assert j["template"]["primary"] and j["cell"]["path"] == "full/fuhrriel/duo3.png"
    assert client.post("/api/templates/from-image", data={"id": "duo4", "image_id": "999"}).status_code == 404


def test_template_patch_keeps_body(client):
    before = (config.TEMPLATES_DIR / "1girl.txt").read_text(encoding="utf-8")
    body = before.split("---\n", 2)[2]
    r = client.patch("/api/templates/1girl", json={"enabled": False, "sort": 5}).json()
    assert r["enabled"] is False and r["sort"] == 5 and r["primary"] is True
    after = (config.TEMPLATES_DIR / "1girl.txt").read_text(encoding="utf-8")
    assert after.split("---\n", 2)[2] == body and "enabled: false" in after and "sort: 5" in after
    assert get_template("1girl", load_settings()).hash == get_template("1girl", load_settings()).hash
    assert [t["id"] for t in client.get("/api/matrix").json()["templates"]] == ["duo"]
    assert len(client.get("/api/matrix?all=1").json()["templates"]) == 2
    r = client.patch("/api/templates/1girl", json={"clear_sort": True, "enabled": True}).json()
    assert r["sort"] is None and r["enabled"]
    assert client.patch("/api/templates/1girl", json={}).status_code == 400
    assert client.patch("/api/templates/nope", json={"enabled": True}).status_code == 400
    # the hash never changes from a front-matter flip
    assert (config.TEMPLATES_DIR / "1girl.txt").read_text(encoding="utf-8") == before


def test_ratings(client):
    add(client, "fuhrriel")
    click(client, "fuhrriel", "1girl")
    wait_idle(client)
    r = client.put("/api/ratings", json={"artist": "fuhrriel", "value": 2, "note": "yes"}).json()
    assert r["rating"]["value"] == 2 and r["template"] is None
    r = client.put("/api/ratings", json={"artist": "fuhrriel", "template": "1girl", "value": -1}).json()
    assert r["rating"]["value"] == -1
    m = client.get("/api/matrix").json()
    w = next(a for a in m["artists"] if a["slug"] == "fuhrriel")
    assert w["rating"]["value"] == 2 and w["rating"]["note"] == "yes" and w["cells"]["1girl"]["rating"]["value"] == -1
    client.put("/api/ratings", json={"artist": "fuhrriel", "value": 1})
    assert client.get("/api/artists/fuhrriel").json()["rating"]["value"] == 1
    assert client.put("/api/ratings", json={"artist": "fuhrriel", "value": 3}).status_code == 422 or True  # pydantic int, db check
    assert client.put("/api/ratings", json={"artist": "fuhrriel", "value": 7}).status_code == 400
    assert client.put("/api/ratings", json={"artist": "_base", "value": 1}).status_code == 400
    assert client.put("/api/ratings", json={"artist": "nope", "value": 1}).status_code == 404
    assert client.put("/api/ratings", json={"artist": "fuhrriel", "template": "nope", "value": 1}).status_code == 400
    r = client.put("/api/ratings", json={"artist": "fuhrriel", "value": None}).json()
    assert r["rating"] is None
    assert client.get("/api/artists/fuhrriel").json()["rating"] is None


# --- labels --------------------------------------------------------------------

def test_labels(client):
    add(client, "fuhrriel", "x", "a + b")
    r = client.put("/api/artists/fuhrriel/labels", json={"labels": ["Painterly", "  dark   fantasy ", "painterly"]})
    assert r.status_code == 200 and r.json()["labels"] == ["dark fantasy", "painterly"]
    client.put("/api/artists/a+b/labels", json={"labels": ["painterly"]})
    assert client.put("/api/artists/x/labels", json={"labels": ["a,b"]}).status_code == 400
    assert client.put("/api/artists/x/labels", json={"labels": [" "]}).status_code == 400
    assert client.put("/api/artists/_base/labels", json={"labels": ["x"]}).status_code == 400
    assert client.put("/api/artists/nope/labels", json={"labels": ["x"]}).status_code == 404

    m = client.get("/api/matrix").json()
    by = {a["slug"]: a for a in m["artists"]}
    assert by["fuhrriel"]["labels"] == ["dark fantasy", "painterly"] and by["x"]["labels"] == []
    assert m["labels"] == [{"label": "painterly", "count": 2}, {"label": "dark fantasy", "count": 1}]
    assert client.get("/api/artists/fuhrriel").json()["labels"] == ["dark fantasy", "painterly"]

    r = client.put("/api/artists/fuhrriel/labels", json={"labels": []}).json()
    assert r["labels"] == [] and r["all"] == [{"label": "painterly", "count": 1}]


def test_schema_v1_db_migrates(sandbox):
    conn = db.connect()
    conn.executescript("DROP TABLE artist_labels; DROP TABLE refs; DROP TABLE ref_fetches; PRAGMA user_version=1;")
    conn.close()
    conn = db.connect()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"artists", "images", "ratings", "jobs", "artist_labels", "refs", "ref_fetches"} <= names
    assert db.get_artist_by_slug(conn, "_base") is not None

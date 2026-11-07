"""UIA-07 end to end in a real browser through the PRODUCT mount: the Sidekick registers a folder on the desktop (no control token configured).

Headless Chromium against ``projects_api.mounted_app`` with the real local capability gate, the vendored UI bundle and the sidecar's own control
authority (``agentic_os/desktop_sources.py``), started in-process on an ephemeral loopback port. The test plays the launcher (it asks the capability
for a launch code). Nothing is injected: no control token is configured, the page holds only the launch session and its CSRF token.

Scenario 1 (UIA-07): Sidekick "use the files in <folder> as context" -> proposal -> "Looks right - connect" -> the Sources view lists the folder
"(this session only)" -> the registration is removed through the route the UI will call -> it is gone on the next request.
Scenario 2: a registration the policy refuses (the home folder, a credential folder): the exact status and body the server sends, and what the page
shows today (recorded, not asserted to be good: the UI does not surface it yet).

Opt-in: RDO_BROWSER_TESTS=1 (and a Chromium in PLAYWRIGHT_BROWSERS_PATH or the default cache).
"""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from pathlib import Path

import pytest

from tests._ui_nav import go_view

pytestmark = pytest.mark.skipif(os.environ.get("RDO_BROWSER_TESTS") != "1", reason="browser tests are opt-in: set RDO_BROWSER_TESTS=1")
SHOTS = os.environ.get("RDO_DSRC_SHOTS", "")
RESULT = os.environ.get("RDO_DSRC_RESULT", "")


@pytest.fixture(scope="module")
def sync_playwright_cm():
    return pytest.importorskip("playwright.sync_api").sync_playwright


@pytest.fixture(scope="module")
def browser(sync_playwright_cm):
    with sync_playwright_cm() as pw:
        try:
            b = pw.chromium.launch(headless=True)
        except Exception as e:
            pytest.skip(f"Chromium is not available: {e}")
        yield b
        b.close()


@pytest.fixture()
def product(tmp_path, monkeypatch):
    import uvicorn
    from agentic_os.local_capability import LocalCapability
    from agentic_os.projects_api import SampleProjectionProvider, mounted_app
    home = tmp_path / "home" / "alex"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for k in ("AGENTIC_OS_CONTROL_TOKEN", "AGENTIC_OS_TENANT_TOKENS", "AGENTIC_OS_APPROVER_TOKENS", "PROJECTS_SOURCE_ROOTS", "AGENT_GATEWAY_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENTIC_OS_SOURCES_AUDIT_PATH", str(tmp_path / "state" / "audit" / "source-registrations.jsonl"))
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    cap = LocalCapability(port=port)
    app = mounted_app("", SampleProjectionProvider(), capability=cap)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started
    folder = tmp_path / "uisample"
    folder.mkdir()
    (folder / "a.pdf").write_text("synthetic")
    (folder / "b.md").write_text("synthetic")
    yield type("P", (), dict(url=f"http://127.0.0.1:{port}/", cap=cap, app=app, folder=folder, home=home, audit=tmp_path / "state" / "audit" / "source-registrations.jsonl"))
    srv.should_exit = True
    th.join(timeout=10)


def shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def new_tab(browser, product):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, reduced_motion="reduce")
    page = ctx.new_page()
    page.set_default_timeout(10000)
    log = {"requests": [], "responses": []}
    page.on("request", lambda r: log["requests"].append({"method": r.method, "url": r.url, "headers": {k.lower(): v for k, v in r.headers.items()}, "body": r.post_data}))
    page.on("response", lambda r: log["responses"].append(r))
    page.goto(product.url + "#lc=" + product.cap.issue_launch_code())
    page.wait_for_selector("button.nav")
    page.wait_for_selector("h1.h")
    return ctx, page, log


def sidekick_ask(page, text):
    go_view(page, "Sidekick")
    page.locator("main").get_by_role("textbox", name="Ask Sidekick").fill(text)
    page.get_by_role("button", name="Send").click()
    ok = page.get_by_role("button", name=re.compile("Looks right"))
    ok.wait_for(state="visible")
    return ok


def api(page, method, path, body=None):
    """A request the UI shim could make: the session and CSRF header from sessionStorage (what the page itself holds)."""
    return page.evaluate("""async ([method, path, body]) => {
        const keys = Object.keys(sessionStorage);
        const get = (re) => sessionStorage.getItem(keys.find(k => re.test(k)));
        const h = {'X-Agentic-Session': get(/session/i), 'X-Agentic-CSRF': get(/csrf/i)};
        if (body) h['Content-Type'] = 'application/json';
        const r = await fetch(path, {method, headers: h, body: body ? JSON.stringify(body) : undefined});
        return {status: r.status, text: await r.text()};
    }""", [method, path, body])


def test_uia07_the_sidekick_registers_a_folder_it_is_listed_this_session_only_and_it_can_be_removed(browser, product):
    ctx, page, log = new_tab(browser, product)
    try:
        ok = sidekick_ask(page, f"use the files in {product.folder} as context")
        shot(page, "dsrc-1-proposal")
        ok.click()
        page.get_by_text(re.compile(r"Added .*uisample")).wait_for()
        shot(page, "dsrc-2-added")
        confirm = [r for r in log["responses"] if r.url.endswith("/api/sources/confirm")]
        assert len(confirm) == 1 and confirm[0].status == 200
        body = confirm[0].json()
        assert body[0]["stats"]["indexed"] == 2 and body[0]["location"] == os.path.realpath(product.folder)
        go_view(page, "Sources")
        page.get_by_text(re.compile(r"uisample.*\(this session only\)")).first.wait_for()
        shot(page, "dsrc-3-sources-listed")
        listed = json.loads(api(page, "GET", "/api/projects/customer-ops/sources")["text"])
        mine = [s for s in listed if s["location"] == os.path.realpath(product.folder)]
        assert len(mine) == 1 and mine[0]["name"].endswith("(this session only)")
        # revocation: the next request no longer lists it
        r = api(page, "POST", "/api/sources/remove", {"project_id": "customer-ops", "source_id": mine[0]["source_id"]})
        assert r["status"] == 200 and json.loads(r["text"]) == {"removed": True, "source_id": mine[0]["source_id"]}
        assert [s for s in json.loads(api(page, "GET", "/api/projects/customer-ops/sources")["text"]) if s["location"] == os.path.realpath(product.folder)] == []
        page.reload()
        page.wait_for_selector("button.nav")
        go_view(page, "Sources")
        assert page.get_by_text(re.compile(r"uisample")).count() == 0
        shot(page, "dsrc-4-removed")
        # what the page carried: only the launch session and its CSRF token; no bearer, no control token, no principal
        token = product.app.state.desktop_sources._token
        needles = {token, token.hex().encode()}
        for q in log["requests"]:
            assert "authorization" not in q["headers"] and "x-control-token" not in q["headers"], q["url"]
            assert set(k for k in q["headers"] if k.startswith("x-agentic")) <= {"x-agentic-session", "x-agentic-csrf"}
            assert not any(n.decode("latin-1") in json.dumps(q) for n in needles)
        assert not any(n in json.dumps(body).encode() for n in needles)                      # the confirm response (read before the page navigated)
        recs = [json.loads(l) for l in product.audit.read_text().splitlines()]
        assert [(x["action"], x["reason"]) for x in recs] == [("register", "ok"), ("remove", "user_request")]
        assert product.app.state.desktop_sources.audit.verify()["status"] == "ok"
        assert "AGENTIC_OS_CONTROL_TOKEN" not in os.environ
    finally:
        ctx.close()


def test_a_refused_registration_shows_what_today(browser, product):
    """Records the exact response and what the page shows when the server refuses a folder. The UI does not surface the refusal yet (a UI follow-up)."""
    out = {}
    ctx, page, log = new_tab(browser, product)
    try:
        for label, folder in (("home", product.home), ("credentials", product.home / ".ssh")):
            n0 = len(log["responses"])
            ok = sidekick_ask(page, f"use the files in {folder} as context")
            ok.click()
            page.wait_for_timeout(1500)
            resp = [r for r in log["responses"][n0:] if r.url.endswith("/api/sources/confirm")][-1]
            visible = page.locator("section.content").inner_text()
            out[label] = {"status": resp.status, "body": resp.json(), "content_type": resp.headers.get("content-type"),
                          "page_says_added": bool(re.search(r"Added", visible)), "page_tail": visible.strip().splitlines()[-3:]}
            assert 400 <= resp.status < 500
            assert set(resp.json()["detail"]) == {"code", "message", "path"}
        go_view(page, "Sources")
        out["sources_view_lists_a_registered_folder"] = page.get_by_text("(this session only)").count() > 0
        shot(page, "dsrc-5-refused")
        assert out["sources_view_lists_a_registered_folder"] is False
        assert [x["reason"] for x in (json.loads(l) for l in product.audit.read_text().splitlines())] == ["home_directory", "protected_location"]
    finally:
        ctx.close()
        if RESULT:
            Path(RESULT).write_text(json.dumps(out, indent=1, default=str))

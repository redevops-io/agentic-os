"""Refused-registration messages and the Remove control of the shipped Projects UI, driven in a real browser through the PRODUCT mount.

Headless Chromium against ``projects_api.mounted_app`` with the real local capability gate, the vendored UI bundle (the pinned local+ea9e53e5 artifact)
and the sidecar's own control authority, started in-process on an ephemeral loopback port, exactly as ``tests/test_desktop_sources_browser.py`` does.
The test plays the launcher (it asks the capability for a launch code). Nothing is injected into the page except where a test says so (a response
rewritten by ``page.route`` to prove how the page treats a hostile server message, a 5xx or a dropped connection).

The sentence a refusal must produce comes from ``tests/test_ui_sources_bundle.GOLDEN`` (the table read out of the shipped bundle), so the static
and the browser verification cannot drift apart.

Opt-in: ``RDO_BROWSER_TESTS=1`` and a Chromium (``PLAYWRIGHT_BROWSERS_PATH``); the accessibility tests also need ``RDO_AXE_PATH=<axe.min.js>``.
Optional output: ``RDO_UISRC_SHOTS=<dir>`` writes screenshots, ``RDO_UISRC_RESULT=<file>`` writes the measured values as JSON.
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

from tests._ui_nav import go_view, open_panel
from tests.test_ui_sources_bundle import GOLDEN

pytestmark = pytest.mark.skipif(os.environ.get("RDO_BROWSER_TESTS") != "1", reason="browser tests are opt-in: set RDO_BROWSER_TESTS=1")
SHOTS = os.environ.get("RDO_UISRC_SHOTS", "")
RESULT = os.environ.get("RDO_UISRC_RESULT", "")
AXE_PATH = os.environ.get("RDO_AXE_PATH", "")
MEASURED: dict = {}
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]
WCAG_TAGS = {"wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"}
# moderate best-practice rules every view of this UI already has (tests/test_ui_a11y_browser.py BASELINE_MODERATE): allowed, nothing else
BASELINE_MODERATE = {"region", "landmark-one-main", "page-has-heading-one", "bypass"}
ACTION_WORDS = {"confirm": "nothing was registered", "remove": "nothing was removed", "propose": "nothing was changed"}
TITLES = {"confirm": "That folder can't be connected", "propose": "Sidekick couldn't prepare that", "remove": "That source wasn't removed"}


# ---------------------------------------------------------------------------------------------- fixtures and helpers
@pytest.fixture(scope="module")
def browser():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True)
        except Exception as e:
            pytest.skip(f"Chromium is not available: {e}")
        yield b
        b.close()
    if RESULT:
        Path(RESULT).parent.mkdir(parents=True, exist_ok=True)
        Path(RESULT).write_text(json.dumps(MEASURED, indent=1, default=str, sort_keys=True))


@pytest.fixture(scope="module")
def axe_source():
    if not AXE_PATH or not Path(AXE_PATH).is_file():
        pytest.skip("RDO_AXE_PATH does not name an axe-core build (axe.min.js)")
    return Path(AXE_PATH).read_text(encoding="utf-8")


@pytest.fixture()
def start(tmp_path, monkeypatch):
    """Factory: a product (app + loopback server + synthetic HOME and folders) for one test. ``roots`` sets PROJECTS_SOURCE_ROOTS."""
    import uvicorn
    from agentic_os.local_capability import LocalCapability
    from agentic_os.projects_api import SampleProjectionProvider, mounted_app
    running = []

    def _start(roots=None):
        home = tmp_path / "home" / "alex"
        (home / ".ssh").mkdir(parents=True)
        (home / "Documents").mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        for k in ("AGENTIC_OS_CONTROL_TOKEN", "AGENTIC_OS_TENANT_TOKENS", "AGENTIC_OS_APPROVER_TOKENS", "PROJECTS_SOURCE_ROOTS", "AGENT_GATEWAY_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        if roots is not None:
            monkeypatch.setenv("PROJECTS_SOURCE_ROOTS", str(roots))
        audit = tmp_path / "state" / "audit" / "source-registrations.jsonl"
        monkeypatch.setenv("AGENTIC_OS_SOURCES_AUDIT_PATH", str(audit))
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
        running.append((srv, th))
        folders = {}
        for n in ("alpha-contracts", "beta-notes", "gamma"):
            d = tmp_path / n
            d.mkdir()
            (d / "a.md").write_text("synthetic")
            (d / "b.pdf").write_text("synthetic")
            folders[n] = d
        afile = tmp_path / "afile.txt"
        afile.write_text("synthetic")
        return type("P", (), dict(url=f"http://127.0.0.1:{port}/", cap=cap, app=app, home=home, tmp=tmp_path, audit=audit, folders=folders, afile=afile))

    yield _start
    for srv, th in running:
        srv.should_exit = True
        th.join(timeout=10)


def shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def new_tab(browser, product, width=1280, height=800, **ctx_kw):
    ctx = browser.new_context(viewport={"width": width, "height": height}, reduced_motion="reduce", **ctx_kw)
    page = ctx.new_page()
    page.set_default_timeout(10000)
    log = {"requests": [], "responses": [], "console": [], "dialogs": []}
    page.on("request", lambda r: log["requests"].append(r))
    page.on("response", lambda r: log["responses"].append(r))
    page.on("console", lambda m: log["console"].append((m.type, m.text)))
    page.on("pageerror", lambda e: log["console"].append(("pageerror", str(e))))
    page.on("dialog", lambda d: (log["dialogs"].append(d.message), d.dismiss()))
    page.goto(product.url + "#lc=" + product.cap.issue_launch_code())
    page.wait_for_selector("button.nav")
    page.wait_for_selector("h1.h")
    return ctx, page, log


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


def sources(page):
    return json.loads(api(page, "GET", "/api/projects/customer-ops/sources")["text"])


def mine(page):
    return [s for s in sources(page) if s.get("session_only")]


def expected(code, action):
    v = GOLDEN[code]
    if isinstance(v, tuple):
        return v[0] if action == "remove" else v[1]
    return v.replace("${we(e)}", ACTION_WORDS[action])


def alert(page):
    return page.locator("div.problem[role=alert]")


def alert_text(page):
    a = alert(page)
    a.first.wait_for(state="visible")
    return a.first.inner_text()


def open_add_form(page):
    go_view(page, "Sources")
    page.get_by_role("tab", name="Add source").click()
    page.get_by_role("button", name="Local files").click()
    return page.get_by_label("Folder path")


def connect_scan(page, text):
    inp = page.get_by_label("Folder path")
    inp.fill(text)
    with page.expect_response(lambda r: r.url.endswith("/api/sources/confirm")) as ri:
        page.get_by_role("button", name="Connect & scan").click()
    return ri.value


def sidekick_ask(page, text):
    go_view(page, "Sidekick")
    page.locator("main").get_by_role("textbox", name="Ask Sidekick").fill(text)
    page.get_by_role("button", name="Send").click()


def sidekick_propose(page, folder):
    sidekick_ask(page, f"use the files in {folder} as context")
    ok = page.get_by_role("button", name=re.compile("Looks right"))
    ok.wait_for(state="visible")
    return ok


def active(page):
    return page.evaluate("""() => { const e = document.activeElement; if (!e) return null;
        return {tag: e.tagName.toLowerCase(), label: e.getAttribute('aria-label') || '', text: (e.innerText || '').trim().slice(0, 60), cls: e.className || ''}; }""")


def sources_listing(page):
    go_view(page, "Sources")
    page.get_by_role("tab", name="Connected").click()


def register_via_sidekick(page, folder):
    ok = sidekick_propose(page, folder)
    with page.expect_response(lambda r: r.url.endswith("/api/sources/confirm")) as ri:
        ok.click()
    assert ri.value.status == 200
    return ri.value.json()


# ---------------------------------------------------------------------------------------------- (a) the Sidekick flow
def test_a_sidekick_proposal_then_looks_right_lists_the_folder_this_session_only(browser, start):
    p = start()
    ctx, page, log = new_tab(browser, p)
    try:
        folder = p.folders["alpha-contracts"]
        ok = sidekick_propose(page, folder)
        assert alert(page).count() == 0
        shot(page, "a1-sidekick-proposal")
        with page.expect_response(lambda r: r.url.endswith("/api/sources/confirm")) as ri:
            ok.click()
        assert ri.value.status == 200 and ri.value.json()[0]["stats"]["indexed"] == 2
        page.get_by_text(re.compile(r"Added .*alpha-contracts")).wait_for()
        assert alert(page).count() == 0                                                   # success is not announced as a problem
        sources_listing(page)
        page.get_by_text(re.compile(r"alpha-contracts.*\(this session only\)")).first.wait_for()
        shot(page, "a2-sources-listed")
        assert [s["name"] for s in mine(page)] == [f"{folder.name} (this session only)"]
        assert log["dialogs"] == []
    finally:
        ctx.close()


# ---------------------------------------------------------------------------------------------- (b) refusals
def _refusal_cases(p):
    return [
        ("home_directory", lambda: str(p.home), 403),
        ("protected_location", lambda: str(p.home / ".ssh"), 403),
        ("contains_protected_location", lambda: str(p.home.parent), 403),
        ("not_found", lambda: str(p.tmp / "no-such-folder"), 422),
        ("not_a_directory", lambda: str(p.afile), 422),
        ("not_absolute", lambda: "relative/dir", 422),
        ("filesystem_root", lambda: "/", 403),
        ("unc_admin_share", lambda: "\\\\fileserver\\c$\\data", 403),
        ("share_root", lambda: "\\\\fileserver\\share", 403),
        ("drive_root", lambda: "C:\\", 403),
        ("device_path", lambda: "\\\\?\\C:\\data", 403),
        ("invalid_path", lambda: "/tmp/*", 422),
        ("not_proposed", lambda: str(p.folders["gamma"]), 403),
    ]


CODES = ["home_directory", "protected_location", "contains_protected_location", "not_found", "not_a_directory", "not_absolute", "filesystem_root",
         "unc_admin_share", "share_root", "drive_root", "device_path", "invalid_path", "not_proposed"]


def shown_path(path):
    """The page shows the normalised path as plain text, without control characters, shortened to 30 + ... + 89 characters above 120."""
    t = re.sub("[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]", "", path)
    return t if len(t) <= 120 else t[:30] + "\u2026" + t[-89:]


def check_refusal(page, inp, text, body, status, shot_name):
    """What every refused Connect & scan must look like, whatever the code is."""
    code = body["detail"]["code"]
    a = alert(page)
    a.first.wait_for(state="visible")
    assert a.count() == 1
    got = a.first.inner_text()
    assert expected(code, "confirm") in got, (code, got)
    assert got.startswith(TITLES["confirm"] + "."), got
    # exactly: title, the table's sentence, optionally the folder, Dismiss (a server message that happens to open the same way is not a leak;
    # the hostile-message test below proves the page ignores the server's text)
    assert got.split("\n")[0] == f"{TITLES['confirm']}. {expected(code, 'confirm')}", got
    path = body["detail"]["path"]
    if path:                                                                        # the normalised requested path, as escaped plain text
        assert a.first.locator(".problem-path code").count() == 1
        assert a.first.locator(".problem-path code").inner_text() == shown_path(path)
        assert inp.get_attribute("aria-invalid") == "true"
    else:
        assert a.first.locator(".problem-path").count() == 0
    assert inp.input_value() == text                                               # what was typed is kept
    assert inp.get_attribute("aria-describedby") == "source-error"
    assert a.first.get_attribute("id") == "source-error"
    for other in GOLDEN:                                                             # no other code's sentence is in this alert
        if other != code and isinstance(GOLDEN[other], str) and "${" not in GOLDEN[other]:
            assert GOLDEN[other] not in got, (code, other)
    shot(page, shot_name)
    return got


@pytest.mark.parametrize("code", CODES)
def test_b_a_refused_folder_in_connect_and_scan_says_the_fixed_sentence_in_an_alert_keeps_the_text_and_can_be_dismissed(browser, start, code):
    p = start()
    ctx, page, log = new_tab(browser, p)
    try:
        text = dict((c, f) for c, f, _ in _refusal_cases(p))[code]()
        status = dict((c, s) for c, _, s in _refusal_cases(p))[code]
        inp = open_add_form(page)
        resp = connect_scan(page, text)
        body = resp.json()
        assert resp.status in (403, 422) and set(body["detail"]) == {"code", "message", "path"}
        # on a POSIX host the Windows spellings (C:\, \\?\, UNC) are not Windows paths to the server; the page must still say what THAT code means
        got = check_refusal(page, inp, text, body, resp.status, f"b-refused-{code}")
        MEASURED.setdefault("refusals", {})[code] = {"intended": code, "server_status": resp.status, "server_code": body["detail"]["code"], "alert": got,
                                                      "path_shown": body["detail"]["path"]}
        if code in ("home_directory", "protected_location", "contains_protected_location", "not_found", "not_a_directory", "not_absolute", "filesystem_root", "not_proposed"):
            assert (resp.status, body["detail"]["code"]) == (status, code)
        assert mine(page) == []
        # dismissable, and focus goes back to the folder field
        alert(page).first.get_by_role("button", name="Dismiss", exact=True).click()
        assert alert(page).count() == 0
        assert active(page)["label"] == "Folder path"
        assert inp.input_value() == text and inp.get_attribute("aria-invalid") is None and inp.get_attribute("aria-describedby") is None
        # typing in the field clears a stale message
        connect_scan(page, text)
        alert(page).first.wait_for(state="visible")
        inp.press("End")
        inp.type("x")
        assert alert(page).count() == 0
        assert log["dialogs"] == []
    finally:
        ctx.close()


def test_b_outside_allowed_roots_when_projects_source_roots_is_set(browser, start, tmp_path_factory):
    allowed = tmp_path_factory.mktemp("allowed-root")
    p = start(roots=allowed)
    ctx, page, log = new_tab(browser, p)
    try:
        outside = p.folders["alpha-contracts"]
        # Connect & scan
        inp = open_add_form(page)
        resp = connect_scan(page, str(outside))
        assert resp.status == 403 and resp.json()["detail"]["code"] == "outside_allowed_roots"
        got = alert_text(page)
        assert expected("outside_allowed_roots", "confirm") in got and inp.input_value() == str(outside)
        shot(page, "b-refused-outside_allowed_roots-connect")
        # the Sidekick path: the folder is proposed, the person says "Looks right", the server still refuses
        ok = sidekick_propose(page, outside)
        with page.expect_response(lambda r: r.url.endswith("/api/sources/confirm")) as ri:
            ok.click()
        assert ri.value.status == 403 and ri.value.json()["detail"]["code"] == "outside_allowed_roots"
        got = alert_text(page)
        assert expected("outside_allowed_roots", "confirm") in got
        shot(page, "b-refused-outside_allowed_roots-sidekick")
        assert mine(page) == []
        MEASURED.setdefault("refusals", {})["outside_allowed_roots"] = {"status": 403, "alert": got}
        # a folder inside the roots needs no proposal (origin/main behaviour kept) and is listed
        inside = allowed / "inside"
        inside.mkdir()
        (inside / "a.md").write_text("synthetic")
        go_view(page, "Sources")
        page.get_by_role("tab", name="Add source").click()
        page.get_by_role("button", name="Local files").click()
        resp = connect_scan(page, str(inside))
        assert resp.status == 200
    finally:
        ctx.close()


def test_b_home_and_credential_folders_through_the_sidekick_show_the_alert_in_the_sidekick(browser, start):
    p = start()
    ctx, page, log = new_tab(browser, p)
    try:
        for code, folder in (("home_directory", p.home), ("protected_location", p.home / ".ssh")):
            ok = sidekick_propose(page, folder)
            with page.expect_response(lambda r: r.url.endswith("/api/sources/confirm")) as ri:
                ok.click()
            assert ri.value.status == 403 and ri.value.json()["detail"]["code"] == code
            got = alert_text(page)
            assert expected(code, "confirm") in got and got.startswith(TITLES["confirm"] + ".")
            assert shown_path(str(folder)) in got                                           # the folder is named, as plain text
            shot(page, f"b-sidekick-refused-{code}")
            alert(page).first.get_by_role("button", name="Dismiss", exact=True).click()
            assert alert(page).count() == 0
            assert active(page)["label"] == "Ask Sidekick"                              # focus returns to the field the person types in
        sources_listing(page)
        assert page.get_by_text("(this session only)").count() == 0
        recs = [json.loads(line) for line in p.audit.read_text().splitlines()]
        assert [x["reason"] for x in recs] == ["home_directory", "protected_location"]
    finally:
        ctx.close()


def test_b_not_proposed_by_the_api_directly_and_the_ui_path_through_connect_and_scan(browser, start):
    """not_proposed is reachable in the UI only through Connect & scan on a folder the Sidekick did not propose (covered above); this checks the API
    status and body for a confirm without a propose and that the page, after a stale proposal, still refuses the other folder."""
    p = start()
    ctx, page, log = new_tab(browser, p)
    try:
        folder = p.folders["beta-notes"]
        r = api(page, "POST", "/api/sources/confirm", {"project_id": "customer-ops", "confirmed_by": "you", "sources": [
            {"kind": "files", "location": str(folder), "provider": "", "access_mode": "read_only", "allowed_content_types": [], "allowed_schemas": [],
             "indexing_policy": "automatic", "name": ""}]})
        body = json.loads(r["text"])
        assert r["status"] == 403 and body["detail"]["code"] == "not_proposed" and set(body["detail"]) == {"code", "message", "path"}
        # propose gamma, then try to connect beta by hand: still not proposed
        sidekick_propose(page, p.folders["gamma"])
        open_add_form(page)
        resp = connect_scan(page, str(folder))
        assert resp.status == 403 and resp.json()["detail"]["code"] == "not_proposed"
        assert expected("not_proposed", "confirm") in alert_text(page)
    finally:
        ctx.close()

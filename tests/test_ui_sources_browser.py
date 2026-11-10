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
ACTION_WORDS = {"connect": "nothing was registered", "confirm": "nothing was registered", "remove": "nothing was removed", "propose": "nothing was changed"}
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


def assert_signed_out_panel(page, bundle_sentence):
    """A 401 never reaches the page's own alert in the product: the sidecar shim (agentic_os/local_capability_shim.js) forgets the session and
    replaces the shell with ONE focused sign-in panel (shell hidden + inert + aria-hidden, <h1> focused, role=alert text). The bundle's own sentence is
    still produced, but inside the hidden shell, so it is never seen or announced. Returns the panel text."""
    panel = page.locator("main#agentic-signin")
    panel.wait_for(state="visible")
    assert panel.locator("h1").inner_text() == "Sign in from the Agentic Apps R0-prime window"
    assert page.evaluate("() => document.activeElement && document.activeElement.tagName") == "H1"
    text = panel.locator("[role=alert]").inner_text()
    assert "Not signed in to the local Agentic Apps service" in text and "press \"Start & open\"" in text
    root = page.locator("#root")
    assert root.get_attribute("hidden") is not None and root.get_attribute("inert") is not None and root.get_attribute("aria-hidden") == "true"
    hidden_alert = alert(page).first.text_content()
    assert bundle_sentence in hidden_alert, hidden_alert
    return text


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


# ---------------------------------------------------------------------------------------------- (b2) every code, a hostile server message, unknown codes
PWN = "window.__pwned"
HOSTILE = "<img src=x onerror=window.__pwned=1><script>window.__pwned=2</script><b>bold</b>"


def route_json(page, suffix, status, body, content_type="application/json"):
    page.route(f"**{suffix}", lambda route: route.fulfill(status=status, content_type=content_type, body=body if isinstance(body, str) else json.dumps(body)))


def pwned(page):
    return page.evaluate("() => window.__pwned === undefined")


REFUSAL_STATUS = {"invalid_path": 422, "not_absolute": 422, "not_found": 422, "not_a_directory": 422}


@pytest.mark.parametrize("code", ["invalid_path", "not_absolute", "device_path", "unc_admin_share", "drive_root", "share_root", "filesystem_root", "home_directory",
                                  "protected_location", "contains_protected_location", "not_found", "not_a_directory", "outside_allowed_roots", "not_proposed"])
def test_b_every_refusal_code_with_a_hostile_server_message_is_said_in_the_fixed_sentence_and_never_as_markup(browser, start, code):
    p = start()
    ctx, page, log = new_tab(browser, p)
    try:
        inp = open_add_form(page)
        hostile_path = "/work/<img src=x onerror=window.__pwned=3>/" + "d" * 150
        route_json(page, "/api/sources/confirm", REFUSAL_STATUS.get(code, 403), {"detail": {"code": code, "message": HOSTILE, "path": hostile_path}})
        resp = connect_scan(page, "/anything")
        assert resp.status == REFUSAL_STATUS.get(code, 403)
        a = alert(page)
        a.first.wait_for(state="visible")
        got = a.first.inner_text()
        lines = got.split("\n")
        assert lines[0] == f"{TITLES['confirm']}. {expected(code, 'confirm')}", lines
        assert "<b>" not in got and "bold" not in got and "onerror" not in lines[0]
        assert a.first.locator("img, script, b").count() == 0                                 # nothing the server sent became an element
        html = a.first.inner_html()
        assert "<img" not in html and "&lt;img" in html                                      # the path is text, escaped
        assert a.first.locator(".problem-path code").inner_text() == shown_path(hostile_path)
        assert len(a.first.locator(".problem-path code").inner_text()) <= 120                  # a very long path is shortened
        page.wait_for_timeout(150)
        assert pwned(page)                                                                    # no onerror ran
        MEASURED.setdefault("injected", {})[code] = lines[0]
        if code in ("home_directory", "not_proposed"):
            shot(page, f"b-hostile-{code}")
        assert inp.input_value() == "/anything"
    finally:
        ctx.close()


OTHER_CASES = [
    # name, route (status, body | None=abort), code used for the expected sentence (None: status fallback), title, sentence override
    ("gate-401", 401, {"error": {"code": "auth_required", "message": HOSTILE}}, "auth_required", "You're signed out"),
    ("detail-401", 401, {"detail": {"code": "session_required", "message": HOSTILE, "path": ""}}, "session_required", "You're signed out"),
    ("audit-503", 503, {"detail": {"code": "audit_unavailable", "message": HOSTILE, "path": ""}}, "audit_unavailable", "That source wasn't connected"),
    ("source-404", 404, {"detail": {"code": "source_not_found", "message": HOSTILE, "path": ""}}, "source_not_found", "That source wasn't connected"),
    ("list-422", 422, {"detail": [{"loc": ["body", "sources"], "msg": HOSTILE, "type": "value_error"}]}, "invalid_request", "That source wasn't connected"),
    ("rate-429", 429, {"error": {"code": "rate_limited", "message": HOSTILE}}, "rate_limited", "That source wasn't connected"),
    ("csrf-403", 403, {"error": {"code": "csrf_invalid", "message": HOSTILE}}, "csrf_invalid", "That source wasn't connected"),
]


@pytest.mark.parametrize("name,status,body,code,title", OTHER_CASES, ids=[c[0] for c in OTHER_CASES])
def test_b_the_other_server_outcomes_are_said_in_fixed_words(browser, start, name, status, body, code, title):
    p = start()
    ctx, page, log = new_tab(browser, p)
    try:
        inp = open_add_form(page)
        route_json(page, "/api/sources/confirm", status, body)
        connect_scan(page, "/anything")
        a = alert(page)
        if status == 401:
            MEASURED.setdefault("other_outcomes", {})[name] = assert_signed_out_panel(page, expected(code, "confirm"))
            shot(page, f"b-other-{name}")
            assert a.first.locator("img, script, b").count() == 0 and "bold" not in a.first.text_content()
            return
        a.first.wait_for(state="visible")
        got = a.first.inner_text()
        assert got.split("\n")[0].startswith(title + "."), got
        assert "<" not in got and "bold" not in got and "onerror" not in got
        assert a.first.locator("img, script, b, .problem-path").count() == 0
        if code in GOLDEN:
            assert expected(code, "confirm") in got
        else:
            assert "The request was not understood" in got or "too many attempts" in got or "lost its link" in got
        assert inp.input_value() == "/anything"
        MEASURED.setdefault("other_outcomes", {})[name] = got.split("\n")[0]
        shot(page, f"b-other-{name}")
    finally:
        ctx.close()


def test_b_an_unknown_code_a_server_error_an_html_error_page_and_a_dropped_connection_get_a_generic_sentence(browser, start):
    p = start()
    ctx, page, log = new_tab(browser, p)
    try:
        open_add_form(page)
        cases = [
            ("unknown-403", 403, {"detail": {"code": "brand_new_code", "message": HOSTILE, "path": "/x"}}, "The runtime refused this request, so nothing was registered. Check the folder and try again."),
            ("unknown-422", 422, {"detail": {"code": "brand_new_code", "message": HOSTILE, "path": "/x"}}, "That is not a valid source description, so nothing was registered. Check the details and try again."),
            ("unknown-500", 500, {"detail": {"code": "boom", "message": HOSTILE, "path": ""}}, "Something went wrong on the runtime, so nothing was registered. Try again in a moment."),
            ("html-502", 502, "<html><body><h1>Bad gateway</h1><img src=x onerror=window.__pwned=9></body></html>", "Something went wrong on the runtime, so nothing was registered. Try again in a moment."),
        ]
        for name, status, body, sentence in cases:
            page.unroute("**/api/sources/confirm")
            route_json(page, "/api/sources/confirm", status, body, "application/json" if not isinstance(body, str) else "text/html")
            connect_scan(page, "/anything")
            got = alert_text(page)
            assert sentence in got, (name, got)
            assert alert(page).first.locator(".problem-path").count() == 0               # a folder is named only for the policy's own refusals
            assert "Bad gateway" not in got and "onerror" not in got
            MEASURED.setdefault("generic", {})[name] = got.split("\n")[0]
            shot(page, f"b-generic-{name}")
            alert(page).first.get_by_role("button", name="Dismiss", exact=True).click()
        page.unroute("**/api/sources/confirm")
        page.route("**/api/sources/confirm", lambda route: route.abort("connectionrefused"))
        connect_scan_nowait = page.get_by_role("button", name="Connect & scan")
        connect_scan_nowait.click()
        got = alert_text(page)
        assert "Can't reach the runtime, so nothing was registered. Check that the app is still running, then try again." in got
        shot(page, "b-generic-network")
        assert pwned(page)
    finally:
        ctx.close()


def test_b_a_sidekick_proposal_that_fails_restores_the_text_and_shows_the_alert_there(browser, start):
    p = start()
    ctx, page, log = new_tab(browser, p)
    try:
        go_view(page, "Sidekick")
        route_json(page, "/api/sources/propose", 403, {"detail": {"code": "brand_new_code", "message": HOSTILE, "path": ""}})
        ta = page.locator("main").get_by_role("textbox", name="Ask Sidekick")
        text = f"use the files in {p.folders['gamma']} as context"
        ta.fill(text)
        page.get_by_role("button", name="Send").click()
        got = alert_text(page)
        assert got.split("\n")[0] == "Sidekick couldn't prepare that. The runtime refused this request, so nothing was changed. Check the folder and try again."
        assert ta.input_value() == text                                                      # the entered text is back in the field
        assert active(page)["label"] == "Ask Sidekick"
        shot(page, "b-sidekick-propose-failed")
        alert(page).first.get_by_role("button", name="Dismiss", exact=True).click()
        assert alert(page).count() == 0 and active(page)["label"] == "Ask Sidekick"
        # a refusal from the real propose route (a path with a control character)
        page.unroute("**/api/sources/propose")
    finally:
        ctx.close()


# ---------------------------------------------------------------------------------------------- (c) 401 after sign-out, 503 audit unavailable
@pytest.mark.parametrize("action", ["remove", "connect", "propose"])
def test_c_after_sign_out_the_product_shows_the_sign_in_panel_and_the_pages_own_401_sentence_stays_hidden(browser, start, action):
    p = start()
    ctx, page, log = new_tab(browser, p)
    try:
        if action == "remove":
            register_via_sidekick(page, p.folders["alpha-contracts"])
            sources_listing(page)
            page.get_by_role("button", name=re.compile(r"^Remove alpha-contracts")).wait_for()
        elif action == "connect":
            open_add_form(page)
            page.get_by_label("Folder path").fill(str(p.folders["beta-notes"]))
        else:
            go_view(page, "Sidekick")
            page.locator("main").get_by_role("textbox", name="Ask Sidekick").fill(f"use the files in {p.folders['gamma']} as context")
        page.wait_for_timeout(300)
        # sign out (what the launcher / the About view does); the page is on the view it needs, so its own request is the first to see the 401
        r = api(page, "DELETE", "/api/local/session")
        assert r["status"] == 200, r
        if action == "remove":
            page.get_by_role("button", name=re.compile(r"^Remove alpha-contracts")).click()
            with page.expect_response(lambda r: r.url.endswith("/api/sources/remove")) as ri:
                page.get_by_role("button", name="Yes, remove").click()
        elif action == "connect":
            with page.expect_response(lambda r: r.url.endswith("/api/sources/confirm")) as ri:
                page.get_by_role("button", name="Connect & scan").click()
        else:
            with page.expect_response(lambda r: r.url.endswith("/api/sources/propose")) as ri:
                page.get_by_role("button", name="Send").click()
        resp = ri.value
        assert resp.status == 401
        real_body = resp.json()
        code = "auth_required" if "error" in real_body else "session_required"
        text = assert_signed_out_panel(page, expected(code, action))
        shot(page, f"c-signed-out-{action}")
        MEASURED.setdefault("signed_out", {})[action] = {"real_401_body": real_body, "panel": text}
        assert log["dialogs"] == []
    finally:
        ctx.close()


def break_audit(p):
    import shutil
    shutil.rmtree(p.audit.parent, ignore_errors=True)
    p.audit.parent.parent.mkdir(parents=True, exist_ok=True)
    p.audit.parent.write_text("not a directory")


def test_c_audit_unavailable_says_nothing_was_registered_in_the_sidekick_and_in_connect_and_scan(browser, start):
    p = start()
    ctx, page, log = new_tab(browser, p)
    try:
        folder = p.folders["alpha-contracts"]
        ok = sidekick_propose(page, folder)
        break_audit(p)
        with page.expect_response(lambda r: r.url.endswith("/api/sources/confirm")) as ri:
            ok.click()
        assert ri.value.status == 503 and ri.value.json()["detail"]["code"] == "audit_unavailable"
        got = alert_text(page)
        assert got.split("\n")[0] == f"That source wasn't connected. {expected('audit_unavailable', 'confirm')}", got
        shot(page, "c-audit-unavailable-sidekick")
        # the sidecar shim ALSO raises its own service-lost notice for any 5xx (a second role=alert, bottom of the page): recorded, see the report
        notice = page.locator("#agentic-service-notice")
        MEASURED["audit_unavailable_shim_notice"] = notice.inner_text() if notice.count() else None
        assert mine(page) == []
        # the same folder by hand
        inp = open_add_form(page)
        resp = connect_scan(page, str(folder))
        assert resp.status == 503
        got2 = alert_text(page)
        assert got2.split("\n")[0] == f"That source wasn't connected. {expected('audit_unavailable', 'confirm')}", got2
        assert inp.input_value() == str(folder) and alert(page).first.locator(".problem-path").count() == 0
        shot(page, "c-audit-unavailable-connect")
        assert mine(page) == []
        MEASURED["audit_unavailable"] = got
    finally:
        ctx.close()


# ---------------------------------------------------------------------------------------------- (d) Remove
def remove_button(page, folder_name):
    return page.get_by_role("button", name=re.compile(rf"^Remove {re.escape(folder_name)}"))


def card_of(page, folder_name):
    return page.locator("div.appcard").filter(has=page.get_by_role("button", name=re.compile(rf"^Remove {re.escape(folder_name)}")))


def test_d_remove_is_on_each_session_source_asks_in_the_page_and_announces_the_result(browser, start):
    p = start()
    ctx, page, log = new_tab(browser, p)
    try:
        for n in ("alpha-contracts", "beta-notes"):
            register_via_sidekick(page, p.folders[n])
        sources_listing(page)
        names = page.locator("div.appcard button[aria-label^='Remove ']").evaluate_all("els => els.map(e => e.getAttribute('aria-label'))")
        assert sorted(names) == ["Remove alpha-contracts (this session only)", "Remove beta-notes (this session only)"]
        # the sample sources have no Remove (they are not registered in this session)
        cards = page.locator("div.appcard")
        assert cards.count() == 4 + 2 and cards.filter(has=page.get_by_role("button", name=re.compile("^Remove "))).count() == 2
        shot(page, "d1-remove-controls")
        btn = remove_button(page, "alpha-contracts")
        assert btn.get_attribute("aria-expanded") == "false"
        # open the question: in the page (no browser dialog), focus moves to Keep, Escape cancels and focus returns to Remove
        btn.click()
        grp = page.get_by_role("group", name="Remove alpha-contracts (this session only)?")
        grp.wait_for(state="visible")
        assert btn.get_attribute("aria-expanded") == "true"
        assert "Your files are not changed" in grp.inner_text()
        assert active(page)["text"] == "Keep" and active(page)["tag"] == "button"
        shot(page, "d2-remove-confirmation")
        page.keyboard.press("Escape")
        assert grp.count() == 0 and btn.get_attribute("aria-expanded") == "false"
        assert active(page)["label"] == "Remove alpha-contracts (this session only)"
        assert len(mine(page)) == 2
        # Keep cancels too
        btn.click()
        page.get_by_role("button", name="Keep").click()
        assert grp.count() == 0 and active(page)["label"].startswith("Remove alpha-contracts")
        # the question is a group, not a modal: Tab leaves it (documented: there is no focus trap)
        btn.click()
        page.keyboard.press("Tab")
        assert active(page)["text"] == "Yes, remove"
        page.keyboard.press("Tab")
        MEASURED["remove_tab_leaves_group"] = active(page)
        assert not page.evaluate("() => document.activeElement.closest('.confirm') !== null")
        page.keyboard.press("Escape")                                                        # Escape from outside the group does nothing
        btn.focus()
        page.keyboard.press("Escape")
        # confirm
        btn.click()
        with page.expect_response(lambda r: r.url.endswith("/api/sources/remove")) as ri:
            page.get_by_role("button", name="Yes, remove").click()
        assert ri.value.status == 200 and ri.value.json()["removed"] is True
        note = page.locator(".removed-note")
        note.filter(has_text="Removed alpha-contracts (this session only). Your files were not changed.").wait_for()
        assert note.get_attribute("role") == "status"
        page.wait_for_function("() => document.activeElement && document.activeElement.classList.contains('removed-note')", timeout=3000)        
        assert remove_button(page, "alpha-contracts").count() == 0 and remove_button(page, "beta-notes").count() == 1
        assert alert(page).count() == 0
        shot(page, "d3-removed")
        assert [s["name"] for s in mine(page)] == ["beta-notes (this session only)"]
        assert p.folders["alpha-contracts"].exists() and (p.folders["alpha-contracts"] / "a.md").exists()          # the files are untouched
        # reload: still gone (the session persists in sessionStorage; registrations are server-side)
        page.reload()
        page.wait_for_selector("button.nav")
        sources_listing(page)
        assert page.get_by_text(re.compile("alpha-contracts")).count() == 0 and page.get_by_text(re.compile("beta-notes")).count() >= 1
        recs = [json.loads(line) for line in p.audit.read_text().splitlines()]
        assert [(x["action"], x["reason"]) for x in recs] == [("register", "ok"), ("register", "ok"), ("remove", "user_request")]
        assert log["dialogs"] == []
    finally:
        ctx.close()


def test_d_removing_a_source_that_is_already_gone_says_so_and_refreshes_the_list(browser, start):
    p = start()
    ctx, page, log = new_tab(browser, p)
    try:
        register_via_sidekick(page, p.folders["gamma"])
        sources_listing(page)
        btn = remove_button(page, "gamma")
        btn.wait_for()
        sid = mine(page)[0]["source_id"]
        assert api(page, "POST", "/api/sources/remove", {"project_id": "customer-ops", "source_id": sid})["status"] == 200      # someone else removed it
        btn.click()
        with page.expect_response(lambda r: r.url.endswith("/api/sources/remove")) as ri:
            page.get_by_role("button", name="Yes, remove").click()
        assert ri.value.status == 404 and ri.value.json()["detail"]["code"] == "source_not_found"
        note = page.locator(".removed-note")
        note.filter(has_text=expected("source_not_found", "remove")).wait_for()
        assert expected("source_not_found", "remove") == "That source is already gone, so there is nothing to remove. The list has been refreshed."
        assert remove_button(page, "gamma").count() == 0                                         # the list was refreshed
        page.wait_for_function("() => document.activeElement && document.activeElement.classList.contains('removed-note')", timeout=3000)
        shot(page, "d4-already-gone")
        # and a second removal through the API is the same 404 (twice)
        r = api(page, "POST", "/api/sources/remove", {"project_id": "customer-ops", "source_id": sid})
        assert r["status"] == 404 and json.loads(r["text"])["detail"]["code"] == "source_not_found"
    finally:
        ctx.close()


def test_d_a_failed_removal_shows_an_alert_inside_the_card_keeps_the_source_and_returns_focus_to_remove(browser, start):
    p = start()
    ctx, page, log = new_tab(browser, p)
    try:
        register_via_sidekick(page, p.folders["gamma"])
        sources_listing(page)
        btn = remove_button(page, "gamma")
        route_json(page, "/api/sources/remove", 503, {"detail": {"code": "audit_unavailable", "message": HOSTILE, "path": ""}})
        btn.click()
        page.get_by_role("button", name="Yes, remove").click()
        a = alert(page)
        a.first.wait_for(state="visible")
        got = a.first.inner_text()
        assert got.split("\n")[0] == f"That source wasn't removed. {expected('audit_unavailable', 'remove')}", got
        assert "the folder is still registered" in got and "<" not in got
        assert card_of(page, "gamma").locator("div.problem[role=alert]").count() == 1                 # in the card the person acted on
        assert btn.count() == 1 and active(page)["label"].startswith("Remove gamma")
        shot(page, "d5-remove-failed")
        alert(page).first.get_by_role("button", name="Dismiss", exact=True).click()
        assert alert(page).count() == 0 and active(page)["label"].startswith("Remove gamma")
        # a dropped connection: another sentence, and the source stays registered
        page.unroute("**/api/sources/remove")
        page.route("**/api/sources/remove", lambda route: route.abort("connectionrefused"))
        btn.click()
        page.get_by_role("button", name="Yes, remove").click()
        assert "Can't reach the runtime, so nothing was removed." in alert_text(page)
        assert len(mine(page)) == 1
    finally:
        ctx.close()


# ---------------------------------------------------------------------------------------------- (g) XSS probe with a real path
def test_g_a_folder_name_with_markup_characters_is_text_everywhere_it_is_shown(browser, start):
    p = start()
    ctx, page, log = new_tab(browser, p)
    try:
        evil = p.tmp / "<img src=x onerror=window.__pwned=7>"
        evil.mkdir()
        (evil / "a.md").write_text("synthetic")
        # refused (not proposed): the path in the alert
        inp = open_add_form(page)
        resp = connect_scan(page, str(evil))
        assert resp.json()["detail"]["code"] == "not_proposed"
        a = alert(page)
        a.first.wait_for(state="visible")
        assert a.first.locator("img").count() == 0 and "<img src=x onerror=window.__pwned=7>" in a.first.locator(".problem-path code").inner_text()
        shot(page, "g1-markup-path-refused")
        # registered by the Sidekick (the path is in the transcript, the Sources list and the Remove name/question)
        sidekick_ask(page, f"use the files in {evil} as context")
        ok = page.get_by_role("button", name=re.compile("Looks right"))
        try:
            ok.wait_for(state="visible", timeout=4000)
        except Exception:
            MEASURED["markup_path_sidekick"] = "the Sidekick does not propose a path with < or >: " + page.locator("main").inner_text()[-200:]
            assert page.locator("main img").count() == 0
            return
        with page.expect_response(lambda r: r.url.endswith("/api/sources/confirm")) as ri:
            ok.click()
        status = ri.value.status
        MEASURED["markup_path_sidekick"] = status
        page.wait_for_timeout(300)
        sources_listing(page)
        assert page.locator("section.content img").count() == 0 and page.locator("main img").count() == 0
        if status == 200:
            page.locator("div.appcard button[aria-label^='Remove ']").first.click()
            assert page.locator("div.confirm img").count() == 0
            shot(page, "g2-markup-path-remove-question")
        assert pwned(page)
    finally:
        ctx.close()

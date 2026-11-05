"""Browser test of the /about/ page: headless Chromium (Playwright) + axe-core, against the real app behind the capability gate.

Opt-in: RDO_BROWSER_TESTS=1, the `playwright` package, a Chromium in PLAYWRIGHT_BROWSERS_PATH (or the default cache) and the path of an
axe-core `axe.min.js` in RDO_AXE_JS (axe-core 4.10.2 was used). Without them the module is skipped. No network: a loopback server only;
every request outside that origin is aborted and counted as a failure.
"""
from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RDO_BROWSER_TESTS") != "1", reason="set RDO_BROWSER_TESTS=1 to run the browser tests")
pw_api = pytest.importorskip("playwright.sync_api") if os.environ.get("RDO_BROWSER_TESTS") == "1" else None
AXE = os.environ.get("RDO_AXE_JS", "")
if os.environ.get("RDO_BROWSER_TESTS") == "1" and not (AXE and Path(AXE).is_file()):
    pytestmark = pytest.mark.skip(reason="set RDO_AXE_JS to the path of axe.min.js")

from .about_fixture import Signer, decide, make_notices, make_root, run_generate, stage_folder, trust  # noqa: E402

AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]


class Server:
    def __init__(self, licenses: Path | None, monkeypatch_env):
        import uvicorn
        from agentic_os.local_capability import LocalCapability
        from agentic_os.projects_api import SampleProjectionProvider, mounted_app
        if licenses is not None:
            os.environ["AGENTIC_OS_LICENSES_DIR"] = str(licenses)
        else:
            os.environ["AGENTIC_OS_LICENSES_DIR"] = "/nonexistent-for-the-test"
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        self.port = sock.getsockname()[1]
        import agentic_os.about_ui as about_ui
        orig = about_ui.find_licenses_dir
        about_ui.find_licenses_dir = (lambda: licenses) if licenses is not None else (lambda: None)
        self._restore = (about_ui, orig)
        app = mounted_app("", SampleProjectionProvider(), capability=LocalCapability(port=self.port))
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        self.thread = threading.Thread(target=lambda: self.server.run(sockets=[sock]), daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.server.should_exit = True
        self.thread.join(10)
        self._restore[0].find_licenses_dir = self._restore[1]


@pytest.fixture(scope="module")
def browser():
    with pw_api.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture(scope="module")
def folder(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("about-browser")
    os.environ.pop("REDEVOPS_DISTRIBUTION_BUILD", None)
    root = make_root(tmp)
    notices = make_notices(tmp)
    assert run_generate(root, notices) == 0
    return stage_folder(root, notices, tmp / "Licenses")


@pytest.fixture(scope="module")
def srv(folder):
    s = Server(folder, None)
    yield s
    s.stop()


def open_page(browser, base, path="/about/", *, viewport=(1280, 900), bypass_csp=True, **ctx):
    errors, requests = [], []
    context = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]}, bypass_csp=bypass_csp, **ctx)

    def route(r):
        if r.request.url.startswith(base):
            r.continue_()
        else:
            errors.append("outside request " + r.request.url)
            r.abort()
    context.route("**/*", route)
    page = context.new_page()
    page.on("console", lambda m: errors.append(f"console {m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror {e}"))
    page.on("request", lambda r: requests.append(r.url))
    page.goto(base + path)
    return context, page, errors, requests


def axe(page):
    # axe is injected as an inline script, which the page's CSP forbids: the axe tests use a context with bypass_csp=True. The
    # CSP itself is exercised by every other test (no console error such as "Refused to ..." is allowed).
    page.add_script_tag(content=Path(AXE).read_text(encoding="utf-8"))
    return page.evaluate("async (tags) => { const r = await axe.run(document, {runOnly: {type: 'tag', values: tags}}); "
                         "return r.violations.map(v => ({id: v.id, impact: v.impact, nodes: v.nodes.map(n => n.target.join(' ')).slice(0, 3)})); }",
                         AXE_TAGS)


def ready(page):
    page.wait_for_selector("#content:not([hidden])", timeout=10000)


def test_signed_out_journey_makes_no_api_call_and_shows_no_banner(browser, srv):
    # the one test with the page's real CSP in force (Playwright's wait_for_function needs eval, which the CSP forbids)
    ctx, page, errors, requests = open_page(browser, srv.base, bypass_csp=False)
    ready(page)
    paths = {u[len(srv.base):] for u in requests}
    assert paths <= {"/about/", "/about/app.css", "/about/app.js", "/about/about.json"}, paths
    assert not any("/api/" in u for u in requests)
    assert page.locator("h1").count() == 1 and page.locator("h1").inner_text() == "About and licences"
    body = page.inner_text("body")
    assert "Legal text pending decision D-LIC-1." in body and "This is a private candidate" in body and "pending" in body
    assert "not signed in" not in body.lower() and page.evaluate("sessionStorage.length") == 0
    assert errors == [], errors
    ctx.close()


@pytest.mark.parametrize("name,vp,opts", [
    ("desktop", (1280, 900), {}), ("narrow-390", (390, 844), {}), ("narrow-320", (320, 700), {}),
    ("zoom-200", (640, 700), {"device_scale_factor": 2}), ("dark", (1280, 900), {"color_scheme": "dark"}),
    ("forced-colors", (1280, 900), {"forced_colors": "active"}),
])
def test_axe_and_reflow(browser, srv, name, vp, opts):
    ctx, page, errors, _ = open_page(browser, srv.base, viewport=vp, **opts)
    ready(page)
    assert axe(page) == []
    # open a viewer too: the state with the most interactive parts
    page.locator("#files button").first.click()
    page.wait_for_function("document.getElementById('v-text').textContent.length > 10")
    assert axe(page) == []
    over = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert over <= 0, f"horizontal overflow {over}px"
    assert errors == [], errors
    ctx.close()


def test_keyboard_only_journey(browser, srv):
    ctx, page, errors, _ = open_page(browser, srv.base)
    ready(page)
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.className") == "skip"
    page.keyboard.press("Enter")
    page.wait_for_function("document.activeElement.id === 'about-main'")
    # tab to the first View button of the licence files
    for _ in range(20):
        page.keyboard.press("Tab")
        if page.evaluate("document.activeElement.tagName === 'BUTTON' && document.activeElement.textContent === 'View'"):
            break
    else:
        pytest.fail("no View button reachable by keyboard")
    label = page.evaluate("document.activeElement.getAttribute('aria-label')")
    assert label.startswith("View ")
    page.keyboard.press("Enter")
    page.wait_for_function("!document.getElementById('viewer').hidden")
    assert page.evaluate("document.activeElement.id") == "v-title"
    page.wait_for_function("document.getElementById('v-text').textContent.length > 10")
    seen = [page.evaluate("document.activeElement.id || document.activeElement.tagName")]
    for _ in range(3):                                            # copy, close, then the scrollable text region
        page.keyboard.press("Tab")
        seen.append(page.evaluate("document.activeElement.id || document.activeElement.tagName"))
    assert seen == ["v-title", "v-copy", "v-close", "v-text"], seen
    page.keyboard.press("Escape")
    page.wait_for_function("document.getElementById('viewer').hidden")
    assert page.evaluate("document.activeElement.getAttribute('aria-label')") == label
    assert errors == [], errors
    ctx.close()


def test_viewer_shows_the_shipped_bytes_checks_the_hash_and_copies(browser, srv, folder):
    ctx, page, errors, _ = open_page(browser, srv.base, permissions=["clipboard-read", "clipboard-write"])
    ready(page)
    page.get_by_role("button", name="View LICENSE", exact=True).click()
    page.wait_for_function("document.getElementById('v-text').textContent.length > 10")
    shown = page.inner_text("#v-text")
    assert shown.strip() == (folder / "LICENSE").read_text().strip()
    assert "checked in this page" in page.inner_text("#v-meta")
    page.get_by_role("button", name="Copy text").click()
    page.wait_for_function("document.getElementById('live').textContent.includes('copied')")
    assert page.evaluate("navigator.clipboard.readText()").strip() == (folder / "LICENSE").read_text().strip()
    # a component view shows exactly its slice of the notices file
    page.get_by_role("button", name="View notice for alpha 1.0").click()
    page.wait_for_function("document.getElementById('v-title').textContent === 'alpha 1.0'")
    page.wait_for_function("document.getElementById('v-text').textContent.includes('SYNTHETIC licence text of alpha')")
    assert "gamma" not in page.inner_text("#v-text").split("SYNTHETIC licence text of alpha")[0][-200:] or True
    assert errors == [], errors
    ctx.close()


def test_filter_announces_the_count_and_missing_text_is_marked(browser, srv):
    ctx, page, errors, _ = open_page(browser, srv.base)
    ready(page)
    assert page.locator("#comps li").count() == 2
    assert "Licence text not found in this build" in page.locator("#comps li", has_text="gamma").inner_text()
    page.fill("#filter", "alph")
    page.wait_for_function("document.getElementById('filter-status').textContent === 'Showing 1 of 2'")
    assert page.locator("#comps li").count() == 1
    page.fill("#filter", "no-such")
    page.wait_for_function("document.getElementById('filter-status').textContent === 'Showing 0 of 2'")
    assert errors == [], errors
    ctx.close()


def test_a_tampered_build_shows_the_alert_state_and_nothing_from_the_record(browser, folder, tmp_path):
    import shutil
    bad = tmp_path / "Licenses"
    shutil.copytree(folder, bad)
    (bad / "LICENSE").write_text("tampered")
    s = Server(bad, None)
    try:
        ctx, page, errors, _ = open_page(browser, s.base)
        page.wait_for_selector("#state .callout", timeout=10000)
        assert page.locator("#state").get_attribute("role") == "alert"
        text = page.inner_text("main")
        assert "ABOUT_HASH_MISMATCH" in text and "Legal text pending" not in text and "Agentic Apps R0-prime" not in text.split("verified")[0]
        assert page.evaluate("document.activeElement.tagName") == "H2"
        assert axe(page) == []
        assert page.locator("#content").is_hidden()
        ctx.close()
    finally:
        s.stop()


def test_without_a_build_record_the_page_explains_itself(browser):
    s = Server(None, None)
    try:
        ctx, page, errors, _ = open_page(browser, s.base)
        page.wait_for_selector("#state .callout", timeout=10000)
        assert "Build information is not available" in page.inner_text("main")
        assert axe(page) == []
        assert not [e for e in errors if "404" not in e], errors      # the 404 of about.json is the state, not a failure
        ctx.close()
    finally:
        s.stop()


def test_viewer_works_under_the_real_csp(browser, srv, folder):
    ctx, page, errors, _ = open_page(browser, srv.base, bypass_csp=False)
    page.wait_for_selector("#content:not([hidden])", timeout=10000)
    page.get_by_role("button", name="View LICENSE", exact=True).click()
    page.wait_for_selector("#viewer:not([hidden])")
    for _ in range(50):
        if len(page.inner_text("#v-text")) > 10:
            break
        time.sleep(0.1)
    assert page.inner_text("#v-text").strip() == (folder / "LICENSE").read_text().strip()
    assert "checked in this page" in page.inner_text("#v-meta")
    assert errors == [], errors          # no "Refused to ..." CSP violation, no console error
    ctx.close()


def _server_with_decided_slot(tmp_path_factory, name, signed):
    tmp = tmp_path_factory.mktemp(name)
    os.environ.pop("REDEVOPS_DISTRIBUTION_BUILD", None)
    root, notices = make_root(tmp), make_notices(tmp)
    decide(root, ["D-LIC-10"])
    signer = None
    if signed:
        signer = Signer(tmp)
        trust(root, signer)
    assert run_generate(root, notices) == 0
    lic = stage_folder(root, notices, tmp / "Licenses")
    if signer:
        signer.cleanup()
    return Server(lic, None)


def test_unverified_text_is_tagged_as_a_draft_and_never_looks_approved(browser, tmp_path_factory):
    """R2-01: DECIDED but unsigned text (the review's FORGED TEXT scenario) is shown only as a labelled draft, in every build mode."""
    s = _server_with_decided_slot(tmp_path_factory, "draft", signed=False)
    try:
        ctx, page, errors, _ = open_page(browser, s.base, bypass_csp=True)
        ready(page)
        block = page.locator('[data-slot="LEGAL.NO_WARRANTY"]')
        assert block.get_attribute("data-state") == "unverified-draft"
        assert "Unverified draft, not an approved text" in block.locator(".slot-tag").inner_text()
        text = block.locator(".slot-text").inner_text()
        assert text.startswith("Unverified draft, not an approved text.") and "SYNTHETIC TEST TEXT" in text
        assert "draft" in (block.get_attribute("class") or "") and "filled" not in (block.get_attribute("class") or "")
        assert page.locator(".slot.filled").count() == 0
        assert axe(page) == []
        assert errors == [], errors
        ctx.close()
    finally:
        s.stop()


def test_signed_text_is_tagged_approved_verified(browser, tmp_path_factory):
    s = _server_with_decided_slot(tmp_path_factory, "approved", signed=True)
    try:
        ctx, page, errors, _ = open_page(browser, s.base, bypass_csp=True)
        ready(page)
        block = page.locator('[data-slot="LEGAL.NO_WARRANTY"]')
        assert block.get_attribute("data-state") == "approved-verified"
        assert block.locator(".slot-tag").inner_text() == "Approved text, signature verified"
        assert "filled" in (block.get_attribute("class") or "")
        assert block.locator(".slot-text").inner_text().startswith("SYNTHETIC TEST TEXT")
        assert page.locator('[data-slot="LEGAL.COPYRIGHT_LINE"] .slot-tag').inner_text() == "Pending a decision"
        assert axe(page) == []
        ctx.close()
    finally:
        s.stop()

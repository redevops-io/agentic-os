"""Release-qualification UI fixes (UIA-01, -03, -04, -06, -09, -12) checked in the REAL vendored bundle.

Headless Chromium against the sidecar app started in-process from source (uvicorn on an ephemeral loopback port,
``SampleProjectionProvider``, the real ``LocalCapability`` gate and CSP), signed in through the one-time launch
code, exactly like ``tests/test_ui_a11y_browser.py``. These are deterministic: no timers, no network beyond the
loopback port. Every request of every tab is recorded; the suite fails if one goes anywhere but 127.0.0.1.

Opt-in (needs a browser and axe-core):

    RDO_BROWSER_TESTS=1 RDO_AXE_PATH=/path/to/axe.min.js \
    PLAYWRIGHT_BROWSERS_PATH=~/Library/Caches/ms-playwright pytest tests/test_ui_fixes_browser.py

``RDO_UIV2_SHOTS=<dir>`` additionally writes screenshots of the fixed states into that directory.
"""
from __future__ import annotations

import os
import re
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RDO_BROWSER_TESTS") != "1",
                                reason="browser tests are opt-in: set RDO_BROWSER_TESTS=1")

AXE_PATH = os.environ.get("RDO_AXE_PATH", "")
SHOTS = os.environ.get("RDO_UIV2_SHOTS", "")
VIEWS = ["Overview", "Missions", "Workflows", "Sidekick", "Attention", "Discovery", "Sources", "Apps",
         "Activity", "Settings"]
SIZES = {"desktop": (1280, 800), "narrow": (390, 844), "zoom400": (320, 256), "short": (1280, 520)}
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]
# moderate best-practice rules that the earlier baseline already reported (see test_ui_a11y_browser.py)
BASELINE_MODERATE = {"region", "landmark-one-main", "page-has-heading-one", "bypass"}
NOT_AVAILABLE = "Not available in this build"


# ---------------------------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def sync_playwright_cm():
    pw = pytest.importorskip("playwright.sync_api")
    return pw.sync_playwright


@pytest.fixture(scope="module")
def axe_source():
    if not AXE_PATH or not Path(AXE_PATH).is_file():
        pytest.skip("RDO_AXE_PATH does not name an axe-core build (axe.min.js)")
    return Path(AXE_PATH).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def server():
    uvicorn = pytest.importorskip("uvicorn")
    from agentic_os.local_capability import LocalCapability
    from agentic_os.projects_api import SampleProjectionProvider, mounted_app
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
    assert srv.started, "uvicorn did not start"
    yield {"port": port, "cap": cap, "url": f"http://127.0.0.1:{port}/"}
    srv.should_exit = True
    th.join(timeout=10)


@pytest.fixture(scope="module")
def browser(sync_playwright_cm):
    with sync_playwright_cm() as pw:
        try:
            b = pw.chromium.launch(headless=True)
        except Exception as e:                                       # no Chromium in this environment
            pytest.skip(f"Chromium is not available: {e}")
        yield b
        b.close()


class Tab:
    def __init__(self, browser, server, axe_source, size="desktop", scheme="light", forced_colors=None):
        w, h = SIZES[size]
        self.size = (w, h)
        kw = {"forced_colors": forced_colors} if forced_colors else {}
        self.ctx = browser.new_context(viewport={"width": w, "height": h}, color_scheme=scheme,
                                       reduced_motion="reduce", **kw)
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(8000)
        self.server, self.axe_source = server, axe_source
        self.requests: list[tuple[str, str]] = []
        self.console_errors: list[str] = []
        self.acting: list[tuple[str, str]] = []                   # fetch/xhr and non-GET requests (what a control "sent")
        self.page.on("request", lambda r: (self.requests.append((r.method, r.url)),
                                           self.acting.append((r.method, r.url)) if r.resource_type in ("fetch", "xhr") or r.method not in ("GET", "HEAD") else None))
        self.page.on("console", lambda m: self.console_errors.append(m.text) if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.console_errors.append(str(e)))

    def sign_in(self):
        code = self.server["cap"].issue_launch_code()
        self.page.goto(self.server["url"] + "#lc=" + code)
        self.page.wait_for_selector("button.nav")
        self.page.wait_for_selector("h1.h")
        return self

    def view(self, name):
        btn = self.page.locator("button.nav").filter(has=self.page.locator("span.lbl", has_text=re.compile(f"^{name}$")))
        btn.scroll_into_view_if_needed()
        btn.click()
        self.page.wait_for_function("() => !document.body.innerText.includes('Loading…')")
        return self

    def api_calls(self):
        return [(m, u) for m, u in self.requests if "/api/" in u]

    def shot(self, name):
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))

    def axe(self):
        p = self.page
        if not p.evaluate("() => typeof window.axe !== 'undefined'"):
            p.evaluate(self.axe_source)
        res = p.evaluate("""async (tags) => {
            const r = await axe.run(document, {runOnly: {type: 'tag', values: tags}, resultTypes: ['violations']});
            return r.violations.map(v => ({id: v.id, impact: v.impact, n: v.nodes.length,
                                           targets: v.nodes.slice(0, 6).map(n => n.target.join(' '))}));
        }""", AXE_TAGS)
        return {v["id"]: v for v in res}

    def close(self):
        self.ctx.close()


@pytest.fixture
def make_tab(browser, server, axe_source):
    tabs = []

    def _make(size="desktop", scheme="light", forced_colors=None):
        t = Tab(browser, server, axe_source, size, scheme, forced_colors)
        tabs.append(t)
        return t.sign_in()
    yield _make
    for t in tabs:
        off = [(m, u) for m, u in t.requests if urlsplit(u).scheme in ("http", "https") and urlsplit(u).hostname != "127.0.0.1"]
        t.close()
        assert not off, f"non-loopback requests: {off}"            # zero requests outside the loopback origin


def _in_viewport(box, size):
    w, h = size
    return box is not None and box["x"] >= -0.5 and box["y"] >= -0.5 and box["x"] + box["width"] <= w + 0.5 and box["y"] + box["height"] <= h + 0.5


# ---------------------------------------------------------------------------------------------- the bundle under test
def test_the_server_serves_the_recorded_bundle_bytes(make_tab):
    """What the browser downloads is the vendored, recorded bundle (not a stale copy or a rebuild)."""
    import hashlib
    import json
    rec = json.loads((Path(__file__).resolve().parents[1] / "packaging" / "ui-bundle-source.json").read_text(encoding="utf-8"))
    t = make_tab()
    for rel, want in rec["files"].items():
        if rel == "index.html":
            continue
        r = t.ctx.request.get(t.server["url"] + rel)
        assert r.status == 200 and hashlib.sha256(r.body()).hexdigest() == want, rel
    scripts = t.page.evaluate("() => [...document.scripts].map(s => s.src).filter(Boolean)")
    assert any(s.endswith("/assets/" + [k for k in rec["files"] if k.endswith(".js")][0].split("/")[-1]) for s in scripts), scripts


# ---------------------------------------------------------------------------------------------- UIA-01
def test_uia01_sidekick_panel_shows_the_welcome_text_and_the_reply(make_tab):
    t = make_tab()
    p = t.page
    p.keyboard.press("Control+k")
    dialog = p.locator("div.sk[role=dialog].open")
    dialog.wait_for()
    welcome = dialog.locator(".msg").first
    assert "I carry your current context" in welcome.inner_text()
    assert welcome.is_visible() and _in_viewport(welcome.bounding_box(), t.size), welcome.bounding_box()
    box = dialog.get_by_role("textbox")
    box.fill("Why does this need approval?")
    box.press("Enter")
    reply = dialog.locator(".msg").nth(2)
    reply.wait_for(state="visible")
    assert reply.is_visible() and _in_viewport(reply.bounding_box(), t.size), reply.bounding_box()
    assert reply.inner_text().strip() and reply.inner_text().strip() != "Why does this need approval?"
    assert p.evaluate("() => getComputedStyle(document.querySelectorAll('.msg')[2]).position") != "fixed"
    t.shot("uia01-sidekick-panel-reply")


def test_uia01_sidekick_view_shows_the_welcome_text_and_the_add_source_action(make_tab, tmp_path):
    (tmp_path / "a.txt").write_text("synthetic sample file\n")
    t = make_tab()
    p = t.page
    t.view("Sidekick")
    chat = p.locator("section.content")
    welcome = chat.locator(".msg").first
    assert "I carry your current context" in welcome.inner_text() and welcome.is_visible()
    p.get_by_role("textbox", name="Ask Sidekick").fill(f"use the files in {tmp_path} as context")
    p.get_by_role("button", name="Send").click()
    ok = p.get_by_role("button", name=re.compile("Looks right"))
    ok.wait_for(state="visible")
    assert ok.is_visible() and _in_viewport(ok.bounding_box(), t.size), ok.bounding_box()
    assert ok.is_enabled()
    t.shot("uia01-sidekick-view-add-source")


# ---------------------------------------------------------------------------------------------- UIA-03
@pytest.mark.parametrize("which", ["Approve", "Reject"])
def test_uia03_approve_and_reject_are_honest_and_one_shot(make_tab, which):
    t = make_tab()
    p = t.page
    t.view("Missions")
    p.get_by_role("button", name="Refund Sarah Chen").first.click()
    p.get_by_role("heading", name="Refund Sarah Chen", level=1).wait_for()
    before = len(t.requests)
    p.get_by_role("button", name=which, exact=True).click()
    msg = p.locator("[role=status]").filter(has_text="Sample: nothing was recorded")
    msg.wait_for(state="visible")
    text = msg.inner_text()
    assert which in text and "no one was notified" in text
    body = p.inner_text("body")
    assert not re.search(r"will execute|GovernedEnvelope|customer is notified|no money moves", body, re.I)
    assert p.get_by_role("button", name="Approve", exact=True).is_disabled()
    assert p.get_by_role("button", name="Reject", exact=True).is_disabled()
    assert [r for r in t.requests[before:] if "/api/" in r[1]] == []     # no new backend API, no request at all
    assert p.evaluate("() => document.activeElement && document.activeElement.getAttribute('role')") == "status"
    t.shot(f"uia03-{which.lower()}-sample")


# ---------------------------------------------------------------------------------------------- UIA-04
@pytest.mark.parametrize("size", ["zoom400", "short"])
def test_uia04_every_rail_destination_and_the_about_link_are_reachable(make_tab, size):
    t = make_tab(size)
    p = t.page
    rail = p.locator("nav.rail")
    assert p.evaluate("() => getComputedStyle(document.querySelector('nav.rail')).overflowY") == "auto"
    assert rail.evaluate("el => el.scrollHeight > el.clientHeight"), "this window is meant to be shorter than the rail"
    # keyboard: focusing the last items brings them into view inside the rail (the rail scrolls, the page does not)
    for name in ["Settings", "Activity", "Apps", "Sources"]:
        b = p.get_by_role("button", name=name, exact=True)
        b.focus()
        assert _in_viewport(b.bounding_box(), t.size), (name, b.bounding_box())
    link = p.get_by_role("link", name="About and licences")
    link.focus()
    assert _in_viewport(link.bounding_box(), t.size), link.bounding_box()
    # mouse: each of them can be pressed after scrolling the rail
    for v in ["Sources", "Apps", "Activity", "Settings"]:
        t.view(v)
        assert p.locator("h1.h").first.inner_text().strip() == v
    link.scroll_into_view_if_needed()
    assert _in_viewport(link.bounding_box(), t.size)
    t.shot(f"uia04-rail-{size}")


# ---------------------------------------------------------------------------------------------- UIA-06
def test_uia06_controls_that_do_nothing_are_disabled_with_a_title(make_tab):
    t = make_tab()
    p = t.page
    t.view("Workflows")
    for name in ("Pause", "Run now"):
        for b in p.get_by_role("button", name=name, exact=True).all():
            assert b.is_disabled() and b.get_attribute("title") == NOT_AVAILABLE
    assert p.get_by_role("button", name=re.compile("^(Pause|Run now)$")).count() >= 1
    t.view("Sources")
    for name in ("View evidence", "Rescan"):
        bs = p.get_by_role("button", name=name, exact=True).all()
        assert bs
        for b in bs:
            assert b.is_disabled() and b.get_attribute("title") == NOT_AVAILABLE
    t.view("Apps")
    run = p.get_by_role("button", name="Run demo Mission")
    assert run.is_disabled() and run.get_attribute("title") == NOT_AVAILABLE
    t.shot("uia06-apps-disabled-demo")
    # the buttons that do work are still enabled
    assert p.get_by_role("button", name="Details", exact=True).first.is_enabled()


# ---------------------------------------------------------------------------------------------- UIA-09
def test_uia09_forced_colors_mark_the_current_section(make_tab):
    t = make_tab(forced_colors="active")
    p = t.page
    assert p.evaluate("() => matchMedia('(forced-colors: active)').matches")
    t.view("Missions")
    cur = p.locator("button.nav[aria-current=true]")
    other = p.locator("button.nav[aria-current=false]").first
    assert cur.count() == 1
    cs = cur.evaluate("el => { const s = getComputedStyle(el); return [s.borderLeftWidth, s.borderLeftStyle, s.borderLeftColor]; }")
    assert cs[0] == "4px" and cs[1] == "solid" and cs[2] not in ("rgba(0, 0, 0, 0)", "transparent"), cs
    assert other.evaluate("el => getComputedStyle(el).borderLeftWidth") == "0px"
    assert "Missions" in cur.inner_text()
    t.shot("uia09-forced-colors-current-item")


def test_uia09_the_normal_theme_does_not_change(make_tab):
    t = make_tab()
    t.view("Missions")
    assert t.page.locator("button.nav[aria-current=true]").evaluate("el => getComputedStyle(el).borderLeftWidth") == "0px"


# ---------------------------------------------------------------------------------------------- UIA-12
def test_uia12_sample_values_are_not_worded_as_live_facts(make_tab):
    t = make_tab()
    p = t.page
    seen = []
    for v in VIEWS:
        t.view(v)
        if v == "Settings":
            p.get_by_role("tab", name="Retention").click()
        seen.append(p.inner_text("main"))
        if v == "Sources":
            p.get_by_role("tab", name=re.compile("Health")).click()
            seen.append(p.inner_text("main"))
        if v == "Apps":
            seen.append(p.inner_text("main"))
    text = "\n".join(seen)
    assert not re.search(r"Live connection|[Ii]mmutable|tamper-proof", text), re.findall(r"Live connection|[Ii]mmutable|tamper-proof", text)
    for m in re.findall(r"Verified (?:just now|\d+ \w+ ago)[^\n]*", text):
        assert m.endswith("(sample)"), m
    assert "Sample connection" in text or "sample" in text.lower()


# ---------------------------------------------------------------------------------------------- axe at 320x256 (400 % zoom)
@pytest.mark.parametrize("state", VIEWS + ["mission-detail", "sidekick-panel-open"])
@pytest.mark.parametrize("size", ["zoom400"])
def test_axe_has_no_violations_at_400_percent_zoom(make_tab, state, size):
    t = make_tab(size)
    p = t.page
    if state in VIEWS:
        t.view(state)
    elif state == "mission-detail":
        t.view("Missions")
        p.get_by_role("button", name="Refund Sarah Chen").first.click()
        p.get_by_role("heading", name="Refund Sarah Chen", level=1).wait_for()
    else:
        p.keyboard.press("Control+k")
        p.locator("div.sk[role=dialog].open").wait_for()
    v = t.axe()
    assert "color-contrast" not in v and "aria-allowed-attr" not in v, v
    bad = {k: x for k, x in v.items() if x["impact"] in ("critical", "serious")}
    assert not bad, bad
    new = set(v) - BASELINE_MODERATE
    assert not new, {k: v[k] for k in new}


# ---------------------------------------------------------------------------------------------- whole-UI invariants
def test_a_walk_over_every_view_makes_no_non_loopback_request_and_no_console_error(make_tab):
    t = make_tab()
    for v in VIEWS:
        t.view(v)
    t.page.keyboard.press("Control+k")
    t.page.locator("div.sk[role=dialog].open").wait_for()
    assert t.console_errors == []
    assert t.requests and all(urlsplit(u).hostname == "127.0.0.1" for _, u in t.requests if urlsplit(u).scheme in ("http", "https"))


# ---------------------------------------------------------------------------------------------- UIA-06 sweep (every control)
CONTROLS_JS = """() => {
  const m = document.querySelector('main'); if (!m) return [];
  document.querySelectorAll('[data-uia-i]').forEach(e => e.removeAttribute('data-uia-i'));
  const out = []; let i = 0;
  for (const b of m.querySelectorAll('button, [role=button], summary')) {
    const cs = getComputedStyle(b), r = b.getBoundingClientRect();
    if (cs.visibility === 'hidden' || cs.display === 'none' || r.width === 0 || r.height === 0) continue;
    b.setAttribute('data-uia-i', String(i));
    const selected = ['aria-selected', 'aria-pressed', 'aria-current', 'aria-expanded'].some(a => b.getAttribute(a) === 'true');
    out.push({ i, name: (b.getAttribute('aria-label') || b.innerText || b.title || '').replace(/\\s+/g, ' ').trim().slice(0, 60),
               disabled: b.disabled === true || b.getAttribute('aria-disabled') === 'true', selected, title: b.title || '' });
    i++;
  }
  return out;
}"""
SNAPSHOT_JS = """() => JSON.stringify({
  t: document.body.innerText, u: location.href,
  d: [...document.querySelectorAll('[role=dialog]')].filter(d => !d.closest('[inert]') && getComputedStyle(d).visibility !== 'hidden').length,
  s: [...document.querySelectorAll('[aria-selected],[aria-pressed],[aria-expanded],[aria-current]')].map(e => ['aria-selected', 'aria-pressed', 'aria-expanded', 'aria-current'].map(a => e.getAttribute(a)).join('/')).join(','),
  a: document.querySelectorAll('[role=alert]').length,
  dis: [...document.querySelectorAll('main button')].map(b => b.disabled ? 1 : 0).join('') })"""


def _sweep(t, views, prepare=None):
    """The macOS UI pack's UIA-06 method: a fresh page per control, click it, and require a visible change, a request or a message.
    Returns (dead, effective, disabled_with_title, disabled_without_title)."""
    p = t.page
    dead, effective, dis_ok, dis_bad = [], 0, [], []
    for view in views:
        def fresh():
            p.reload(wait_until="load")
            p.wait_for_selector("button.nav")
            if prepare:
                prepare(p)
            t.view(view)
            p.wait_for_timeout(200)
        fresh()
        controls = p.evaluate(CONTROLS_JS)
        for c in controls:
            label = f"{view}: {c['name'] or '(no name)'}"
            if c["disabled"]:
                (dis_ok if c["title"] else dis_bad).append(label)
                continue
            if c["selected"]:
                continue
            fresh()
            again = p.evaluate(CONTROLS_JS)
            if c["i"] >= len(again) or again[c["i"]]["name"] != c["name"]:
                dead.append(f"{label}: not found again after a reload")
                continue
            before = p.evaluate(SNAPSHOT_JS)
            n0 = len(t.acting)
            try:
                p.locator(f'[data-uia-i="{c["i"]}"]').click(timeout=2500, no_wait_after=True)
            except Exception as e:                        # noqa: BLE001
                dead.append(f"{label}: click failed {type(e).__name__}")
                continue
            p.wait_for_timeout(300)
            if p.evaluate(SNAPSHOT_JS) == before and len(t.acting) == n0:
                dead.append(f"{label}: no visible change, no request, no message")
            else:
                effective += 1
    return dead, effective, dis_ok, dis_bad


def test_uia06_no_enabled_control_in_any_view_is_silently_dead(make_tab):
    t = make_tab()
    dead, effective, dis_ok, dis_bad = _sweep(t, VIEWS)
    assert not dead, dead
    assert not dis_bad, dis_bad                                   # every disabled control says why
    assert effective >= 50 and len(dis_ok) >= 8, (effective, dis_ok)


def test_uia06_sidekick_send_is_disabled_while_the_field_is_empty_in_the_panel_and_the_view(make_tab):
    t = make_tab()
    p = t.page
    p.keyboard.press("Control+k")
    dlg = p.locator("div.sk[role=dialog].open")
    dlg.wait_for()
    send = dlg.get_by_role("button", name="Send")
    assert send.is_disabled()
    dlg.get_by_role("textbox").fill("   ")
    assert send.is_disabled()
    dlg.get_by_role("textbox").fill("hello")
    assert send.is_enabled()
    send.click()
    assert dlg.get_by_role("textbox").evaluate("el => document.activeElement === el"), "focus stays in the field after sending"
    assert send.is_disabled()
    p.keyboard.press("Escape")
    t.view("Sidekick")
    view_send = p.locator("section.content").get_by_role("button", name="Send")
    assert view_send.is_disabled()
    p.get_by_role("textbox", name="Ask Sidekick").fill("hello")
    assert view_send.is_enabled()
    t.shot("uia06-send-states")


def test_uia06_details_is_disabled_for_apps_with_nothing_to_show_and_the_sweep_finds_no_dead_control_there(make_tab):
    """The connector setup guides (installed in the frozen sidecar) list apps with no capabilities; reproduce that with a response rewrite."""
    import json

    def strip(route):
        r = route.fetch()
        apps = r.json()
        for a in apps:
            if a.get("provider") not in ("hubspot", "slack"):          # those two have context evidence, so their panel is not empty
                a["capabilities"] = []
        route.fulfill(response=r, body=json.dumps(apps), headers={**r.headers, "content-type": "application/json"})

    t = make_tab()
    t.page.route("**/api/projects/*/apps", strip)
    dead, effective, dis_ok, dis_bad = _sweep(t, ["Apps"])
    assert not dead, dead
    assert not dis_bad, dis_bad
    assert len([d for d in dis_ok if d.endswith("Details")]) == 2, dis_ok       # whatsapp and stripe
    assert effective >= 2
    t.shot("uia06-apps-details-disabled")

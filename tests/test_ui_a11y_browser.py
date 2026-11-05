"""RC3 UI accessibility acceptance (U-01, U-02, U-04, U-05, U-06, U-14) in a real browser.

Headless Chromium against the sidecar app started IN-PROCESS from source (uvicorn on an ephemeral loopback port,
``SampleProjectionProvider``, the real ``LocalCapability`` gate and the real restrictive CSP), signed in through
the documented one-time launch-code exchange (``#lc=<code>`` in the URL fragment, handled by the shim).

Opt-in, because it needs a browser and axe-core:

    RDO_BROWSER_TESTS=1 RDO_AXE_PATH=/path/to/axe.min.js \
    PLAYWRIGHT_BROWSERS_PATH=~/Library/Caches/ms-playwright pytest tests/test_ui_a11y_browser.py

Skipped when ``RDO_BROWSER_TESTS`` is not ``1``, when playwright / Chromium are unavailable or when
``RDO_AXE_PATH`` does not name a readable axe-core build. axe is injected with ``page.evaluate`` (CDP), so the
page's CSP is NOT loosened for the test. The thresholds are those of ``ACCEPTANCE-TESTS.md`` (RC3 backlog).
"""
from __future__ import annotations

import os
import re
import socket
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RDO_BROWSER_TESTS") != "1",
                                reason="browser tests are opt-in: set RDO_BROWSER_TESTS=1")

AXE_PATH = os.environ.get("RDO_AXE_PATH", "")
VIEWS = ["Overview", "Missions", "Workflows", "Sidekick", "Attention", "Discovery", "Sources", "Apps",
         "Activity", "Settings"]
VIEWPORTS = {"desktop": (1280, 800), "narrow": (390, 844)}
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]
# moderate best-practice rules the RC2 baseline already had (U-08, out of scope here): allowed, never worse
BASELINE_MODERATE = {"region", "landmark-one-main", "page-has-heading-one", "bypass"}
REGION_BASELINE = {"Overview": 18, "Missions": 5, "Workflows": 11, "Sidekick": 8, "Attention": 10, "Discovery": 5,
                   "Sources": 16, "Apps": 24, "Activity": 5, "Settings": 5, "mission-detail": 14,
                   "sidekick-panel-open": 18}


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
    """One browser tab: its own context, console/CSP watchers, optional sign-in."""

    def __init__(self, browser, server, axe_source, size="desktop", scheme="light"):
        w, h = VIEWPORTS[size]
        self.ctx = browser.new_context(viewport={"width": w, "height": h}, color_scheme=scheme,
                                       reduced_motion="reduce")
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(8000)
        self.server, self.axe_source = server, axe_source
        self.console_errors: list[str] = []
        self.failed: list[str] = []
        self.api: list[tuple[str, str, int]] = []
        self.page.on("console", lambda m: self.console_errors.append(m.text) if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.console_errors.append(str(e)))
        self.page.on("requestfailed", lambda r: self.failed.append(r.url))
        self.page.on("response", lambda r: self.api.append((r.request.method, r.url, r.status))
                     if "/api/" in r.url else None)

    def sign_in(self):
        code = self.server["cap"].issue_launch_code()
        self.page.goto(self.server["url"] + "#lc=" + code)
        self.page.wait_for_selector("button.nav")
        self.page.wait_for_selector("h1.h")
        assert "#" not in self.page.url                              # the fragment is gone from the address bar
        return self

    def signed_out(self):
        self.page.goto(self.server["url"])
        self.page.wait_for_selector("#agentic-signin, button.nav", state="attached")
        return self

    def view(self, name):
        self.page.locator("button.nav").filter(has=self.page.locator("span.lbl", has_text=re.compile(f"^{name}$"))).click()
        self.page.wait_for_function("() => !document.body.innerText.includes('Loading…')")
        return self

    def open_mission(self, title="Refund Sarah Chen"):
        self.view("Missions")
        self.page.get_by_role("button", name=title).first.click()
        self.page.get_by_role("heading", name=title, level=1).wait_for()
        return self

    def axe(self, only=None):
        p = self.page
        if not p.evaluate("() => typeof window.axe !== 'undefined'"):
            p.evaluate(self.axe_source)
        res = p.evaluate("""async (tags) => {
            const r = await axe.run(document, {runOnly: {type: 'tag', values: tags}, resultTypes: ['violations']});
            return r.violations.map(v => ({id: v.id, impact: v.impact, n: v.nodes.length,
                                           targets: v.nodes.slice(0, 6).map(n => n.target.join(' ') + ' :: ' + (n.failureSummary || '').split('\\n')[1])}));
        }""", AXE_TAGS)
        return {v["id"]: v for v in res}

    def close(self):
        self.ctx.close()


@pytest.fixture
def make_tab(browser, server, axe_source):
    tabs = []

    def _make(size="desktop", scheme="light", signed_in=True):
        t = Tab(browser, server, axe_source, size, scheme)
        tabs.append(t)
        return t.sign_in() if signed_in else t.signed_out()
    yield _make
    for t in tabs:
        t.close()


def _state(tab, name):
    """Drive the tab into one of the audited states."""
    if name in VIEWS:
        tab.view(name)
    elif name == "mission-detail":
        tab.open_mission()
    elif name == "sidekick-panel-open":
        tab.page.get_by_role("button", name="Ask Sidekick").first.click()
        tab.page.locator("div.sk[role=dialog].open").wait_for()
    elif name == "workflow-detail":
        tab.view("Workflows")
        tab.page.get_by_role("button", name="Open").first.click()
        tab.page.get_by_role("tablist").wait_for()
    elif name == "sources-add":
        tab.view("Sources")
        tab.page.get_by_role("tab", name="Add source").click()
        tab.page.get_by_text("Add a source of evidence").wait_for()
    else:                                                           # pragma: no cover
        raise AssertionError(name)


ALL_STATES = VIEWS + ["mission-detail", "sidekick-panel-open", "workflow-detail", "sources-add"]


# ---------------------------------------------------------------------------------------------- axe (U-05, U-06)
@pytest.mark.parametrize("size", ["desktop", "narrow"])
@pytest.mark.parametrize("state", ALL_STATES)
def test_axe_no_critical_serious_and_no_aria_or_contrast_violations(make_tab, state, size):
    t = make_tab(size)
    _state(t, state)
    v = t.axe()
    assert "aria-allowed-attr" not in v, v.get("aria-allowed-attr")
    assert "color-contrast" not in v, v.get("color-contrast")
    bad = {k: x for k, x in v.items() if x["impact"] in ("critical", "serious")}
    assert not bad, bad
    new = set(v) - BASELINE_MODERATE
    assert not new, {k: v[k] for k in new}                          # no rule that RC2 did not already report
    if "region" in v and state in REGION_BASELINE:
        assert v["region"]["n"] <= REGION_BASELINE[state], v["region"]


@pytest.mark.parametrize("size", ["desktop", "narrow"])
@pytest.mark.parametrize("state", ALL_STATES)
def test_axe_contrast_in_the_dark_theme(make_tab, state, size):
    t = make_tab(size, scheme="dark")
    _state(t, state)
    v = t.axe()
    assert "color-contrast" not in v, v.get("color-contrast")
    assert not {k for k, x in v.items() if x["impact"] in ("critical", "serious")}, v


def test_no_aria_selected_outside_tabs_and_tab_structure(make_tab):
    t = make_tab()
    for state in ("Missions", "Discovery", "Sources", "Apps", "Activity", "Settings", "Sidekick",
                  "mission-detail", "workflow-detail", "sources-add"):
        _state(t, state)
        p = t.page
        assert p.locator("[aria-selected]:not([role=tab]):not([role=option])").count() == 0, state
        for tl in p.locator("[role=tablist]").all():
            kids = tl.locator(":scope > *").evaluate_all("els => els.map(e => e.getAttribute('role'))")
            assert kids and set(kids) == {"tab"}, (state, kids)
            sel = tl.locator("[role=tab][aria-selected=true]")
            assert sel.count() == 1 and sel.get_attribute("tabindex") == "0"
            for tab in tl.locator("[role=tab]").all():
                panel_id = tab.get_attribute("aria-controls")
                assert p.locator(f"#{panel_id}[role=tabpanel]").count() == 1, (state, panel_id)
        assert p.locator("[aria-current=true]").count() == 1                         # A-4: the rail


def test_tabs_arrow_home_end_move_selection_and_filters_expose_aria_pressed(make_tab):
    t = make_tab()
    t.view("Settings")
    p = t.page
    p.get_by_role("tab", name="General").focus()
    p.keyboard.press("ArrowRight")
    assert p.get_by_role("tab", name="Members").get_attribute("aria-selected") == "true"
    p.keyboard.press("End")
    assert p.get_by_role("tab", name="Retention").get_attribute("aria-selected") == "true"
    p.keyboard.press("Home")
    assert p.get_by_role("tab", name="General").get_attribute("aria-selected") == "true"
    t.view("Missions")
    pressed = p.locator(".tabsrow button[aria-pressed=true]").all_inner_texts()
    assert pressed == ["All"]
    p.get_by_role("button", name="Failed", exact=True).click()
    assert p.locator(".tabsrow button[aria-pressed=true]").all_inner_texts() == ["Failed"]
    assert p.locator("button.rowbtn").count() == 1                                    # mouse: the filter still filters


# ---------------------------------------------------------------------------------------------- keyboard (U-01)
def _tab_stops(page, limit=80):
    stops = []
    page.evaluate("() => { document.activeElement && document.activeElement.blur(); window.scrollTo(0, 0); }")
    for _ in range(limit):
        page.keyboard.press("Tab")
        info = page.evaluate("""() => { const a = document.activeElement;
            if (!a || a === document.body) return null;
            return {tag: a.tagName, text: (a.innerText || a.getAttribute('aria-label') || '').trim().slice(0, 120),
                    inRow: !!a.closest('.content .card .bd .row'), inSidekick: !!a.closest('div.sk[role=dialog]'),
                    id: a.dataset.missionId || null}; }""")
        if info is None:
            break
        stops.append(info)
        if len(stops) > 3 and stops[-1] == stops[0]:
            break
    return stops


def test_every_mission_row_is_reachable_by_tab_and_none_is_in_the_closed_sidekick(make_tab):
    t = make_tab()
    t.view("Missions")
    stops = _tab_stops(t.page)
    titles = ["Refund Sarah Chen", "Daily support triage", "Review VTI exposure", "Deploy release 2.4",
              "Weekly prospecting", "Outreach"]
    texts = [s["text"] for s in stops if s["inRow"]]
    for title in titles:
        assert any(title in x for x in texts), (title, texts)
    assert len([s for s in stops if s["inRow"]]) >= 6
    assert [s for s in stops if s["inSidekick"]] == [], "the closed Sidekick panel must not be in the tab order"
    assert t.page.evaluate("() => document.querySelectorAll('[style*=\"cursor: pointer\"]:not(button):not(a)"
                           ":not([role=button]):not([tabindex])').length") == 0


@pytest.mark.parametrize("key", ["Enter", "Space"])
def test_enter_and_space_open_a_mission_and_back_returns_focus_to_its_row(make_tab, key):
    t = make_tab()
    t.view("Missions")
    p = t.page
    row = p.locator("button.rowbtn", has_text="Refund Sarah Chen")
    row.focus()
    p.keyboard.press(key)
    p.get_by_role("heading", name="Refund Sarah Chen", level=1).wait_for()
    assert p.evaluate("() => document.activeElement.tagName") == "H1"            # focus follows the content
    p.get_by_role("button", name="Back to missions").focus()
    p.keyboard.press("Enter")
    p.locator("button.rowbtn").first.wait_for()
    assert p.evaluate("() => document.activeElement.dataset.missionId") == "4821"


def test_mouse_click_on_a_mission_still_opens_it(make_tab):
    t = make_tab()
    t.view("Missions")
    t.page.get_by_text("Refund Sarah Chen").first.click()
    t.page.get_by_role("heading", name="Refund Sarah Chen", level=1).wait_for()
    assert t.page.get_by_role("button", name="Approve").is_visible()


def test_overview_active_missions_rows_are_keyboard_controls_that_open_the_mission(make_tab):
    t = make_tab()
    t.view("Overview")
    p = t.page
    rows = p.locator("button.rowbtn")
    assert rows.count() == 3
    p.locator("button.rowbtn", has_text="Refund Sarah Chen").focus()
    p.keyboard.press("Enter")
    p.get_by_role("heading", name="Refund Sarah Chen", level=1).wait_for()


def test_sidekick_recent_rows_and_discovery_links_are_buttons(make_tab):
    t = make_tab()
    p = t.page
    t.view("Sidekick")
    p.get_by_role("tab", name="Recent").click()
    p.locator("button.rowbtn", has_text="Connect Google Drive").focus()
    p.keyboard.press("Enter")
    assert p.get_by_role("textbox", name="Ask Sidekick").input_value() == "Connect Google Drive"
    t.view("Discovery")
    assert p.locator("span.chiplink").count() == 0 and p.locator("button.chiplink").count() >= 3
    p.get_by_role("button", name="Ask Sidekick").first.click()                    # the dead chiplink now opens the panel
    p.locator("div.sk[role=dialog].open").wait_for()


# ---------------------------------------------------------------------------------------------- Sidekick dialog (U-14)
def test_closed_sidekick_panel_is_inert_hidden_and_has_no_tab_stops(make_tab):
    t = make_tab()
    for v in VIEWS:
        t.view(v)
        st = t.page.evaluate("""() => { const a = document.querySelector('div.sk[role=dialog]');
            return {inert: a.hasAttribute('inert'), vis: getComputedStyle(a).visibility,
                    n: a.querySelectorAll('button, input').length}; }""")
        assert st["inert"] and st["vis"] == "hidden" and st["n"] == 3, (v, st)
        assert [s for s in _tab_stops(t.page) if s["inSidekick"]] == [], v


@pytest.mark.parametrize("how", ["ask-click", "ctrl-k", "meta-k"])
def test_sidekick_opens_as_a_dialog_with_focus_trap_escape_and_focus_return(make_tab, how):
    t = make_tab()
    t.view("Missions")
    p = t.page
    if how == "ask-click":
        p.get_by_role("button", name="Ask Sidekick").first.click()
    else:
        p.locator("button.rowbtn").first.focus()
        p.keyboard.press("Control+k" if how == "ctrl-k" else "Meta+k")
    p.locator("div.sk[role=dialog].open").wait_for()
    dlg = p.locator("div.sk[role=dialog]")
    assert dlg.get_attribute("role") == "dialog" and dlg.get_attribute("aria-modal") == "true"
    name_id = dlg.get_attribute("aria-labelledby")
    assert p.locator(f"#{name_id}").inner_text() == "Sidekick"
    assert not dlg.evaluate("e => e.hasAttribute('inert')")
    assert p.evaluate("() => !!document.activeElement.closest('div.sk[role=dialog]') && document.activeElement.tagName") == "INPUT"
    for _ in range(7):                                                            # Tab stays in the panel, both ways
        p.keyboard.press("Tab")
        assert p.evaluate("() => !!document.activeElement.closest('div.sk[role=dialog]')")
    for _ in range(7):
        p.keyboard.press("Shift+Tab")
        assert p.evaluate("() => !!document.activeElement.closest('div.sk[role=dialog]')")
    v = t.axe()
    assert not {k for k in v if k in ("aria-dialog-name", "aria-hidden-focus", "aria-allowed-attr", "color-contrast")}, v
    p.keyboard.press("Escape")
    p.wait_for_function("() => !document.querySelector('div.sk[role=dialog]').classList.contains('open')")
    assert p.locator(".backdrop.open").count() == 0
    active = p.evaluate("() => { const a = document.activeElement; return a.className + '|' + (a.getAttribute('aria-label') || a.innerText.slice(0, 20)); }")
    if how == "ask-click":
        assert active.startswith("ask|"), active
    else:
        assert "rowbtn" in active, active                                         # back to the element that had focus


def test_sidekick_mouse_close_paths_are_unchanged(make_tab):
    t = make_tab()
    p = t.page
    p.get_by_role("button", name="Ask Sidekick").first.click()
    p.locator(".sk.open").wait_for()
    p.get_by_role("button", name="Close").click()
    p.wait_for_function("() => !document.querySelector('.sk:not(.msg)').classList.contains('open')")
    p.get_by_role("button", name="Ask Sidekick").first.click()
    p.locator(".sk.open").wait_for()
    p.mouse.click(20, 400)                                                        # the backdrop
    p.wait_for_function("() => !document.querySelector('.sk:not(.msg)').classList.contains('open')")


# ---------------------------------------------------------------------------------------------- sample data (U-04)
@pytest.mark.parametrize("size", ["desktop", "narrow"])
@pytest.mark.parametrize("state", ALL_STATES)
def test_sample_data_marking_is_visible_in_the_viewport_on_every_view(make_tab, state, size):
    t = make_tab(size)
    _state(t, state)
    p = t.page
    if state == "sidekick-panel-open":
        p.keyboard.press("Escape")                                                # the chip lives in the page header
        p.wait_for_function("() => !document.querySelector('div.sk[role=dialog]').classList.contains('open')")
    p.evaluate("() => window.scrollTo(0, 0)")
    chip = p.get_by_text("Sample data", exact=True)
    assert chip.count() == 1 and chip.is_visible()
    box = chip.bounding_box()
    w, h = VIEWPORTS[size]
    assert box["x"] >= 0 and box["y"] >= 0 and box["x"] + box["width"] <= w and box["y"] + box["height"] <= h, box
    assert p.evaluate("() => getComputedStyle(document.querySelector('.samplechip')).borderTopStyle") == "dashed"
    status_lines = [ln.strip().lower() for ln in p.inner_text("body").splitlines()]
    assert not [ln for ln in status_lines if ln == "live" or ln.endswith("· live")], "no LIVE status label next to sample data"


def test_overview_eyebrow_is_the_project_name_and_status_pills_say_sample(make_tab):
    t = make_tab()
    p = t.page
    assert p.locator("section.content .eyebrow").first.inner_text().strip().lower() == "customer operations"
    assert p.locator(".topbar .pill").inner_text().strip() == "Healthy (sample)"
    assert p.locator(".brand .txt").inner_text().strip() == "Agentic Apps R0-prime"         # the product name, no "ReDevOps" mark
    t.view("Settings")
    p.get_by_role("tab", name="Security").click()
    assert p.get_by_text("Secured (sample)").count() == 1


# ---------------------------------------------------------------------------------------------- signed out (U-02)
@pytest.mark.parametrize("size", ["desktop", "narrow"])
def test_signed_out_page_is_one_focusable_sign_in_panel(make_tab, size):
    t = make_tab(size, signed_in=False)
    p = t.page
    p.wait_for_selector("#agentic-signin")
    assert p.locator("#agentic-signin").count() == 1
    # the heading has focus; the first Tab stop is inside the panel
    assert p.evaluate("() => document.activeElement.tagName + ':' + document.activeElement.textContent") == \
        "H1:Sign in from the Agentic Apps R0-prime window"
    p.keyboard.press("Tab")
    assert p.evaluate("() => !!document.activeElement.closest('#agentic-signin')")
    # no application chrome, no badges, no endless "Loading", nothing fixed over a header
    assert p.locator("button.nav:visible").count() == 0 and p.locator(".badge:visible").count() == 0
    assert "Loading" not in p.inner_text("body")
    root = p.locator("#root")
    assert root.get_attribute("hidden") is not None and root.get_attribute("inert") is not None
    assert p.locator("[role=alert]").count() == 1
    assert p.evaluate("() => getComputedStyle(document.querySelector('[role=alert]')).position") != "fixed"
    assert p.locator(".topbar:visible").count() == 0
    # the developer sentence is behind a closed <details>, operable with the keyboard
    det = p.locator("#agentic-signin details")
    assert det.get_attribute("open") is None
    assert not p.get_by_text("serve command").is_visible()
    p.locator("#agentic-signin summary").focus()
    p.keyboard.press("Enter")
    assert det.get_attribute("open") is not None and p.get_by_text("serve command").is_visible()
    # text anchors and the About hook
    txt = p.inner_text("#agentic-signin")
    assert "Not signed in to the local Agentic Apps service" in txt and 'press "Start & open"' in txt
    assert "Agentic Apps R0-prime" in txt and "ReDevOps" not in txt
    assert p.get_by_role("link", name="About and licences").get_attribute("href") == "./about/"
    # axe: nothing, including region / main / h1
    assert t.axe() == {}
    # the sidecar saw exactly one unauthenticated read and no write
    assert [(m, s) for m, u, s in t.api] == [("GET", 401)], t.api
    assert t.console_errors == [x for x in t.console_errors if "401" in x], t.console_errors


def test_a_401_during_a_session_shows_the_panel_again(make_tab):
    t = make_tab()
    p = t.page
    t.server["cap"]._sessions.clear()                               # the server forgets the session (restart / eviction)
    t.view("Missions")                                              # the next API call answers 401
    p.wait_for_selector("#agentic-signin")
    assert p.locator("button.nav:visible").count() == 0
    # a new tab without the fragment is signed out; the signed-in tab did not leak anything
    t2 = make_tab(signed_in=False)
    t2.page.wait_for_selector("#agentic-signin")


def test_reload_of_the_signed_in_tab_stays_signed_in_and_the_api_stays_gated(make_tab, server):
    t = make_tab()
    t.page.reload()
    t.page.wait_for_selector("h1.h")
    assert t.page.locator("#agentic-signin").count() == 0
    import urllib.error
    import urllib.request
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(server["url"] + "api/projects/customer-ops/overview")
    assert e.value.code == 401


def test_the_page_has_no_console_errors_or_csp_violations_while_walking_the_views(make_tab):
    t = make_tab()
    t.page.add_init_script("window.__csp = []; document.addEventListener('securitypolicyviolation', e => window.__csp.push(e.violatedDirective));")
    t.page.reload()
    t.page.wait_for_selector("h1.h")
    for v in VIEWS:
        t.view(v)
    t.open_mission()
    assert t.page.evaluate("() => window.__csp") == []
    assert t.console_errors == [] and t.failed == []


def test_rail_buttons_and_the_about_link_keep_their_names_when_the_rail_collapses(make_tab):
    t = make_tab("narrow")
    p = t.page
    for v in VIEWS:
        assert p.get_by_role("button", name=v, exact=True).count() == 1, v       # glyph is hidden, the label is the name
    link = p.get_by_role("link", name="About and licences")
    assert link.get_attribute("href") == "./about/" and link.is_visible()
    box = link.bounding_box()
    assert box["width"] >= 24 and box["height"] >= 24                           # WCAG 2.2 target size

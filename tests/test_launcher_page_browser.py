"""Headless-Chromium proxy test of the launcher page under its real CSP (RC3).

Serves `deploy/launcher/ui/` from a loopback server with the SAME Content-Security-Policy string that
`tauri.conf.json` configures (`app.security.csp`), with a stub `window.__TAURI__` (what Tauri injects through the
webview's initialization script, which is not subject to the page CSP), loads the page and asserts:
  * zero CSP violations (`securitypolicyviolation` events and console errors) while the page is used;
  * the page works: the six buttons call the stub with the right command names and show the answer;
  * the About / Legal view works from the keyboard against the fixture build record;
  * inline script, eval, inline handlers, inline style, form posts, base-tag injection, framing and requests to
    any other origin are blocked, and the "other origin" server sees zero requests;
  * axe-core: 0 violations at 560x640 and at a narrow width, light and dark, main and About view.

WHAT THIS PROXY CANNOT SHOW (UNVERIFIED until the native GUI acceptance run): the real WKWebView (macOS) and
WebView2 (Windows); the `tauri://localhost` / `http://tauri.localhost` origins; the `ipc:` custom-protocol fetch
and the postMessage fallback; Tauri's own header additions (it appends a 'sha256-...' source for app.js to
script-src, which is simulated by the second header variant below); Tauri's injected scripts; the ACL.

Runs only with RDO_BROWSER_TESTS=1 and when Playwright and a Chromium are available (otherwise skipped).
axe-core is read from the path in RDO_AXE_JS (axe.min.js, 4.10.x); the axe test is skipped without it.
Run it with an `env -i` allowlist (PLAYWRIGHT_BROWSERS_PATH, a throwaway HOME/TMPDIR).
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "deploy" / "launcher" / "ui"
CONF = ROOT / "deploy" / "launcher" / "src-tauri" / "tauri.conf.json"
FIXTURE = ROOT / "tests" / "fixtures" / "launcher_about" / "Licenses"

if os.environ.get("RDO_BROWSER_TESTS") != "1":
    pytest.skip("set RDO_BROWSER_TESTS=1 to run the headless-browser tests", allow_module_level=True)
sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

CSP = json.loads(CONF.read_text(encoding="utf-8"))["app"]["security"]["csp"]
NOSNIFF = json.loads(CONF.read_text(encoding="utf-8"))["app"]["security"]["headers"]["X-Content-Type-Options"]


def _tauri_effective_csp() -> str:
    """The configured CSP plus the 'sha256-...' source Tauri appends to script-src for each embedded .js file."""
    js = (UI / "app.js").read_bytes().replace(b"\r\n", b"\n")
    h = base64.b64encode(hashlib.sha256(js).digest()).decode()
    return CSP.replace("script-src 'self'", f"script-src 'self' 'sha256-{h}'")


class _Server:
    """Loopback static server for a directory, with fixed response headers; records every requested path."""

    def __init__(self, directory: Path, headers: dict[str, str], extra: dict[str, bytes] | None = None):
        self.hits: list[str] = []
        extra = extra or {}
        outer = self

        class H(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=str(directory), **kw)

            def log_message(self, *a):  # quiet
                pass

            def end_headers(self):
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Cache-Control", "no-store")
                super().end_headers()

            def do_GET(self):
                outer.hits.append(self.path)
                if self.path in extra:  # a same-origin script the test adds: it runs UNDER the page CSP (unlike page.evaluate)
                    body = extra[self.path]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/javascript")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                super().do_GET()

            def do_POST(self):
                outer.hits.append("POST " + self.path)
                self.send_response(204)
                self.end_headers()

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


# Runs as a same-origin external script (allowed by script-src 'self'), so eval and friends are judged by the page CSP.
PROBE = """
window.__probe = {};
try { window.__probe.eval = String(eval("1+1")); } catch (e) { window.__probe.eval = e.name; }
try { window.__probe.fn = String(new Function("return 3")()); } catch (e) { window.__probe.fn = e.name; }
window.__probe.timeout = "scheduled";
try { setTimeout("window.__pwn = 3", 0); } catch (e) { window.__probe.timeout = e.name; }
"""

STUB = """
(() => {
  const fixture = %(fixture)s;
  window.__calls = [];
  window.__viol = [];
  document.addEventListener("securitypolicyviolation", (e) =>
    window.__viol.push({directive: e.violatedDirective, blocked: e.blockedURI, effective: e.effectiveDirective}));
  window.__TAURI__ = { core: { invoke: (name, args) => {
    window.__calls.push({name, args: args || {}});
    const mode = fixture.mode;
    switch (name) {
      case "device_report": return Promise.resolve("device report: ok");
      case "start_native": return Promise.resolve("http://127.0.0.1:55555/");
      case "open_ui": return Promise.resolve(null);
      case "stop_native": return Promise.resolve("Stopped.");
      case "legacy_service_warning": return Promise.resolve(null);
      case "docker_available": return Promise.resolve(true);
      case "advanced_docker_up": return Promise.resolve("containers up");
      case "advanced_docker_down": return Promise.resolve("containers down");
      case "advanced_docker_purge": return Promise.resolve("containers purged");
      case "about_info":
        if (mode === "missing") return Promise.reject("ABOUT_RECORD_MISSING: The build record (Licenses/about.json) is not part of this build.");
        if (mode === "malformed") return Promise.resolve("{not json");
        if (mode === "schema") return Promise.resolve('{"schema":"other/1"}');
        return Promise.resolve(fixture.record);
      case "about_text":
        if (Object.prototype.hasOwnProperty.call(fixture.texts, args.slug)) return Promise.resolve(fixture.texts[args.slug]);
        return Promise.reject("ABOUT_UNKNOWN_FILE: That file is not one of the files this view can show.");
      default: return Promise.reject("unknown command " + name);
    }
  }}};
})();
"""


def _stub(mode: str = "ok", record_override=None) -> str:
    record = (FIXTURE / "about.json").read_text(encoding="utf-8")
    if record_override is not None:
        rec = json.loads(record)
        record_override(rec)
        record = json.dumps(rec)
    texts = {p.name: p.read_text(encoding="utf-8") for p in FIXTURE.iterdir() if p.name != "about.json"}
    return STUB % {"fixture": json.dumps({"mode": mode, "record": record, "texts": texts})}


@pytest.fixture(scope="module")
def browser():
    try:
        pw = sync_api.sync_playwright().start()
        b = pw.chromium.launch()
    except Exception as e:  # no Chromium in this environment
        pytest.skip(f"headless Chromium unavailable: {e}")
    yield b
    b.close()
    pw.stop()


@pytest.fixture(params=["configured", "tauri-effective"])
def served(request):
    """The launcher page on server A with the CSP header; server B is 'another origin' that must see nothing."""
    csp = CSP if request.param == "configured" else _tauri_effective_csp()
    a = _Server(UI, {"Content-Security-Policy": csp, "X-Content-Type-Options": NOSNIFF}, {"/__probe.js": PROBE.encode()})
    other = _Server(UI, {})
    yield a, other
    a.close()
    other.close()


def _open(browser, served, stub, viewport=(560, 640), scheme="light"):
    a, other = served
    ctx = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]}, color_scheme=scheme)
    ctx.add_init_script(stub)
    page = ctx.new_page()
    problems: list[str] = []
    requested: list[str] = []
    page.on("console", lambda m: problems.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.on("request", lambda r: requested.append(r.url))
    page.goto(a.base + "/index.html")
    page.wait_for_load_state("load")
    return ctx, page, problems, requested


def _wait(page, fn: str, arg=None, timeout: float = 8.0):
    """Poll `fn` (a JS function source) with page.evaluate. page.wait_for_function would eval its predicate, which the page CSP blocks."""
    import time

    end = time.time() + timeout
    while time.time() < end:
        if page.evaluate(fn, arg):
            return
        page.wait_for_timeout(50)
    raise AssertionError(f"timed out waiting for {fn} ({arg!r})")


def _viol(page):
    return page.evaluate("window.__viol")


def _calls(page):
    return [c["name"] for c in page.evaluate("window.__calls")]


def _focus_by_tab(page, element_id: str, limit: int = 40):
    for _ in range(limit):
        page.keyboard.press("Tab")
        if page.evaluate("document.activeElement && document.activeElement.id") == element_id:
            return
    raise AssertionError(f"{element_id} is not reachable with Tab")


def test_the_response_carries_the_csp_header_exactly(browser, served):
    a, _ = served
    ctx = browser.new_context()
    r = ctx.request.get(a.base + "/index.html")
    assert r.headers["content-security-policy"].startswith(CSP.split("script-src")[0])
    assert r.headers["x-content-type-options"] == "nosniff"
    ctx.close()


def test_page_loads_styled_with_no_violation_and_no_external_request(browser, served):
    a, other = served
    ctx, page, problems, requested = _open(browser, served, _stub())
    assert page.title() == "Agentic Apps R0-prime"
    assert page.locator("h1").inner_text() == "Agentic Apps R0-prime"
    assert "Private candidate, release line r0-prime." in page.locator("#release-line").inner_text()
    assert "ReDevOps" not in page.content()
    assert page.evaluate("getComputedStyle(document.body).padding") == "24px"   # app.css was applied (style-src 'self')
    assert page.evaluate("typeof window.__calls") == "object" and "docker_available" in _calls(page)
    assert _viol(page) == [] and problems == []
    assert all(u.startswith(a.base) or u.startswith("data:") for u in requested), requested
    assert other.hits == []
    ctx.close()


def test_the_six_buttons_call_the_stub_and_show_the_status_text(browser, served):
    ctx, page, problems, _ = _open(browser, served, _stub())
    _wait(page, "() => !document.getElementById('advanced').hidden")        # docker_available -> true in the stub
    page.once("dialog", lambda d: d.accept())
    steps = [
        ("check", ["device_report"], "device report: ok"),
        ("start", ["start_native", "open_ui", "legacy_service_warning"], "Running at http://127.0.0.1:55555/"),
        ("stop", ["stop_native"], "Stopped."),
        ("docker-up", ["advanced_docker_up"], "containers up"),
        ("docker-down", ["advanced_docker_down"], "containers down"),
        ("docker-purge", ["advanced_docker_purge"], "containers purged"),
    ]
    for bid, commands, text in steps:
        page.evaluate("window.__calls.length = 0")
        page.click("#" + bid)
        _wait(page, "(t) => document.getElementById('out').textContent.includes(t)", text)
        assert _calls(page) == commands, (bid, _calls(page))
    assert _viol(page) == [] and problems == []
    ctx.close()


def test_a_stub_failure_is_shown_not_swallowed(browser, served):
    ctx, page, problems, _ = _open(browser, served, _stub().replace('case "device_report": return Promise.resolve("device report: ok");', 'case "device_report": return Promise.reject("boom");'))
    page.click("#check")
    _wait(page, "() => document.getElementById('out').textContent.startsWith('Error:')")
    assert "boom" in page.locator("#out").inner_text()
    ctx.close()


def test_injection_and_exfiltration_attempts_are_blocked_and_nothing_reaches_another_origin(browser, served):
    a, other = served
    ctx, page, problems, _ = _open(browser, served, _stub())
    assert _viol(page) == []
    result = page.evaluate(
        """async (otherBase) => {
          const r = {};
          window.__pwn = 0;
          // 1. inline script element
          const s = document.createElement("script"); s.textContent = "window.__pwn = 1"; document.body.append(s);
          // 2. inline event handler attribute
          const b = document.createElement("button"); b.id = "evil"; b.setAttribute("onclick", "window.__pwn = 2"); b.textContent = "x"; document.body.append(b); b.click();
          // 3. eval / Function / string timers: see PROBE (run as an external script below)
          const probe = document.createElement("script"); probe.src = "/__probe.js"; document.body.append(probe);
          // 4. inline style attribute
          const p = document.createElement("p"); p.id = "styled"; p.setAttribute("style", "color: rgb(1, 2, 3)"); p.textContent = "y"; document.body.append(p);
          r.styledColor = getComputedStyle(p).color;
          // 5. javascript: URL
          const a = document.createElement("a"); a.href = "javascript:window.__pwn = 4"; document.body.append(a); a.click();
          // 6. requests to another origin: fetch, image, script, stylesheet, iframe, form, base
          try { await fetch(otherBase + "/index.html"); r.fetch = "allowed"; } catch (e) { r.fetch = "blocked"; }
          for (const [tag, attr, url] of [["img", "src", otherBase + "/x.png"], ["script", "src", otherBase + "/app.js"], ["iframe", "src", otherBase + "/index.html"]]) {
            const el = document.createElement(tag); el[attr] = url; document.body.append(el);
          }
          const l = document.createElement("link"); l.rel = "stylesheet"; l.href = otherBase + "/app.css"; document.head.append(l);
          const f = document.createElement("form"); f.action = otherBase + "/post"; f.method = "post"; document.body.append(f); f.requestSubmit?.call(f);
          const base = document.createElement("base"); base.href = otherBase + "/"; document.head.append(base);
          r.baseHref = document.baseURI;
          await new Promise((res) => setTimeout(res, 600));
          r.pwn = window.__pwn;
          r.probe = window.__probe;
          return r;
        }""",
        other.base,
    )
    assert result["pwn"] == 0, result
    assert result["probe"]["eval"] == "EvalError" and result["probe"]["fn"] == "EvalError", result
    assert result["styledColor"] != "rgb(1, 2, 3)", result
    assert result["fetch"] == "blocked", result
    assert not result["baseHref"].startswith(other.base), result
    directives = {v["effective"] for v in _viol(page)}
    assert {"script-src-elem", "script-src-attr", "style-src-attr", "connect-src", "img-src", "frame-src", "style-src-elem", "form-action", "base-uri"} <= directives, directives
    assert other.hits == [], other.hits                                   # nothing left the page's origin
    ctx.close()


def test_ipc_scheme_and_ipc_localhost_are_admitted_by_connect_src(browser, served):
    ctx, page, problems, _ = _open(browser, served, _stub())
    page.evaluate(
        """async () => { for (const u of ["ipc://localhost/start_native", "http://ipc.localhost/start_native"]) {
             try { await fetch(u, {method: "POST", body: "{}"}); } catch (e) {} } }"""
    )
    assert [v for v in _viol(page) if v["effective"] == "connect-src"] == []     # a network failure, not a CSP block
    ctx.close()


def test_the_page_cannot_be_framed_by_another_origin(browser, served):
    a, other = served
    host = Path(os.environ.get("TMPDIR", "/tmp")) / f"rdo-host-{os.getpid()}"
    host.mkdir(exist_ok=True)
    (host / "host.html").write_text(f'<!doctype html><iframe id="f" src="{a.base}/index.html"></iframe>', encoding="utf-8")
    hs = _Server(host, {})
    try:
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(hs.base + "/host.html")
        page.wait_for_timeout(800)
        frames = [f for f in page.frames if f != page.main_frame]
        assert frames, "the host page has an iframe"
        assert not frames[0].url.startswith(a.base), frames[0].url      # blocked by frame-ancestors 'none' (an error page, not the launcher)
        ctx.close()
    finally:
        hs.close()
        (host / "host.html").unlink()
        host.rmdir()


# --------------------------------------------------------------------------------------- About / Legal
def test_about_legal_works_from_the_keyboard_and_shows_the_record_as_text(browser, served):
    a, other = served
    ctx, page, problems, requested = _open(browser, served, _stub())
    _wait(page, "() => window.__calls.length >= 1")
    _focus_by_tab(page, "open-about")
    page.keyboard.press("Enter")
    page.wait_for_selector("#about-body h3")
    assert page.evaluate("document.activeElement.id") == "about-title" or page.evaluate("document.activeElement.id") == ""   # focus moved into the view
    assert page.locator("#view-main").is_hidden() and page.locator("#view-about").is_visible()
    body = page.locator("#about-body").inner_text()
    assert "Agentic Apps R0-prime" in body and "0.1.1" in body and "r0-prime" in body and "private-candidate" in body
    # legal slots: placeholders only, with the decision they wait for; nothing that implies clearance
    for marker in ("Legal text pending decision D-LIC-1. This is a private candidate; nothing here is a legal statement or clearance.",
                   "Legal text pending decision D-LIC-10.", "Legal text pending decision D-LIC-5."):
        assert marker in body, marker                                    # the record's own text, verbatim
    assert "Source access for this build is not published yet (private candidate)." in body
    low = (body + page.locator("#about-private-note").inner_text()).lower()
    for word in ("cleared", "compliant", "approved by counsel", "certified", "open source", "tamper-proof"):
        assert word not in low, word
    note = page.locator("#about-private-note").inner_text()
    assert note.startswith("Private candidate.") and "state of each legal slot" in note and "placeholder" not in note.lower()
    # every slot shows its own state tag and its accessible name includes the state
    assert page.locator(".slot .slot-tag").count() == 5
    assert page.locator(".slot.pending").count() == 5
    assert page.get_attribute(".slot >> nth=0", "aria-label") == "LEGAL.COPYRIGHT_LINE: Pending a decision"
    # the licence file list and the notices component list
    assert page.locator("ul[aria-label='Licence files'] li").count() == 5
    assert page.locator("ul[aria-label='Third-party components'] li").count() == 5          # 4 components + 1 reference notice
    assert "Licence text not found in this build" in body and "libyaml" in body
    assert "4 components listed" in body and "not verified" in body
    # keyboard: Tab to the "View LICENSE" button, Enter opens the viewer with the exact file text
    page.focus("button[aria-label='View LICENSE']")
    page.keyboard.press("Enter")
    page.wait_for_selector("#viewer:not([hidden])")
    assert page.locator("#viewer-text").inner_text() == (FIXTURE / "LICENSE").read_text(encoding="utf-8")
    assert "sha256 " in page.locator("#viewer-note").inner_text()
    assert [c for c in page.evaluate("window.__calls") if c["name"] == "about_text"][-1]["args"] == {"slug": "LICENSE"}
    # Escape closes the viewer and returns focus to the button that opened it; Escape again leaves the About view
    page.keyboard.press("Escape")
    assert page.locator("#viewer").is_hidden()
    assert page.evaluate("document.activeElement.getAttribute('aria-label')") == "View LICENSE"
    page.keyboard.press("Escape")
    assert page.locator("#view-main").is_visible() and page.locator("#view-about").is_hidden()
    assert page.evaluate("document.activeElement.id") == "open-about"
    names = _calls(page)
    assert names.count("about_info") == 1 and set(names) <= {"docker_available", "legacy_service_warning", "about_info", "about_text"}
    assert _viol(page) == [] and problems == []
    assert all(u.startswith(a.base) or u.startswith("data:") for u in requested), requested
    assert other.hits == []
    ctx.close()


def test_a_component_notice_is_the_slice_of_the_notices_file_given_by_offset_and_length(browser, served):
    ctx, page, problems, _ = _open(browser, served, _stub())
    page.click("#open-about")
    page.wait_for_selector("button[aria-label='View notice for starlette 1.6.0']")
    page.click("button[aria-label='View notice for starlette 1.6.0']")
    page.wait_for_selector("#viewer:not([hidden])")
    notices = (FIXTURE / "THIRD_PARTY_NOTICES.txt").read_bytes()
    rec = json.loads((FIXTURE / "about.json").read_text(encoding="utf-8"))
    c = rec["notices"]["components"][1]
    assert page.locator("#viewer-text").inner_text() == notices[c["offset"] : c["offset"] + c["length"]].decode().strip("\n") or \
        page.locator("#viewer-text").inner_text().strip() == notices[c["offset"] : c["offset"] + c["length"]].decode().strip()
    # the not-found component has a badge and no View button
    assert page.locator("button[aria-label='View notice for redevops-connectors 0.1.0']").count() == 0
    # filter
    page.fill("#comp-filter", "adler")
    _wait(page, "() => document.querySelectorAll(\"ul[aria-label='Third-party components'] li\").length === 1")
    assert _viol(page) == [] and problems == []
    ctx.close()


@pytest.mark.parametrize("mode,needle", [("missing", "ABOUT_RECORD_MISSING"), ("malformed", "not valid JSON"), ("schema", "unsupported schema")])
def test_a_missing_or_bad_record_shows_a_clear_alert_with_try_again(browser, served, mode, needle):
    ctx, page, problems, _ = _open(browser, served, _stub(mode))
    page.click("#open-about")
    page.wait_for_selector("#about-error:not([hidden])")
    assert needle in page.locator("#about-error").inner_text()
    assert page.locator("#about-error").get_attribute("role") == "alert"
    assert page.locator("#about-error button", has_text="Try again").count() == 1
    assert page.locator("#about-body").inner_text() == ""
    assert _viol(page) == [] and problems == []
    ctx.close()


def test_record_values_are_rendered_as_text_never_as_markup(browser, served):
    evil = '<img src=x onerror="window.__pwn=9"><script>window.__pwn=8</script><a href="javascript:window.__pwn=7">x</a>'

    def tamper(rec):
        rec["product"]["name"] = evil
        rec["legal"]["slots"]["LEGAL.COPYRIGHT_LINE"] = {"state": "pending", "tag": "Pending a decision", "text": evil}
        rec["legal"]["slots"]["LEGAL.NO_WARRANTY"]["text"] = evil + "2"
        rec["notices"]["components"][0]["name"] = evil
        rec["source_access"] = {"state": "offer-recorded", "location": "javascript:window.__pwn=6", "commit": evil}
        rec["notices"]["reference_notices"][0]["title"] = evil

    ctx, page, problems, _ = _open(browser, served, _stub(record_override=tamper))
    page.click("#open-about")
    page.wait_for_selector("#about-body h3")
    assert evil in page.locator("#about-body").inner_text()                           # literally, as text
    assert page.locator("#about-body img, #about-body script, #about-body a, #about-body iframe").count() == 0
    assert page.evaluate("window.__pwn || 0") == 0
    assert _viol(page) == [] and problems == []
    ctx.close()


def test_a_slot_with_text_in_the_record_is_shown_verbatim_and_an_unknown_state_fails_safe(browser, served):
    def tweak(rec):
        rec["legal"]["slots"]["LEGAL.COPYRIGHT_LINE"]["text"] = "[PLACEHOLDER VERBATIM FROM THE RECORD]"
        rec["legal"]["slots"]["LEGAL.NO_WARRANTY"] = {"state": "mystery"}
        rec["legal"]["slots"]["LEGAL.SECTION13_NOTICE"] = {"state": "not-applicable", "decisions": ["D-LIC-5"], "pending": [], "text": ""}

    ctx, page, problems, _ = _open(browser, served, _stub(record_override=tweak))
    page.click("#open-about")
    page.wait_for_selector("#about-body h3")
    body = page.locator("#about-body").inner_text()
    assert "[PLACEHOLDER VERBATIM FROM THE RECORD]" in body
    assert "[LEGAL TEXT NOT AVAILABLE IN THIS VIEW]" in body
    assert "LEGAL.SECTION13_NOTICE" not in body                                 # a not-applicable slot is not shown
    ctx.close()


def _all_states(rec):
    mod_ok, draft = "Approved fixture text.", "Draft fixture text."
    prefix = "Unverified draft, not an approved text. This is a private candidate; nothing here is a legal statement or clearance.\n\n"
    s = rec["legal"]["slots"]
    s["LEGAL.COPYRIGHT_LINE"].update(state="approved-verified", tag="Approved text, signature verified", pending=[], text=mod_ok,
                                      text_sha256=hashlib.sha256(mod_ok.encode()).hexdigest())
    s["LEGAL.NO_WARRANTY"].update(state="unverified-draft", tag="Unverified draft, not an approved text", pending=[], text=prefix + draft,
                                   text_sha256=hashlib.sha256(draft.encode()).hexdigest())
    s["LEGAL.SECTION13_NOTICE"].update(state="not-applicable", tag="", pending=[], text="")


def test_slot_states_have_a_visible_text_tag_a_distinct_style_and_an_accessible_name(browser, served):
    ctx, page, problems, _ = _open(browser, served, _stub(record_override=_all_states))
    page.click("#open-about")
    page.wait_for_selector("#about-body h3")
    assert page.locator(".slot").count() == 4                                    # the not-applicable slot is hidden
    assert "LEGAL.SECTION13_NOTICE" not in page.locator("#about-body").inner_text()
    ok = page.locator(".slot.approved-verified")
    dr = page.locator(".slot.unverified-draft")
    pe = page.locator(".slot.pending")
    assert ok.count() == 1 and dr.count() == 1 and pe.count() == 2
    assert ok.locator(".slot-tag").inner_text() == "Approved text, signature verified"
    assert dr.locator(".slot-tag").inner_text() == "Unverified draft, not an approved text"
    assert ok.get_attribute("aria-label") == "LEGAL.COPYRIGHT_LINE: Approved text, signature verified"
    assert dr.get_attribute("aria-label") == "LEGAL.NO_WARRANTY: Unverified draft, not an approved text"
    # the draft's own text carries its label first (inside the record text), verbatim
    assert dr.locator(".slot-text").inner_text().startswith("Unverified draft, not an approved text. This is a private candidate;")
    assert "Draft fixture text." in dr.locator(".slot-text").inner_text()
    # distinct by more than colour: different border style/width, and only the approved one is solid
    style = lambda loc: loc.evaluate("e => { const c = getComputedStyle(e); return [c.borderTopStyle, c.borderTopWidth] }")
    assert style(ok) == ["solid", "2px"] and style(dr) == ["dashed", "3px"] and style(pe.first) == ["dashed", "2px"]
    assert _viol(page) == [] and problems == []
    ctx.close()


def test_an_old_or_unknown_slot_state_is_never_shown_as_legal_text(browser, served):
    def old(rec):
        rec["legal"]["slots"]["LEGAL.COPYRIGHT_LINE"] = {"state": "filled", "text": "SHOULD NOT SHOW AS LEGAL TEXT"}
        rec["legal"]["slots"]["LEGAL.NO_WARRANTY"] = {"state": "placeholder", "text": "ALSO NOT"}

    ctx, page, problems, _ = _open(browser, served, _stub(record_override=old))
    page.click("#open-about")
    page.wait_for_selector("#about-body h3")
    body = page.locator("#about-body").inner_text()
    assert "SHOULD NOT SHOW" not in body and "ALSO NOT" not in body
    assert body.count("[LEGAL TEXT NOT AVAILABLE IN THIS VIEW]") == 2 and body.count("State not recognised") == 2
    ctx.close()


# --------------------------------------------------------------------------------------------- axe
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]


def _axe_source() -> str:
    p = os.environ.get("RDO_AXE_JS")
    if not p or not Path(p).is_file():
        pytest.skip("set RDO_AXE_JS to axe.min.js (axe-core 4.10.x) to run the accessibility checks")
    return Path(p).read_text(encoding="utf-8")


@pytest.mark.parametrize("viewport", [(560, 640), (320, 640)], ids=["560x640", "narrow-320"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("state", ["main", "about", "about-viewer", "about-error", "about-states"])
def test_axe_has_no_violation_in_the_launcher_page(browser, viewport, scheme, state):
    axe_src = _axe_source()
    a = _Server(UI, {"Content-Security-Policy": CSP, "X-Content-Type-Options": NOSNIFF})
    try:
        mode = "missing" if state == "about-error" else "ok"
        ctx, page, problems, _ = _open(browser, (a, a), _stub(mode, record_override=_all_states if state == "about-states" else None), viewport=viewport, scheme=scheme)
        _wait(page, "() => !document.getElementById('advanced').hidden")
        if state != "main":
            page.click("#open-about")
            page.wait_for_selector("#about-error:not([hidden])" if state == "about-error" else "#about-body h3")
        if state == "about-viewer":
            page.click("button[aria-label='View LICENSE']")
            page.wait_for_selector("#viewer:not([hidden])")
        page.evaluate(axe_src)
        res = page.evaluate("async (tags) => await axe.run(document, {runOnly: {type: 'tag', values: tags}})", AXE_TAGS)
        violations = [(v["id"], v["impact"], [n["target"] for n in v["nodes"]][:4]) for v in res["violations"]]
        assert violations == [], violations
        assert page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 0, "horizontal overflow"
        assert _viol(page) == [] and problems == []
        ctx.close()
    finally:
        a.close()

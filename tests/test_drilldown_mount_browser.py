"""End to end in a real browser: the drill-down through the PRODUCT mount, signed in with the launch capability only.

Headless Chromium against ``projects_api.mounted_app`` (the real local capability gate, the real session-to-principal bridge, the real encrypted
grant store, the real audit sink, the vendored UI bundle) started in-process on an ephemeral loopback port, over the log of a real
``MissionRuntime`` demo mission. The test plays the launcher (it asks the capability for a launch code) and the operator
(``grant_local_inspect`` / ``revoke_local_inspect``, what ``agentic-os permissions allow-drilldown`` runs). No bearer token and no principal is
injected anywhere; the browser sends the launch session only.

States: unconfigured -> Sample; configured, no grant -> "no recorded history" with the access explanation; operator grants -> all eight views Recorded;
operator revokes -> the next Refresh says "Access changed while you were viewing".

Opt-in: RDO_BROWSER_TESTS=1 (and a Chromium in PLAYWRIGHT_BROWSERS_PATH or the default cache).
"""
from __future__ import annotations

import os
import socket
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RDO_BROWSER_TESTS") != "1", reason="browser tests are opt-in: set RDO_BROWSER_TESTS=1")

VIEWS = ["summary", "tasks", "evidence", "timeline", "graph", "world", "explain", "replay"]
LABEL = {"summary": "Summary", "tasks": "Tasks", "evidence": "Evidence", "timeline": "Timeline", "graph": "Graph", "world": "World State", "explain": "EXPLAIN", "replay": "Replay"}


@pytest.fixture(scope="module")
def sync_playwright_cm():
    return pytest.importorskip("playwright.sync_api").sync_playwright


@pytest.fixture(scope="module")
def browser(sync_playwright_cm):
    with sync_playwright_cm() as pw:
        try:
            b = pw.chromium.launch(headless=True)
        except Exception as e:                                       # no Chromium in this environment
            pytest.skip(f"Chromium is not available: {e}")
        yield b
        b.close()


def _serve(app, cap):
    import uvicorn
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    cap.bind_port(port)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started, "uvicorn did not start"
    return srv, th, port


@pytest.fixture()
def product(tmp_path, monkeypatch):
    """Two real products over the same real mission log: one configured for the drill-down, one not (the desktop's default)."""
    from agentic_os.drilldown.mount import DrilldownConfig
    from agentic_os.evidence_query import JsonlAuditSink
    from agentic_os.local_capability import LocalCapability
    from agentic_os.mission.store import EventStore
    from agentic_os.permissions import GrantStore
    from agentic_os.projects_api import SampleProjectionProvider, mounted_app
    from tests.drilldown_support import SCOPE, completed_mission, set_env
    set_env(monkeypatch)
    monkeypatch.delenv("AGENT_GATEWAY_TOKEN", raising=False)
    log = str(tmp_path / "events.jsonl")
    _, mid = completed_mission(log)
    grants = GrantStore(str(tmp_path / "perm"))
    audit = JsonlAuditSink(str(tmp_path / "audit.jsonl"))
    cfg = DrilldownConfig(store=EventStore(path=log, strict=False), grants=grants, audit=audit, project_id=SCOPE)
    cap_rt, cap_sample = LocalCapability(), LocalCapability()
    rt = _serve(mounted_app("", SampleProjectionProvider(), capability=cap_rt, drilldown=cfg), cap_rt)
    sm = _serve(mounted_app("", SampleProjectionProvider(), capability=cap_sample), cap_sample)
    yield type("P", (), dict(mid=mid, grants=grants, audit=audit, scope=SCOPE, rt_url=f"http://127.0.0.1:{rt[2]}/", sample_url=f"http://127.0.0.1:{sm[2]}/",
                             cap_rt=cap_rt, cap_sample=cap_sample))
    for srv, th, _ in (rt, sm):
        srv.should_exit = True
        th.join(timeout=10)


class Tab:
    def __init__(self, browser, url, cap):
        self.ctx = browser.new_context(viewport={"width": 1280, "height": 800}, reduced_motion="reduce")
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(10000)
        self.url, self.cap = url, cap
        self.headers: list[dict] = []
        self.page.on("request", lambda r: self.headers.append({"url": r.url, **{k.lower(): v for k, v in r.headers.items()}}))

    def sign_in(self):
        self.page.goto(f"{self.url}#lc={self.cap.issue_launch_code()}")           # the launcher's job; the page exchanges it for a session
        self.page.wait_for_function("() => !!sessionStorage.getItem('agentic_os.session')")

    def go(self, hash_):
        self.page.goto(f"{self.url}#{hash_}")
        self.page.reload()
        self.page.wait_for_selector(".content, .mview")

    def close(self):
        self.ctx.close()


def tab(browser, product, which="rt"):
    t = Tab(browser, product.rt_url if which == "rt" else product.sample_url, product.cap_rt if which == "rt" else product.cap_sample)
    t.sign_in()
    return t


def test_unconfigured_product_says_sample_and_nothing_recorded(browser, product):
    t = tab(browser, product, "sample")
    try:
        t.go("/missions/4821/summary")
        t.page.wait_for_selector('.mview [data-basis="sample"]')
        assert t.page.locator(".samplechip").first.inner_text() == "Sample data"
        bases = set(t.page.eval_on_selector_all(".mview [data-basis]", "els => els.map(e => e.getAttribute('data-basis'))"))
        assert bases == {"sample"}
        cfg = t.page.evaluate("async () => (await fetch('/api/ui-config')).json()")
        assert cfg["data_source"] == "sample" and cfg["drilldown"]["mounted"] is False and cfg["stores"] == []
        assert t.page.evaluate(f"async () => (await fetch('/api/projects/{product.scope}/missions/{product.mid}/timeline')).status") == 404
    finally:
        t.close()


def test_no_grant_then_grant_then_revoke_through_the_product_mount(browser, product):
    from agentic_os.drilldown.operator import grant_local_inspect, revoke_local_inspect
    t = tab(browser, product)
    p = t.page
    try:
        # 1. the launch session alone: no recorded history on any of the eight views, no Sample chip (the server says runtime)
        for v in VIEWS:
            t.go(f"/missions/{product.mid}/{v}")
            alert = p.locator(".statebox[role=alert]").first
            alert.wait_for()
            text = alert.inner_text()
            assert "No recorded history to show" in text and "not been granted access" in text and "does not say which of these applies" in text, (v, text)
            assert p.locator("[data-basis]").count() == 0, v
            assert p.locator(".samplechip").count() == 0, v
            assert p.get_by_role("button", name="Partly sample").count() == 1, v        # runtime server, provider-backed pages still sample
        # 2. the operator grants inspect for the project (recorded in the audit log); the same open session now reads all eight views
        grant_local_inspect(product.grants, product.scope, operator="browser-test", audit=product.audit)
        for v in VIEWS:
            t.go(f"/missions/{product.mid}/{v}")
            p.wait_for_selector(f'.mview h2:text-is("{LABEL[v]}")')
            p.wait_for_function("() => !document.querySelector('.mview [data-state=loading]')")
            bases = set(p.eval_on_selector_all(".mview [data-basis]", "els => els.map(e => e.getAttribute('data-basis'))"))
            assert bases == {"recorded"}, (v, bases)
            assert p.locator(".samplechip").count() == 0, v
            assert p.get_by_role("button", name="Partly sample").count() == 1, v        # the chip does not hide that these views are recorded: each says so itself
        # 3. the operator revokes while the page is open: the next Refresh says so and removes the content
        t.go(f"/missions/{product.mid}/timeline")
        p.wait_for_selector(".tl")
        revoke_local_inspect(product.grants, product.scope, operator="browser-test", audit=product.audit)
        p.get_by_role("button", name="Refresh").click()
        alert = p.locator(".statebox[role=alert]").first
        alert.wait_for()
        assert "Access changed while you were viewing" in alert.inner_text()
        assert p.locator(".tl").count() == 0
        # nothing in the whole session carried a bearer token, a control token or a principal; only the launch session and CSRF headers
        for h in t.headers:
            for bad in ("authorization", "x-control-token", "x-agentic-principal", "x-principal"):
                assert bad not in h, (bad, h["url"])
        api = [h for h in t.headers if "/api/" in h["url"] and "/api/local/session" not in h["url"]]
        assert api and all("x-agentic-session" in h for h in api)
        # the audit log holds every refused read, the grant, every read that followed, and the revocation, hash-chained
        import json
        recs = [json.loads(x) for x in open(product.audit.path, encoding="utf-8") if x.strip()]
        kinds = [r["kind"] for r in recs]
        g, rv = kinds.index("drilldown.grant"), kinds.index("drilldown.revoke")
        assert g > 0 and all(r["outcome"] == "refused" for r in recs[:g]), "reads before the grant were refused and recorded"
        assert all(r["outcome"] == "ok" for r in recs[g + 1:rv] if r["kind"].startswith("drilldown.") and r["kind"] not in ("drilldown.grant", "drilldown.revoke")), "reads after it were served"
        assert recs[g]["principal"] == "operator:browser-test" and recs[rv]["principal"] == "operator:browser-test"
        assert {r["principal"] for r in recs if r["kind"] in ("drilldown.timeline", "drilldown.evidence", "drilldown.evidence_item") and r["outcome"] == "ok"} == {"local-owner"}
        ok, n = product.audit.verify()
        assert ok and n == len(recs)
    finally:
        t.close()

"""UIA-02 (release qualification 2026-11-05) in a real browser: stop the sidecar under an open tab, then bring it back.

Opt-in (needs Chromium): RDO_BROWSER_TESTS=1 PLAYWRIGHT_BROWSERS_PATH=~/Library/Caches/ms-playwright pytest tests/test_ui_service_notice_browser.py

The vendored bundle shows "Loading..." for ever when a request fails. The shim's service notice must appear (role=alert, names the
launcher button), must not take focus or hide the app, and must go away when the service answers again (same app object, same
session, new uvicorn on the same port).
"""
from __future__ import annotations

import os
import socket
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RDO_BROWSER_TESTS") != "1", reason="browser tests are opt-in: set RDO_BROWSER_TESTS=1")


def _serve(app, port):
    import uvicorn
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            return srv, th
        time.sleep(0.05)
    raise AssertionError("uvicorn did not start")


def _stop(srv, th):
    srv.should_exit = True
    th.join(timeout=10)


def test_notice_appears_when_the_service_stops_and_clears_when_it_returns():
    pw_mod = pytest.importorskip("playwright.sync_api")
    pytest.importorskip("uvicorn")
    from agentic_os.local_capability import LocalCapability
    from agentic_os.projects_api import SampleProjectionProvider, mounted_app
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    cap = LocalCapability(port=port)
    app = mounted_app("", SampleProjectionProvider(), capability=cap)
    srv, th = _serve(app, port)
    try:
        with pw_mod.sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception as e:
                pytest.skip(f"Chromium is not available: {e}")
            page = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
            page.set_default_timeout(8000)
            page.goto(f"http://127.0.0.1:{port}/#lc=" + cap.issue_launch_code())
            page.wait_for_selector("h1.h")
            assert page.locator("#agentic-service-notice").count() == 0        # healthy: no notice
            _stop(srv, th)                                                     # the sidecar goes away under the open tab
            page.get_by_role("button", name="Workflows").click()
            notice = page.locator("#agentic-service-notice")
            notice.wait_for()
            assert notice.get_attribute("role") == "alert"
            text = notice.inner_text()
            assert "not responding" in text and "Start & open" in text and "Agentic Apps R0-prime window" in text
            focus = page.evaluate("document.activeElement && document.activeElement.tagName")
            assert focus != "DIV" and page.locator("#root[hidden], #root[inert]").count() == 0   # focus not stolen, app not hidden
            assert page.get_by_role("button", name="Overview").is_visible()                       # the shell stays usable
            assert page.locator("#agentic-signin").count() == 0                                   # this is not the signed-out panel
            srv, th = _serve(app, port)                                        # the service returns (same session, same port)
            page.get_by_role("button", name="Missions").click()
            page.wait_for_selector("#agentic-service-notice", state="detached")
            page.get_by_role("heading", level=1).first.wait_for()
            browser.close()
    finally:
        _stop(srv, th)

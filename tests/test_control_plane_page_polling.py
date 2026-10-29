"""``/overview`` and ``/m/<name>`` poll ``GET /api/fleet`` for live health dots, and that route needs the fleet credential, which these pages do not hold (it lives in the
dashboard page's memory only and cannot be shared across page loads without storing it). They must therefore ask once: after the first 401 or 503 they stop polling and say
so on one line, instead of making a refused request every five seconds for as long as a tab is open. Behind an authenticating proxy that adds the credential the first
request succeeds and polling continues.

Run with node against a DOM stub (tests/_dashboard_js_harness.js). Node is required in CI (AGENTIC_OS_REQUIRE_NODE_TESTS=1, set by the workflow), skipped locally without it.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_HAS_NODE = bool(shutil.which("node"))
_REQUIRED = os.environ.get("AGENTIC_OS_REQUIRE_NODE_TESTS") == "1"
needs_node = pytest.mark.skipif(not _HAS_NODE and not _REQUIRED, reason="node not installed (set AGENTIC_OS_REQUIRE_NODE_TESTS=1 to make that a failure)")
BODY = "SYNTH-BODY-LEAK-DO-NOT-STORE"
FLEET = [{"name": "m1", "health": "up", "core": "lago", "connected": True}]


def _pages():
    from agentic_os import views
    overview = views.overview_page(groups={"Money": ["m1"]}, has_agent={"m1"},
                                   module_meta={"m1": {"pain": "p", "tagline": "t", "core": "Lago", "repo": "o/r", "group": "Money"}}, workflows=[])
    shell = views.module_shell(name="m1", group="Money", repo="o/r", switcher=[{"name": "m1", "group": "Money"}])
    return {"overview": (overview, "healthnote"), "module shell": (shell, "hlbl")}


def _run(tmp_path, page, responses, steps):
    (tmp_path / "page.html").write_text(page)
    (tmp_path / "sc.json").write_text(json.dumps({"responses": responses, "steps": steps}))
    p = subprocess.run(["node", str(ROOT / "tests" / "_dashboard_js_harness.js"), str(tmp_path / "page.html"), str(tmp_path / "sc.json")],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr[-600:]
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert "error" not in out, out
    return out


@needs_node
def test_node_is_available_when_it_is_required():
    if _REQUIRED:
        assert _HAS_NODE, "AGENTIC_OS_REQUIRE_NODE_TESTS=1 but node is not installed: the dashboard behaviour tests would silently not run"


@needs_node
@pytest.mark.parametrize("which", ["overview", "module shell"])
@pytest.mark.parametrize("status,words", [(401, "credential"), (503, "no fleet credential configured")])
def test_after_the_first_refusal_the_page_stops_polling_and_says_so_once(tmp_path, which, status, words):
    page, note = _pages()[which]
    out = _run(tmp_path, page, {"/api/fleet": {"status": status, "body": {"detail": BODY}}}, [{"tick": 1}, {"tick": 1}, {"tick": 1}])
    assert out["calls"] and all(c["url"] == "/api/fleet" for c in out["calls"])
    assert len(out["calls"]) == 1, f"{len(out['calls'])} requests: polling must stop after the first refusal"
    assert out["intervals"] == 0
    line = out["snapshots"][-1][note]["text"]
    assert words in line, line
    assert BODY not in json.dumps(out) and out["console"] == [] and out["storageTouched"] is False
    assert all(c["headers"] == {} for c in out["calls"]), "these pages hold no credential and send none"


@needs_node
@pytest.mark.parametrize("which", ["overview", "module shell"])
def test_behind_an_authenticating_proxy_the_first_request_succeeds_and_polling_continues(tmp_path, which):
    page, note = _pages()[which]
    out = _run(tmp_path, page, {"/api/fleet": {"status": 200, "body": FLEET}}, [{"tick": 1}, {"tick": 1}])
    assert len(out["calls"]) == 3 and out["intervals"] == 1
    assert "credential" not in out["snapshots"][-1][note]["text"]


@needs_node
def test_a_server_error_does_not_stop_the_polling(tmp_path):
    page, _ = _pages()["overview"]
    out = _run(tmp_path, page, {"/api/fleet": {"status": 500, "body": {}}}, [{"tick": 1}, {"tick": 1}])
    assert len(out["calls"]) == 3 and out["intervals"] == 1

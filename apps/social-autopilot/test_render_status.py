"""A render, search, draft or recording that failed never reads as done, and nothing that is not waiting anywhere reads as queued."""
import importlib
import json
import time

import httpx
import pytest


@pytest.fixture
def vibex(monkeypatch):
    def load(live=True):
        for k in ("VIBEXGEN_API_URL", "VIBEXGEN_API_KEY", "VIBEXGEN_ALLOW_ANON", "VIBEXGEN_API_HOST"):
            monkeypatch.delenv(k, raising=False)
        if live:
            monkeypatch.setenv("VIBEXGEN_API_URL", "http://vibexgen.test")
            monkeypatch.setenv("VIBEXGEN_API_KEY", "k" * 20)
        import sys
        from pathlib import Path
        monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
        sys.modules.pop("vibex", None)
        return importlib.import_module("vibex")
    return load


def answering(monkeypatch, mod, outcome):
    def post(url, **kw):
        if isinstance(outcome, Exception):
            raise outcome
        status, body = outcome
        return httpx.Response(status, content=body if isinstance(body, bytes) else json.dumps(body).encode(), request=httpx.Request("POST", url))
    monkeypatch.setattr(mod.httpx, "post", post)


@pytest.mark.parametrize("outcome,state", [
    ((200, {"success": True, "operation": {"tool": "trim"}, "response": "done"}), "processed"),
    ((200, {"success": True, "task_id": "t-1"}), "submitted"),                    # accepted is not finished
    ((200, {"success": False, "response": "could not render"}), "failed"),
    ((200, {"response": "no verdict at all"}), "failed"),
    ((500, {"detail": "boom"}), "failed"),
    ((401, {"detail": "bad key"}), "failed"),
    (httpx.ConnectError("refused"), "failed"),                                     # nothing was started
    (httpx.ReadTimeout("slow"), "unknown"),                                        # it may have started
    ((200, b"<html>gateway</html>"), "unknown"),
])
def test_a_live_render_reports_only_what_vibexgen_said(vibex, monkeypatch, outcome, state):
    mod = vibex()
    answering(monkeypatch, mod, outcome)
    out = mod.generate("make a clip")
    assert out["status"] == state and out["source"] == "vibexgen" and out["status"] in mod.GENERATE_STATES, out
    assert "queued" not in json.dumps(out).lower()
    if state in ("failed", "unknown"):
        assert out["error"]


def test_when_not_connected_nothing_is_said_to_be_waiting(vibex, monkeypatch):
    mod = vibex(live=False)
    monkeypatch.setattr(mod.httpx, "post", lambda *a, **k: pytest.fail("nothing may be sent"))
    out = mod.generate("make a clip")
    assert out["status"] == "not_connected" and "nothing was queued or rendered" in out["note"]


@pytest.mark.parametrize("outcome", [(503, {}), httpx.ReadTimeout("slow"), (200, {"result": []}), (200, b"not json")])
def test_a_live_search_that_fails_returns_no_sample_data(vibex, monkeypatch, outcome):
    mod = vibex()
    answering(monkeypatch, mod, outcome)
    out = mod.trends("tax planning")
    assert (out["source"], out["status"], out["angles"]) == ("vibexgen", "unavailable", []) and out["error"]


def test_samples_are_labelled_as_samples(vibex):
    out = vibex(live=False).trends()
    assert (out["source"], out["status"]) == ("demo", "sample") and out["angles"] and "not a search" in out["note"]


def test_the_reel_never_reads_queued_or_recording_when_it_is_not(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    app_mod = importlib.import_module("social-autopilot.app")
    monkeypatch.setattr(app_mod, "_REEL_STATUS", tmp_path / "status.json")
    (tmp_path / "status.json").write_text(json.dumps({"status": "recording", "ts": time.time()}))
    assert app_mod._reel_status_read()["status"] == "recording"
    (tmp_path / "status.json").write_text(json.dumps({"status": "recording", "ts": time.time() - 3600}))
    stale = app_mod._reel_status_read()
    assert stale["status"] == "error" and "did not report back" in stale["error"]
    client = TestClient(app_mod.app)
    for result, said in (({"status": "error", "error": "failed to stage draft: Postiz API unreachable"}, "failed"),
                         ({"status": "done", "staged_as_draft": False}, "failed"),
                         ({"status": "done", "staged_as_draft": True}, "drafted")):
        monkeypatch.setattr(app_mod, "_draft", lambda body, r=result: r)
        assert client.post("/api/reel/schedule").json()["status"] == said, result

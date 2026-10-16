"""Hosted Connect sessions: authenticated start, bounded and expiring state, escaped callback output."""
import itertools

import pytest
from fastapi.testclient import TestClient

import agentic_os.projects_api as pa
from agentic_os.integrations.connect import ConnectOutcome
from agentic_os.integrations.hosted_oauth import HostedConnect

_n = itertools.count()


class _Session:
    detail = ""

    def start(self):
        n = next(_n)
        return {"authorize_url": f"https://idp.example/auth?state=s{n}", "state": f"s{n}"}

    def complete(self, code, state):
        if code == "bad":
            return ConnectOutcome("google", "NOT_CONNECTED", False, detail="<b>oops</b>")
        return ConnectOutcome("google", "VERIFIED_READ", True, credential_ref="ref-" + code)


@pytest.fixture()
def hosted(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "tok")
    h = HostedConnect(session_factory=lambda p: _Session(), providers=("google",))
    monkeypatch.setattr(pa, "_HOSTED", h)
    monkeypatch.setattr(pa, "_HOSTED_TRIED", True)
    monkeypatch.setattr(pa, "_HOSTED_BROKEN", False)
    return h, TestClient(pa.create_app())


def test_start_requires_control_token(hosted):
    h, c = hosted
    assert c.post("/api/apps/google/connect/start").status_code == 401
    assert c.post("/api/apps/google/connect/start", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert h._sessions == {}
    r = c.post("/api/apps/google/connect/start", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200 and r.json()["hosted"] is True


def test_callback_needs_a_state_from_an_authenticated_start(hosted):
    h, c = hosted
    assert c.get("/api/apps/connect/callback", params={"code": "x", "state": "s999"}).status_code == 400
    assert h.connected_providers() == ()
    st = c.post("/api/apps/google/connect/start", headers={"Authorization": "Bearer tok"}).json()["state"]
    assert c.get("/api/apps/connect/callback", params={"code": "x", "state": st}).status_code == 200
    assert h.connected_providers() == ("google",)
    # one-shot
    assert c.get("/api/apps/connect/callback", params={"code": "y", "state": st}).status_code == 400


def test_pending_sessions_are_bounded_and_expire():
    now = [0.0]
    h = HostedConnect(session_factory=lambda p: _Session(), providers=("google",), max_pending=3, ttl_s=10,
                      clock=lambda: now[0])
    states = [h.start("google")["state"] for _ in range(3)]
    with pytest.raises(RuntimeError):
        h.start("google")
    now[0] = 11.0
    assert h.start("google")["state"]
    assert len(h._sessions) == 1
    assert h.callback("x", states[0]).connected is False


def test_callback_output_is_escaped(hosted):
    h, c = hosted
    h.session_factory = lambda p: type("S", (_Session,), {})()
    st = c.post("/api/apps/google/connect/start", headers={"Authorization": "Bearer tok"}).json()["state"]
    r = c.get("/api/apps/connect/callback", params={"code": "bad", "state": st})
    assert "<b>oops</b>" not in r.text and "&lt;b&gt;oops&lt;/b&gt;" in r.text


def test_configured_but_broken_does_not_fall_back_to_simulated(monkeypatch):
    monkeypatch.setattr(pa, "_HOSTED", None)
    monkeypatch.setattr(pa, "_HOSTED_TRIED", True)
    monkeypatch.setattr(pa, "_HOSTED_BROKEN", True)
    r = TestClient(pa.create_app()).post("/api/apps/google/connect/start")
    assert r.status_code == 503

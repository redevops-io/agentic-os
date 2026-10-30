"""The Agent Gateway mount on the Projects API: constant-time bearer comparison and the optional durable audit sink.

Synthetic token and data only. The default (no AGENT_GATEWAY_AUDIT_PATH) keeps the in-memory sink and writes no file.
"""
from __future__ import annotations

import hmac
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentic_os.agent_gateway import EventStoreAuditSink, InMemoryAuditSink
from agentic_os.mission.store import EventStore, verify_chain
from agentic_os.projects_api import SampleProjectionProvider, create_app

TOKEN = "agw-synthetic-token-for-tests-only"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SRC = Path(__file__).resolve().parents[1] / "agentic_os" / "projects_api.py"


def _client(monkeypatch, audit_path=None):
    monkeypatch.setenv("AGENT_GATEWAY_TOKEN", TOKEN)
    if audit_path is None:
        monkeypatch.delenv("AGENT_GATEWAY_AUDIT_PATH", raising=False)
    else:
        monkeypatch.setenv("AGENT_GATEWAY_AUDIT_PATH", str(audit_path))
    return TestClient(create_app(SampleProjectionProvider()))


# ── constant-time comparison ────────────────────────────────────────────────────────────────────────────
def test_the_bearer_is_compared_with_hmac_compare_digest(monkeypatch):
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    c = _client(monkeypatch)
    assert c.get("/api/agent-gateway/capabilities", headers=AUTH).status_code == 200
    assert c.get("/api/agent-gateway/capabilities",
                 headers={"Authorization": "Bearer " + "x" * len(TOKEN)}).status_code == 401
    # both the right and the wrong (same-length) bearer went through the constant-time comparison
    assert len(calls) >= 2 and all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in calls)


def test_the_configured_token_is_not_compared_with_equality_in_the_mount():
    src = SRC.read_text()
    start = src.index("def mount_agent_gateway")
    body = src[start:src.index("@dataclass", start)]
    assert "t == token" not in body and "== token" not in body and "token ==" not in body


@pytest.mark.parametrize("bad", ["Bearer " + TOKEN[:-1], "Bearer " + TOKEN + "x", "Bearer " + TOKEN.upper(),
                                 "Bearer ", "Bearer", "", "Basic " + TOKEN])
def test_near_miss_and_malformed_credentials_are_401(monkeypatch, bad):
    c = _client(monkeypatch)
    assert c.get("/api/agent-gateway/capabilities", headers={"Authorization": bad}).status_code == 401


def test_a_non_ascii_credential_is_401_not_a_server_error(monkeypatch):
    c = _client(monkeypatch)
    r = c.get("/api/agent-gateway/capabilities", headers={"Authorization": "Bearer tést".encode("latin-1")})
    assert r.status_code == 401


def test_the_right_token_still_works(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/api/agent-gateway/capabilities/projects.list/invoke", headers=AUTH, json={})
    assert r.status_code == 200 and r.json()["status"] == "ok"


# ── durable audit sink (optional) ───────────────────────────────────────────────────────────────────────
def test_default_audit_is_in_memory_and_writes_no_file(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    c = _client(monkeypatch)
    assert c.post("/api/agent-gateway/capabilities/projects.list/invoke", headers=AUTH, json={}).status_code == 200
    assert list(tmp_path.iterdir()) == []


def test_audit_path_persists_each_call_across_a_restart(monkeypatch, tmp_path):
    path = tmp_path / "gw-audit.jsonl"
    c = _client(monkeypatch, path)
    assert c.post("/api/agent-gateway/capabilities/projects.list/invoke", headers=AUTH, json={}).status_code == 200
    denied = c.post("/api/agent-gateway/capabilities/crm.request_outreach_send/invoke", headers=AUTH, json={"contact": "x"})
    assert denied.json()["status"] == "denied"

    # a new process: the records are still there
    store = EventStore(str(path))
    events = store.all()
    assert [e.type for e in events] == ["agent_gateway.call", "agent_gateway.call"]
    first = events[0].payload
    assert first["capability"] == "projects.list" and first["tenant"] == "demo" and first["status"] == "ok"
    assert first["client_id"] == "external-agent" and first["allowed"] is True
    assert events[1].payload["capability"] == "crm.request_outreach_send" and events[1].payload["allowed"] is False
    assert verify_chain(events).ok and verify_chain(events).checked == 2
    # the credential is not in the audit log
    assert TOKEN not in path.read_text()

    # restart appends to the same chain
    c2 = _client(monkeypatch, path)
    assert c2.get("/api/agent-gateway/capabilities", headers=AUTH).status_code == 200
    c2.post("/api/agent-gateway/capabilities/projects.list/invoke", headers=AUTH, json={})
    assert len(EventStore(str(path)).all()) == 3
    assert verify_chain(EventStore(str(path)).all()).ok


def test_a_tampered_audit_record_is_detected(monkeypatch, tmp_path):
    path = tmp_path / "gw-audit.jsonl"
    c = _client(monkeypatch, path)
    c.post("/api/agent-gateway/capabilities/projects.list/invoke", headers=AUTH, json={})
    c.post("/api/agent-gateway/capabilities/projects.list/invoke", headers=AUTH, json={})
    lines = path.read_text().splitlines()
    rec = json.loads(lines[0])
    rec["payload"]["tenant"] = "other"
    path.write_text("\n".join([json.dumps(rec)] + lines[1:]) + "\n")
    assert not verify_chain(EventStore(str(path)).all()).ok


def test_unauthenticated_calls_are_not_audited(monkeypatch, tmp_path):
    path = tmp_path / "gw-audit.jsonl"
    c = _client(monkeypatch, path)
    assert c.get("/api/agent-gateway/capabilities").status_code == 401
    assert EventStore(str(path)).all() == []


def test_an_unwritable_audit_path_stops_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_GATEWAY_TOKEN", TOKEN)
    monkeypatch.setenv("AGENT_GATEWAY_AUDIT_PATH", str(tmp_path / "missing-dir" / "audit.jsonl"))
    with pytest.raises(OSError):
        create_app(SampleProjectionProvider())


def test_sink_unit(tmp_path):
    from agentic_os.agent_gateway import (AuditEvent, EgressAction, GatewayDecision, GatewayStatus, RiskTier)
    sink = EventStoreAuditSink.from_path(str(tmp_path / "a.jsonl"))
    ev = AuditEvent(request_id="r1", capability="projects.list", client_id="c", subject="s", tenant="t",
                    workspace="w", status=GatewayStatus.OK,
                    decision=GatewayDecision(allowed=True, reason="ok", risk_tier=RiskTier.READ,
                                             egress_action=EgressAction.ALLOW))
    assert sink.record(ev) == "audit_1"
    assert sink.events()[0].payload["request_id"] == "r1"
    assert not isinstance(sink, InMemoryAuditSink)

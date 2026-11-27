"""A governed write needs two separate things: the requesting agent's own stored authority (mission-authority/v1, given
when a person connected it: scoped, expiring, revocable) and a person's approval. Neither replaces the other."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("httpx")

from agentic_os import app_gate  # noqa: E402
from agentic_os.agent_gateway.agent_tokens import envelope_key, envelope_key_for_requester, record_authority  # noqa: E402
from agentic_os.governed_writes.service import GovernedWrites, WriteError  # noqa: E402
from agentic_os.mission.authority import SqliteEnvelopeStore  # noqa: E402
from tests.test_governed_writes import COMMENT, World, _changes, _env, connect  # noqa: E402,F401


def store(tmp_path):
    return SqliteEnvelopeStore(str(tmp_path / "authority.db"))


def revoke(tmp_path, agent_id="a1"):
    store(tmp_path).revoke(envelope_key(agent_id))


def denied(call):
    with pytest.raises(WriteError) as e:
        call()
    assert e.value.code == "denied" and "no_authority" in e.value.detail, (e.value.code, e.value.detail)
    return e.value.detail


def test_a_requester_name_maps_to_the_key_its_authority_was_recorded_under():
    assert envelope_key_for_requester("agent:conn-1") == envelope_key("conn-1")
    assert envelope_key_for_requester("agent-0a1b") == envelope_key("agent-0a1b")       # connected in the installed app
    assert envelope_key_for_requester("external-agent") == envelope_key("external-agent")   # nobody records one


def test_the_runtime_runs_a_write_under_the_requesting_agents_envelope(tmp_path):
    w = World(tmp_path)
    rid = w.ask()["request_id"]
    mission = w.writes.runtime._missions[rid]
    assert w.writes.runtime.authority_key(mission) == envelope_key("a1")


def test_an_agent_nobody_gave_authority_cannot_even_ask(tmp_path):
    w = World(tmp_path, agents=())
    assert "holds no authority" in denied(lambda: w.ask())
    assert w.writes.list() == [] and store(tmp_path).get(envelope_key("a1")) is None


def test_authority_covers_only_the_grants_it_was_given_with(tmp_path):
    w = World(tmp_path, agents=())
    connect("a1", grants=["social.propose", "erp.read"])
    assert "does not cover" in denied(lambda: w.ask())
    assert w.writes.list() == []


def test_expired_authority_refuses(tmp_path):
    w = World(tmp_path, agents=())
    now = time.time()
    record_authority(str(tmp_path / "authority.db"), "a1", ["erp.propose"], tenant="acme", issued_at=now - 100, expires_at=now - 1)
    assert "expired" in denied(lambda: w.ask())


def test_authority_is_for_one_tenant(tmp_path):
    w = World(tmp_path, agents=())
    connect("a1", tenant="globex")
    denied(lambda: w.ask(tenant="acme"))
    assert w.writes.list() == []


def test_revoking_the_agent_stops_its_waiting_requests_approval_or_not(tmp_path):
    w = World(tmp_path)
    rid = w.ask()["request_id"]
    revoke(tmp_path)
    shown = w.writes.get(rid, tenant="acme", for_approver=True)
    assert shown["authorization"]["verdict"] == "denied" and "revoked" in shown["authorization"]["reason"]   # the person sees why
    assert "revoked" in denied(lambda: w.approve(rid))
    assert _changes(w.apps) == [] and w.writes.get(rid)["state"] == "pending"
    assert w.approve(rid, approve=False)["state"] == "rejected"                          # a person can still close it


def test_revocation_between_the_approval_and_the_change_is_seen_at_the_last_moment(tmp_path, monkeypatch):
    w = World(tmp_path)
    rid = w.ask()["request_id"]

    def revoke_just_after_the_decision_was_checked(*a, **k):
        revoke(tmp_path)
        return None
    monkeypatch.setattr(app_gate, "operator_denial", revoke_just_after_the_decision_was_checked)
    done = w.approve(rid)
    assert done["state"] == "failed" and "revoked" in done["note"] and "nothing was changed" in done["note"], done
    assert _changes(w.apps) == []


def test_revocation_holds_after_a_restart(tmp_path):
    w = World(tmp_path)
    rid = w.ask()["request_id"]
    revoke(tmp_path)
    again = w.start()
    denied(lambda: w.approve(rid, writes=again))
    assert _changes(w.apps) == []


def test_developer_mode_cannot_run_a_revoked_agents_change(tmp_path, monkeypatch):
    from agentic_os.governed_writes import preauthorization as P
    monkeypatch.setenv(P.ENV_ENABLED, "1")
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", '{"acme": {"policy_refs": ["app:*", "writes:decide", "writes:preauthorize"]}}')
    w = World(tmp_path)
    w.writes.preauthorize(tenant="acme", requester="agent:a1", actions=["erp.request_comment"], max_uses=3, minutes=60,
                          created_by="approver:acme", reason="trying the flow")
    revoke(tmp_path)
    denied(lambda: w.ask())
    assert _changes(w.apps) == []


def test_without_authority_records_nothing_can_be_changed_whatever_the_profile(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_AUTHORITY_DB")
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "development")
    from tests._writes_fakes import FakeApps
    apps = FakeApps()
    writes = GovernedWrites(tmp_path / "writes", apps.writers())
    why = denied(lambda: writes.request(tenant="acme", requester="agent:a1", action="erp.request_comment", inputs=COMMENT,
                                        idempotency_key="key-00000001"))
    assert "AGENTIC_OS_AUTHORITY_DB" in why and apps.posts == [] and writes.list() == []


def test_an_approval_creates_no_authority(tmp_path):
    w = World(tmp_path)
    rid = w.ask()["request_id"]
    before = store(tmp_path).get(envelope_key("a1")).digest()
    assert w.approve(rid)["state"] == "executed_verified"
    assert store(tmp_path).get(envelope_key("a1")).digest() == before and store(tmp_path).get(rid) is None


def test_over_http_a_revoked_token_leaves_its_pending_request_unapprovable(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from agentic_os.agent_gateway import agent_tokens as AT
    from agentic_os.governed_writes import preauthorization as P
    from tests.test_developer_mode import ACME, mounted
    monkeypatch.delenv(P.ENV_ENABLED, raising=False)
    monkeypatch.setenv("AGENTIC_OS_APPROVER_TOKENS", "acme=" + "A" * 40)
    client, apps, agent = mounted(monkeypatch, tmp_path)
    asked = client.post("/api/agent-gateway/capabilities/erp.request_comment/invoke", headers=agent,
                        json={**COMMENT, "idempotency_key": "revoke-0001"}).json()
    assert asked["status"] == "ok" and asked["output"]["state"] == "pending", asked
    rid = asked["output"]["request_id"]
    digest = client.get(f"/api/writes/{rid}", headers=ACME).json()["digest"]
    assert AT.revoke(str(tmp_path / "agents.json"), "dev1", authority_db=str(tmp_path / "authority.db"))
    refused = client.post(f"/api/writes/{rid}/decision", headers=ACME, json={"approved": True, "digest": digest})
    assert refused.status_code == 403 and "revoked" in refused.text, (refused.status_code, refused.text)
    assert not apps.posts

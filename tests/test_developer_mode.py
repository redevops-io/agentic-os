"""Developer mode: a person preauthorizes one agent for a few low-consequence changes. It never covers a protected
change, never crosses tenants, never overrides a denial, and every use is on record."""
from __future__ import annotations

import json
import threading

import pytest

pytest.importorskip("httpx")

from agentic_os.governed_writes import preauthorization as P  # noqa: E402
from agentic_os.governed_writes.service import ACTIONS, WriteError  # noqa: E402
from tests.test_governed_writes import CASES, COMMENT, World, _changes, _env  # noqa: E402,F401

#: every action that exists is either eligible for developer mode or named here as never eligible: a new one must choose
NEVER = {"economics.request_payment_correction",      # changes the ledger, needs an attestation
         "media.request_generate",                    # spends on a model
         "market.request_change_plan",                # records a person's judgement about what to change
         "apps.request_install"}                      # installs an application: what it may use is a person's decision
LIMITS = {"acme": {"policy_refs": ["app:*", "writes:decide", "writes:preauthorize"]},
          "globex": {"policy_refs": ["app:*", "writes:decide", "writes:preauthorize"]},
          "plain": {"policy_refs": ["app:*", "writes:decide"]}}
GRANT = dict(tenant="acme", requester="agent:a1", actions=["erp.request_comment"], max_uses=3, minutes=60,
             created_by="approver:acme", reason="building the follow-up flow")


@pytest.fixture(autouse=True)
def _developer(monkeypatch, _env):
    monkeypatch.setenv(P.ENV_ENABLED, "1")
    monkeypatch.setenv("AGENTIC_OS_APPROVER_TOKENS", "acme=" + "A" * 40 + ",globex=" + "G" * 40 + ",plain=" + "P" * 40)
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps(LIMITS))


def test_every_action_is_either_eligible_or_named_as_never():
    assert set(ACTIONS) == set(P.ELIGIBLE) | NEVER and not set(P.ELIGIBLE) & NEVER


def test_without_a_preauthorization_nothing_changes_by_itself(tmp_path):
    w = World(tmp_path)
    assert w.ask()["state"] == "pending" and not _changes(w.apps)


def test_a_covered_change_runs_once_through_the_same_decision_and_is_on_record(tmp_path):
    w = World(tmp_path)
    grant = w.writes.preauthorize(**GRANT)
    assert grant["state"] == "active" and grant["granted_by"] == "approver:acme"
    done = w.ask()
    assert done["state"] == "executed_verified" and done["decided_by"] == f"preauthorization:{grant['id']} (granted by approver:acme)", done
    written = len(_changes(w.apps))
    assert written >= 1 and w.ask()["replayed"] is True and len(_changes(w.apps)) == written          # a retry writes nothing more
    after = w.writes.preauthorizations.get(grant["id"])
    assert after["uses"] == 1 and after["used_for"] == [{"request_id": done["request_id"], "at": w.now[0], "outcome": "executed_verified"}]
    approved = next(e for e in w.writes.runtime.store.for_mission(done["request_id"]) if e.type == "ApprovalGranted")
    assert grant["id"] in approved.payload["approver"] and approved.payload["capability"] == "erp.request_comment"


def test_the_uses_run_out_and_the_next_waits_for_a_person(tmp_path):
    w = World(tmp_path)
    w.writes.preauthorize(**{**GRANT, "max_uses": 2})
    states = [w.ask(key=f"key-{n:08d}", inputs={**COMMENT, "text": f"note {n}"})["state"] for n in range(4)]
    assert states == ["executed_verified", "executed_verified", "pending", "pending"], states


def test_many_requests_at_once_cannot_take_more_uses_than_granted(tmp_path):
    w = World(tmp_path)
    w.writes.preauthorize(**{**GRANT, "max_uses": 3})
    results = []
    threads = [threading.Thread(target=lambda n=n: results.append(w.ask(key=f"key-{n:08d}", inputs={**COMMENT, "text": f"n{n}"})["state"]))
               for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results).count("executed_verified") == 3 and results.count("pending") == 5, results


@pytest.mark.parametrize("change", ["expired", "revoked", "other agent", "other tenant", "other action", "mode off", "tenant lost the ref",
                                    "tenant lost deciding"])
def test_anything_outside_the_grant_waits_for_a_person(tmp_path, monkeypatch, change):
    w = World(tmp_path)
    grant = w.writes.preauthorize(**GRANT)
    ask = {}
    if change == "expired":
        w.now[0] += 3601
    elif change == "revoked":
        assert w.writes.preauthorizations.revoke(grant["id"], by="approver:acme", tenant="acme")["state"] == "revoked"
    elif change == "other agent":
        ask = {"requester": "agent:a2"}
    elif change == "other tenant":
        ask = {"tenant": "globex"}
    elif change == "other action":
        ask = {"action": "support.request_private_note"}
    elif change == "mode off":
        monkeypatch.delenv(P.ENV_ENABLED)
    else:
        refs = ["app:*", "writes:decide"] if change == "tenant lost the ref" else ["app:*", "writes:preauthorize"]
        monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({**LIMITS, "acme": {"policy_refs": refs}}))
    if change == "tenant lost deciding":               # then nobody could decide it, so nothing is even asked for
        with pytest.raises(WriteError) as refused:
            w.ask(**ask)
        assert refused.value.code == "denied" and not _changes(w.apps)
    else:
        out = w.ask(**ask)
        assert out["state"] == "pending" and not _changes(w.apps), (change, out)
    assert w.writes.preauthorizations.get(grant["id"])["uses"] == 0


def test_a_match_limits_the_inputs_it_covers(tmp_path):
    w = World(tmp_path)
    w.writes.preauthorize(**{**GRANT, "match": {"name": ["SINV-1"]}})
    assert w.ask(inputs={**COMMENT, "name": "SINV-2"}, key="key-00000002")["state"] == "pending"
    assert w.ask()["state"] == "executed_verified"


def test_a_denial_is_never_overridden_and_the_use_is_given_back(tmp_path, monkeypatch):
    w = World(tmp_path)
    grant = w.writes.preauthorize(**GRANT)
    from agentic_os import app_gate
    monkeypatch.setattr(app_gate, "operator_denial", lambda *a, **k: "the deciding identity may not execute this")
    out = w.ask()
    assert out["state"] == "pending" and not _changes(w.apps), out
    after = w.writes.preauthorizations.get(grant["id"])
    assert after["uses"] == 0 and "not decided: denied" in after["used_for"][0]["outcome"]


def test_an_application_taken_from_the_tenant_stops_the_proposal_whatever_was_preauthorized(tmp_path, monkeypatch):
    w = World(tmp_path)
    w.writes.preauthorize(**GRANT)
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({"acme": {"policy_refs": ["writes:decide", "writes:preauthorize"]}}))
    with pytest.raises(WriteError) as refused:
        w.ask()
    assert refused.value.code == "denied" and not _changes(w.apps)


@pytest.mark.parametrize("fields,why", [
    ({"actions": ["economics.request_payment_correction"]}, "can never be preauthorized"),
    ({"actions": ["erp.request_comment", "media.request_generate"]}, "can never be preauthorized"),
    ({"actions": ["market.request_change_plan"]}, "can never be preauthorized"),
    ({"actions": ["*"]}, "can never be preauthorized"),
    ({"actions": []}, "name at least one"),
    ({"max_uses": 0}, "uses must be"), ({"max_uses": P.MAX_USES + 1}, "uses must be"), ({"max_uses": True}, "uses must be"),
    ({"minutes": 0}, "minutes must be"), ({"minutes": P.MAX_TTL_S // 60 + 1}, "minutes must be"),
    ({"requester": "approver:acme"}, "the one agent"), ({"requester": "*"}, "the one agent"),
    ({"created_by": "agent:a1"}, "only a person"), ({"created_by": ""}, "only a person"),
    ({"created_by": "preauthorization:pre_x"}, "only a person"),
    ({"reason": " "}, "say why"), ({"match": {"name": []}}, "match maps"), ({"match": {"name": [{"x": 1}]}}, "match maps"),
    ({"tenant": "plain"}, "developer mode is not on"),
])
def test_what_a_preauthorization_may_not_be(tmp_path, fields, why):
    with pytest.raises(P.PreauthorizationError, match=why):
        World(tmp_path).writes.preauthorize(**{**GRANT, **fields})


def test_a_record_widened_by_hand_still_cannot_cover_a_protected_change(tmp_path):
    w = World(tmp_path)
    grant = w.writes.preauthorize(**GRANT)
    w.writes._db.execute("UPDATE preauthorizations SET actions=? WHERE id=?",
                         (json.dumps(["media.request_generate", "social.request_post", "economics.request_payment_correction"]), grant["id"]))
    for action, inputs in (("media.request_generate", {"kind": "image", "prompt": "x"}),
                           ("social.request_post", {"channel_id": "c", "caption": "x", "when": "2027-01-01T10:00:00Z"}),
                           ("economics.request_payment_correction", {"reference": "r"})):
        assert w.writes.preauthorizations.claim(tenant="acme", requester="agent:a1", action=action, inputs=inputs) is None, action
    assert w.writes.preauthorizations.claim(tenant="acme", requester="agent:a1", action="social.request_post",
                                            inputs={"when": "draft"}) is not None


def test_what_needs_an_attestation_is_never_decided_by_it(tmp_path):
    w = World(tmp_path)
    waiting = w.ask()                                    # a real pending request, asked before anything was preauthorized
    shown = w.writes.get(waiting["request_id"], tenant="acme", for_approver=True)
    grant = w.writes.preauthorize(**GRANT)
    w.writes._try_preauthorized(waiting["request_id"], tenant="acme", requester="agent:a1", action="erp.request_comment", args=COMMENT,
                                evidence={"attestation": "I checked"}, digest=shown["digest"])
    after = w.writes.preauthorizations.get(grant["id"])
    assert after["uses"] == 0 and after["used_for"] == [] and not _changes(w.apps)
    assert w.writes.get(waiting["request_id"], tenant="acme")["state"] == "pending"
    w.writes._try_preauthorized(waiting["request_id"], tenant="acme", requester="agent:a1", action="erp.request_comment", args=COMMENT,
                                evidence=None, digest=shown["digest"])            # the same call without the attestation decides it
    assert w.writes.get(waiting["request_id"], tenant="acme")["state"] == "executed_verified"


def test_it_survives_a_restart(tmp_path):
    w = World(tmp_path)
    grant = w.writes.preauthorize(**{**GRANT, "max_uses": 2})
    w.ask()
    w.writes = w.start()
    assert w.ask(key="key-00000002", inputs={**COMMENT, "text": "two"})["state"] == "executed_verified"
    assert w.writes.preauthorizations.get(grant["id"])["state"] == "used up"


# ── the routes: only a person, only their own tenant ─────────────────────────────
def mounted(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from agentic_os.agent_gateway import agent_tokens as AT
    from agentic_os.governed_writes import apps as apps_mod
    from agentic_os.projects_api import SampleProjectionProvider, create_app
    from tests._writes_fakes import FakeApps
    apps = FakeApps()
    monkeypatch.setattr(apps_mod, "writers_from_env", lambda: apps.writers())
    agents = str(tmp_path / "agents.json")
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "index.html").write_text("<html></html>")
    monkeypatch.setenv("AGENT_GATEWAY_AGENTS_FILE", agents)
    monkeypatch.setenv("AGENTIC_OS_WRITES_DIR", str(tmp_path / "writes"))
    monkeypatch.setenv("PROJECTS_UI_DIR", str(tmp_path / "ui"))
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", "acme=" + "Q" * 40)            # a requester: sees, never decides
    client = TestClient(create_app(SampleProjectionProvider()), raise_server_exceptions=False)
    token = AT.issue(agents, "dev1", ["erp.read", "erp.propose"], minutes=10, tenant="acme", authority_db=str(tmp_path / "authority.db"))
    return client, apps, {"Authorization": "Bearer " + token}


BODY = {"agent": "agent:dev1", "actions": ["erp.request_comment"], "max_uses": 2, "minutes": 30, "reason": "trying the flow"}
ACME, GLOBEX = {"Authorization": "Bearer " + "A" * 40}, {"Authorization": "Bearer " + "G" * 40}


def test_only_a_person_of_that_tenant_creates_sees_and_revokes_over_http(monkeypatch, tmp_path):
    client, apps, agent = mounted(monkeypatch, tmp_path)
    url = "/api/writes/preauthorizations"
    requester = {"Authorization": "Bearer " + "Q" * 40}
    assert client.get("/api/writes", headers=requester).status_code == 200        # the requester IS recognised, and may only look
    for who in (agent, {}, {"Authorization": "Bearer " + "Q" * 40}):
        assert client.post(url, headers=who, json=BODY).status_code in (401, 403), who
        assert client.get(url, headers=who).status_code in (401, 403), who
    assert client.post(url, headers=GLOBEX, json={**BODY, "tenant": "acme"}).status_code == 403
    assert client.post(url, headers=ACME, json={**BODY, "tenant": "globex"}).status_code == 403
    assert client.post(url, headers=ACME, json={**BODY, "actions": ["media.request_generate"]}).status_code == 422
    assert client.post(url, headers=ACME, json={**BODY, "created_by": "approver:someone"}).status_code == 422
    made = client.post(url, headers=ACME, json=BODY)
    assert made.status_code == 200 and made.json()["tenant"] == "acme" and made.json()["granted_by"] == "approver:acme", made.text
    ident = made.json()["id"]
    assert client.get(url, headers=GLOBEX).json()["preauthorizations"] == []
    assert client.post(f"{url}/{ident}/revoke", headers=GLOBEX).status_code == 404
    assert client.post(f"{url}/{ident}/revoke", headers=agent).status_code in (401, 403)

    asked = client.post("/api/agent-gateway/capabilities/erp.request_comment/invoke", headers=agent,
                        json={**COMMENT, "idempotency_key": "dev-mode-0001"}).json()
    assert asked["status"] == "ok" and asked["output"]["state"] == "executed_verified", asked
    assert ident in asked["output"]["decided_by"] and apps.posts
    shown = client.get(url, headers=ACME).json()["preauthorizations"][0]
    assert shown["uses"] == 1 and shown["used_for"][0]["request_id"] == asked["output"]["request_id"]

    assert client.post(f"{url}/{ident}/revoke", headers=ACME).json()["state"] == "revoked"
    again = client.post("/api/agent-gateway/capabilities/erp.request_comment/invoke", headers=agent,
                        json={**COMMENT, "text": "second", "idempotency_key": "dev-mode-0002"}).json()
    assert again["output"]["state"] == "pending", again

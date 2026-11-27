"""CRM, accounting, procurement and support through the mounted gateway, production profile with tenants: an external
agent retrieves records, proposes a change, a PERSON of its tenant approves it, and the agent verifies the result.
The applications are in-memory doubles behind the writers' own HTTP calls."""
import base64
import json
import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agentic_os.agent_gateway import agent_tokens as AT  # noqa: E402
from agentic_os.governed_writes import apps as apps_mod  # noqa: E402
from agentic_os.projects_api import SampleProjectionProvider, create_app  # noqa: E402
from tests._writes_fakes import FakeApps  # noqa: E402

BASE = "/api/agent-gateway/capabilities"
OPERATOR = {"Authorization": "Basic " + base64.b64encode(b"operator:" + b"p" * 24).decode()}
APPROVER = {"Authorization": "Bearer " + "A" * 40}
OTHER_APPROVER = {"Authorization": "Bearer " + "B" * 40}
REQUESTER = {"Authorization": "Bearer " + "R" * 40}
CLEAR = ("AGENT_GATEWAY_TOKEN", "AGENT_GATEWAY_AUDIT_PATH", "AGENT_GATEWAY_MCP", "PROJECTS_OPERATOR_GATE", "AGENTIC_OS_CONTROL_TOKEN",
         "AGENTIC_OS_TENANT_TOKENS", "AGENTIC_OS_APPROVER_TOKENS", "AGENTIC_OS_DASHBOARDS", "AGENTIC_OS_OAUTH_DB", "PROJECTS_CORS_ORIGINS",
         "AGENTIC_OS_ALLOW_UNAPPROVED_INVOKE", "AGENTIC_OS_PRODUCTS", "AGENTIC_OS_WRITES_DIR", "AGENTIC_OS_WRITES_TTL_S",
         "OPERATOR_BASIC_AUTH", "OUTREACH_BASIC_AUTH", "OPERATOR_BASIC_AUTH_USER", "OPERATOR_BASIC_AUTH_PASS", "AGENTIC_OS_PERMISSIONS",
         "CR_PERMISSIONS", "PERMISSIONS_KEY", "PERMISSIONS_DIR")
LIMITS = {"acme": {"policy_refs": ["app:erpnext", "app:chatwoot", "app:twenty", "writes:decide", "egress:financial"]},
          "globex": {"policy_refs": ["project:*", "writes:decide"]}}
ALL = ["erp.read", "erp.propose", "support.read", "support.propose", "crm.records.read", "crm.propose"]
INVOICE = {"doctype": "Sales Invoice", "name": "SINV-1"}


@pytest.fixture
def world(monkeypatch, tmp_path):
    for k in CLEAR:
        monkeypatch.delenv(k, raising=False)
    fake = FakeApps()
    monkeypatch.setattr(apps_mod, "writers_from_env", lambda: fake.writers())
    agents, db = str(tmp_path / "agents.json"), str(tmp_path / "authority.sqlite")
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<html></html>")
    env = {"AGENT_GATEWAY_AGENTS_FILE": agents, "AGENTIC_OS_PROFILE": "production", "AGENTIC_OS_AUTHORITY_DB": db,
           "AGENTIC_OS_APPROVAL_SECRET": "s" * 48, "AGENTIC_OS_REPLAY_DB": str(tmp_path / "replay.sqlite"),
           "AGENTIC_OS_TENANT_LIMITS": json.dumps(LIMITS), "AGENTIC_OS_WRITES_DIR": str(tmp_path / "writes"),
           "AGENTIC_OS_CONTROL_TOKEN": "C" * 40, "AGENTIC_OS_TENANT_TOKENS": "acme=" + "R" * 40,
           "AGENTIC_OS_APPROVER_TOKENS": "acme=" + "A" * 40 + ",globex=" + "B" * 40,
           "OPERATOR_BASIC_AUTH": "operator:" + "p" * 24, "PROJECTS_UI_DIR": str(ui)}
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    def start():
        return TestClient(create_app(SampleProjectionProvider()), raise_server_exceptions=False)
    count = {"n": 0}

    def agent(grants, tenant="acme"):
        count["n"] += 1
        return {"Authorization": "Bearer " + AT.issue(agents, f"agent-{count['n']}", grants, minutes=10, tenant=tenant, authority_db=db)}

    return start(), agent, fake, start, monkeypatch


def invoke(c, who, tool, /, **args):
    return c.post(f"{BASE}/{tool}/invoke", headers=who, json=args).json()


def offered(c, who):
    return {x["name"] for x in c.get(BASE, headers=who).json()["capabilities"]}


def decide(c, person, request_id, approved=True):
    shown = c.get(f"/api/writes/{request_id}", headers=person).json()
    return c.post(f"/api/writes/{request_id}/decision", headers=person, json={"approved": approved, "digest": shown["digest"]})


def test_procurement_retrieve_propose_approve_verify(world):
    c, agent, fake, *_ = world
    who = agent(ALL)
    suppliers = invoke(c, who, "erp.find_records", doctype="Supplier")
    assert suppliers["status"] == "ok" and suppliers["output"]["records"][0]["name"] == "Steelco", suppliers
    order = {"supplier": "Steelco", "schedule_date": "2026-12-01", "items": [{"item_code": "BOLT", "qty": 50}]}
    asked = invoke(c, who, "erp.request_draft_purchase_order", **order, idempotency_key="po-steelco-0001")
    assert asked["status"] == "ok" and asked["output"]["state"] == "pending" and asked["output"]["executed"] is False, asked
    rid = asked["output"]["request_id"]
    assert not fake.posts
    waiting = c.get("/api/writes?state=pending", headers=APPROVER).json()["requests"]
    assert [w["request_id"] for w in waiting] == [rid] and waiting[0]["inputs"] == order
    assert decide(c, APPROVER, rid).json()["state"] == "executed_verified"
    done = invoke(c, who, "erp.request_status", request_id=rid)["output"]
    assert done["state"] == "executed_verified" and done["decided_by"] == "approver:acme" and "inputs" not in done and "digest" not in done
    made = invoke(c, who, "erp.get_record", doctype="Supplier", name="Steelco")
    assert made["status"] == "ok"
    assert fake.docs[("Purchase Order", done["record"])]["docstatus"] == 0


def test_support_and_crm_retrieve_propose_approve_verify(world):
    c, agent, fake, *_ = world
    who = agent(ALL)
    conv = invoke(c, who, "support.get_conversation", conversation_id=7)["output"]
    assert conv["messages"][0]["text"] == "hello" and "jo@example.com" not in json.dumps(conv)
    rid = invoke(c, who, "support.request_private_note", conversation_id=7, text="Looking into it", idempotency_key="note-7-0001")["output"]["request_id"]
    assert decide(c, OPERATOR, rid).json()["state"] == "executed_verified"
    after = invoke(c, who, "support.get_conversation", conversation_id=7)["output"]["messages"]
    assert after[-1]["private_note"] is True and after[-1]["text"].startswith("Looking into it")
    rid = invoke(c, who, "crm.request_company", name="Newco", domain="newco.example", idempotency_key="co-newco-0001")["output"]["request_id"]
    assert decide(c, APPROVER, rid).json()["state"] == "executed_verified"
    found = invoke(c, who, "crm.find_companies", query="Newco")["output"]["companies"]
    assert found[0]["name"] == "Newco" and found[0]["domain"] == "newco.example"


def test_financial_records_leave_only_with_the_financial_grant(world):
    c, agent, *_ = world
    fake = world[2]
    reader = agent(["erp.read"])
    for tool, args in (("erp.get_record", INVOICE), ("erp.find_records", {"doctype": "Sales Invoice"})):
        without = invoke(c, reader, tool, **args)
        assert without["status"] == "error" and without["error"].startswith("not_authorized (missing_grant)") and "egress:financial" in without["error"], without
        assert "Acme" not in json.dumps(without)
    assert not fake.posts and invoke(c, reader, "erp.find_records", doctype="Supplier")["status"] == "ok"     # the rest is unaffected
    with_grant = invoke(c, agent(["erp.read", "egress:financial"]), "erp.get_record", **INVOICE)
    assert with_grant["status"] == "ok" and with_grant["output"]["record"]["customer"] == "Acme", with_grant


def test_redaction_keeps_record_ids_and_still_hides_what_it_should():
    from agentic_os.agent_gateway.egress import redact
    uid = "12345678-1234-4123-8123-123456789012"                  # mostly digits: the card pattern used to eat part of it
    assert redact({"company_id": uid}) == {"company_id": uid}
    out = redact(f"{uid} paid with 4111 1111 1111 1111, ask jo@example.com about {uid}")
    assert out.count(uid) == 2 and "4111" not in out and "jo@example.com" not in out
    assert "4111" not in redact("4111-1111-1111-1111")


def test_what_these_capabilities_can_change_is_pinned(world):
    """The only side effects the application grants add are the five PROPOSALS; no grant offers deciding or performing."""
    from agentic_os.agent_gateway import app_record_capabilities as records
    changing = {m.name for m in records.manifests() if m.side_effecting}
    assert changing == {"erp.request_comment", "erp.request_draft_purchase_order", "support.request_private_note",
                        "crm.request_company", "crm.request_person"}
    c, agent, fake, *_ = world
    names = offered(c, agent(ALL + ["egress:financial"]))
    assert not [n for n in names if re.search(r"decide|approve|execute|submit|delete|send", n)]
    assert changing <= names


def test_the_agent_cannot_decide_and_only_the_right_person_can(world):
    c, agent, fake, *_ = world
    who = agent(ALL)
    rid = invoke(c, who, "erp.request_comment", **INVOICE, text="please check", idempotency_key="comment-0001")["output"]["request_id"]
    digest = c.get(f"/api/writes/{rid}", headers=APPROVER).json()["digest"]
    body = {"approved": True, "digest": digest}
    assert not any("decide" in n or "approve" in n for n in offered(c, who))
    for person, codes in ((who, (401, 403)), ({}, (401, 403)), (OTHER_APPROVER, (404,)), (REQUESTER, (401, 403))):
        r = c.post(f"/api/writes/{rid}/decision", headers=person, json=body)
        assert r.status_code in codes, (person, r.status_code, r.text)
    assert c.get("/api/writes", headers=who).status_code == 401 and c.get(f"/api/writes/{rid}").status_code == 401
    assert c.get("/api/writes", headers=OTHER_APPROVER).json()["requests"] == []
    assert c.post(f"/api/writes/{rid}/decision", headers=APPROVER, json={**body, "approver": "someone"}).status_code == 422
    assert c.post(f"/api/writes/{rid}/decision", headers=APPROVER, json={**body, "digest": "0" * 64}).status_code == 409
    assert not fake.posts
    assert decide(c, APPROVER, rid, approved=False).json()["state"] == "rejected"
    assert decide_again(c, rid, digest).status_code == 409 and not fake.posts


def decide_again(c, rid, digest):
    return c.post(f"/api/writes/{rid}/decision", headers=APPROVER, json={"approved": True, "digest": digest})


def test_a_tenant_approver_without_the_decision_permission_decides_nothing(world):
    c, agent, fake, start, monkeypatch = world
    rid = invoke(c, agent(ALL), "erp.request_comment", **INVOICE, text="x", idempotency_key="comment-0001")["output"]["request_id"]
    digest = c.get(f"/api/writes/{rid}", headers=APPROVER).json()["digest"]
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({"acme": {"policy_refs": ["app:erpnext"]}}))
    assert c.post(f"/api/writes/{rid}/decision", headers=APPROVER, json={"approved": True, "digest": digest}).status_code == 403
    assert not fake.posts


def test_tenants_and_grants_bound_what_an_agent_sees_and_does(world):
    c, agent, fake, *_ = world
    reader = agent(["erp.read"])
    assert offered(c, reader) >= {"erp.find_records", "erp.get_record"}
    assert not {"erp.request_comment", "erp.request_status", "support.get_conversation", "crm.find_people"} & offered(c, reader)
    assert invoke(c, reader, "erp.request_comment", **INVOICE, text="x", idempotency_key="comment-0001")["status"] == "denied"
    outsider = agent(ALL, tenant="globex")                                  # granted by name, but the tenant has no application
    for name, args in (("erp.find_records", {"doctype": "Supplier"}), ("support.get_conversation", {"conversation_id": 7}),
                       ("erp.request_comment", {**INVOICE, "text": "x", "idempotency_key": "comment-0001"})):
        out = invoke(c, outsider, name, **args)
        assert out["status"] == "error" and "not granted" in out["error"], (name, out)
    mine, theirs = agent(ALL), agent(ALL)
    rid = invoke(c, mine, "erp.request_comment", **INVOICE, text="x", idempotency_key="comment-0002")["output"]["request_id"]
    assert invoke(c, theirs, "erp.request_status", request_id=rid)["status"] == "error"
    assert invoke(c, theirs, "erp.request_status")["output"]["requests"] == []
    assert invoke(c, mine, "support.request_status", request_id=rid)["status"] == "error"   # one application's grant reads only its own
    assert not fake.posts


def test_malformed_proposals_are_refused_before_anything(world):
    c, agent, fake, *_ = world
    who = agent(ALL)
    for args in ({**INVOICE, "text": "x"}, {**INVOICE, "text": "x", "idempotency_key": "comment-0001", "submit": True},
                 {"doctype": "User", "name": "admin", "text": "x", "idempotency_key": "comment-0001"}):
        out = invoke(c, who, "erp.request_comment", **args)
        assert out["status"] in ("error", "denied") and "output" not in out, out
    assert c.get("/api/writes", headers=APPROVER).json()["requests"] == [] and not fake.posts


def test_a_pending_request_survives_a_restart(world):
    c, agent, fake, start, _ = world
    who = agent(ALL)
    rid = invoke(c, who, "erp.request_comment", **INVOICE, text="after restart", idempotency_key="comment-0001")["output"]["request_id"]
    again = start()
    assert decide(again, APPROVER, rid).json()["state"] == "executed_verified" and len(fake.comments) == 1


def test_without_a_state_directory_there_are_lookups_but_no_proposals_or_decision_routes(world):
    _, agent, _, start, monkeypatch = world
    monkeypatch.delenv("AGENTIC_OS_WRITES_DIR")
    c = start()
    who = agent(ALL)
    names = offered(c, who)
    assert "erp.find_records" in names and not any(n.split(".", 1)[1].startswith("request_") for n in names if "." in n)
    assert c.get("/api/writes", headers=APPROVER).status_code == 404


def test_the_agents_tenant_must_hold_the_financial_grant_too(world, monkeypatch):
    c, agent, fake = world[0], world[1], world[2]
    limits = {**LIMITS, "acme": {"policy_refs": [r for r in LIMITS["acme"]["policy_refs"] if r != "egress:financial"]}}
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps(limits))
    out = invoke(c, agent(["erp.read", "egress:financial"]), "erp.get_record", **INVOICE)
    assert out["status"] == "error" and out["error"].startswith("not_authorized (missing_grant)") and "tenant" in out["error"], out
    assert "Acme" not in json.dumps(out) and not fake.posts
    limits["acme"]["policy_refs"].append("egress:financial:erpnext")
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps(limits))
    assert invoke(c, agent(["erp.read", "egress:financial"]), "erp.get_record", **INVOICE)["status"] == "ok"

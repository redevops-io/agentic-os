"""Two vertical workflows an external agent runs through the mounted gateway, under the production profile with a
tenant: Sentinel (investigate, propose, a PERSON decides and executes, verify) and QA (investigate failures, initiate a
run, a PERSON approves it, retrieve results). The products are the contract doubles, which check approval tokens with
the kernel's own functions."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from agentic_os.agent_gateway import agent_tokens as AT
from agentic_os.projects_api import SampleProjectionProvider, create_app
from tests._products_fakes import ACT, CASE, EST, RUN, TGT, FakeQa, FakeSentinel, write_registry

BASE = "/api/agent-gateway/capabilities"
PERSON = {"Authorization": "Basic " + base64.b64encode(b"operator:" + b"p" * 24).decode()}
CLEAR = ("AGENT_GATEWAY_TOKEN", "AGENT_GATEWAY_AUDIT_PATH", "AGENT_GATEWAY_MCP", "PROJECTS_OPERATOR_GATE", "AGENTIC_OS_CONTROL_TOKEN",
         "AGENTIC_OS_TENANT_TOKENS", "AGENTIC_OS_APPROVER_TOKENS", "AGENTIC_OS_DASHBOARDS", "AGENTIC_OS_OAUTH_DB", "PROJECTS_CORS_ORIGINS",
         "AGENTIC_OS_ALLOW_UNAPPROVED_INVOKE")
LIMITS = {"secops": {"policy_refs": ["products:edge-sentinel", "products:redevops-qa"]}, "finance": {"policy_refs": ["project:*"]}}


@pytest.fixture
def world(monkeypatch, tmp_path):
    for k in CLEAR:
        monkeypatch.delenv(k, raising=False)
    sentinel = FakeSentinel(dry_run=False, linked={"projects:operator": "alex"})
    qa = FakeQa(plans=True)
    sentinel_url, qa_url = sentinel.serve(), qa.serve()
    extra = f"""
  - id: redevops-qa
    display_name: redevops-qa
    kind: quality
    api_base: {qa_url}
    ui_url: {qa_url}/ui
    health: {{path: /readyz, interval_s: 1, timeout_s: 2}}
    contract: {{api: /v1, info: /v1/info, min_version: "0.1"}}
    auth:
      service_token_file: {tmp_path / 'secrets' / 'service'}
      decision_token_file: {tmp_path / 'secrets' / 'decision'}
      approval_secret_file: {tmp_path / 'secrets' / 'secret'}
      scheme: bearer
"""
    registry = write_registry(tmp_path, sentinel_url, extra_products=extra)
    agents, db = str(tmp_path / "agents.json"), str(tmp_path / "authority.sqlite")
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<html></html>")
    for k, v in {"AGENTIC_OS_PRODUCTS": registry, "AGENT_GATEWAY_AGENTS_FILE": agents, "AGENTIC_OS_PROFILE": "production",
                 "AGENTIC_OS_AUTHORITY_DB": db, "AGENTIC_OS_APPROVAL_SECRET": "s" * 48, "AGENTIC_OS_REPLAY_DB": str(tmp_path / "replay.sqlite"),
                 "AGENTIC_OS_TENANT_LIMITS": json.dumps(LIMITS), "OPERATOR_BASIC_AUTH": "operator:" + "p" * 24, "PROJECTS_UI_DIR": str(ui)}.items():
        monkeypatch.setenv(k, v)
    client = TestClient(create_app(SampleProjectionProvider()), raise_server_exceptions=False)
    count = {"n": 0}

    def agent(grants, tenant="secops"):
        count["n"] += 1
        return {"Authorization": "Bearer " + AT.issue(agents, f"agent-{count['n']}", grants, minutes=10, tenant=tenant, authority_db=db)}

    yield client, agent, sentinel, qa
    sentinel.close(), qa.close()


def invoke(c, who, name, **args):
    return c.post(f"{BASE}/{name}/invoke", headers=who, json=args).json()


def offered(c, who):
    return {x["name"] for x in c.get(BASE, headers=who).json()["capabilities"]}


def test_sentinel_investigate_propose_approve_execute_verify(world):
    c, agent, sentinel, _ = world
    who = agent(["security.read", "security.propose"])
    # investigate
    assert invoke(c, who, "security.sentinel_posture")["status"] == "ok"
    cases = invoke(c, who, "security.sentinel_list_cases")["output"]["items"]
    assert CASE in {x["case_id"] for x in cases}
    detail = invoke(c, who, "security.sentinel_get_case", case_id=CASE)["output"]
    assert detail["case"]["evidence"] and detail["executed"] is False
    scan = invoke(c, who, "security.deployment_scan", product_id="edge-sentinel")["output"]
    assert scan["scan"]["findings"]
    # propose: a proposal that does nothing; the same key never makes a second one
    before = len(sentinel.actions)
    proposal = invoke(c, who, "security.sentinel_propose_ban", ip="198.51.100.7", reason="scanning", duration_hours=4, idempotency_key="ban-198.51.100.7-a")
    assert proposal["status"] == "ok", proposal
    request_id = proposal["output"]["request_id"]
    assert proposal["output"]["state"] == "AWAITING_APPROVAL" and proposal["output"]["executed"] is False and sentinel.executions == 0
    again = invoke(c, who, "security.sentinel_propose_ban", ip="198.51.100.7", reason="scanning", duration_hours=4, idempotency_key="ban-198.51.100.7-a")
    assert again["output"]["request_id"] == request_id and len(sentinel.actions) == before + 1
    changed = invoke(c, who, "security.sentinel_propose_ban", ip="203.0.113.99", reason="other", idempotency_key="ban-198.51.100.7-a")
    assert changed["status"] == "error" and changed["error"] == "idempotency_conflict" and len(sentinel.actions) == before + 1
    assert sentinel.changes[-1]["actor"].startswith("agent:agent-")                       # the product is told which agent proposed
    assert invoke(c, who, "security.sentinel_read_back", request_id=request_id)["output"]["state"] == "not_run"
    # the agent can neither decide nor execute: not as a tool, and not through the human routes
    assert not {"security.sentinel_decide", "security.sentinel_execute"} & offered(c, who)
    assert invoke(c, who, "security.sentinel_execute", request_id=request_id)["status"] == "denied"
    digest = sentinel.actions[request_id]["digest"]
    case_id = sentinel.actions[request_id]["case_id"]
    for route, body in (("decision", {"approved": True, "digest": digest}), ("execute", {"digest": digest})):
        r = c.post(f"/api/products/edge-sentinel/actions/{request_id}/{route}", headers=who, json=body)
        assert r.status_code in (401, 403), (route, r.status_code, r.text)
    assert sentinel.actions[request_id]["state"] == "AWAITING_APPROVAL" and sentinel.executions == 0
    # a person decides, then executes, in Projects
    r = c.post(f"/api/products/edge-sentinel/actions/{request_id}/decision", headers=PERSON, json={"approved": True, "digest": digest})
    assert r.status_code == 200, r.text
    assert sentinel.actions[request_id]["decision"]["actor"] == "user:alex"
    r = c.post(f"/api/products/edge-sentinel/actions/{request_id}/execute", headers=PERSON, json={"digest": digest})
    assert r.status_code == 200 and sentinel.executions == 1, r.text
    # the agent verifies the resulting state itself
    verified = invoke(c, who, "security.sentinel_read_back", request_id=request_id)["output"]
    assert verified["state"] == "verified" and verified["receipt_id"]
    assert invoke(c, who, "security.sentinel_get_case", case_id=case_id)["output"]["case"]


def test_qa_investigate_failures_initiate_a_run_and_retrieve_results(world):
    c, agent, _, qa = world
    qa.runs["r20261112T090000Z-fa11ed"] = {**qa.runs[RUN], "id": "r20261112T090000Z-fa11ed", "state": "finished", "outcome": "FAILED", "level": "L1"}
    who = agent(["quality.read", "quality.run"])
    runs = invoke(c, who, "quality.qa_list_runs")["output"]["items"]
    failed = [r for r in runs if r.get("outcome") == "FAILED"]
    assert failed
    assert invoke(c, who, "quality.qa_get_run", run_id=failed[0]["id"])["output"]["run"]["outcome"] == "FAILED"
    assert invoke(c, who, "quality.qa_list_targets")["output"]["items"][0]["id"] == TGT
    estimate = invoke(c, who, "quality.qa_estimate", target_id=TGT, profile="edge-sentinel-fixture-readonly")["output"]["estimate"]
    assert estimate["digest"] == EST
    start = dict(target_id=TGT, profile="edge-sentinel-fixture-readonly", estimate_digest=EST, idempotency_key="run-fixture-0001")
    started = invoke(c, who, "quality.qa_start_run", **start)
    assert started["status"] == "ok" and started["output"]["state"] == "awaiting_approval" and started["output"]["executed"] is False
    assert invoke(c, who, "quality.qa_start_run", **start)["output"]["run_id"] == started["output"]["run_id"] and len(qa.started) == 1
    run_id = started["output"]["run_id"]
    assert "quality.qa_approve_run" not in offered(c, who)
    body = {"approved": True, "estimate_digest": EST}
    assert c.post(f"/api/products/redevops-qa/runs/{run_id}/approval", headers=who, json=body).status_code in (401, 403)
    r = c.post(f"/api/products/redevops-qa/runs/{run_id}/approval", headers=PERSON, json=body)
    assert r.status_code == 200 and r.json()["result"]["approved_by"] == "projects:operator", r.text
    assert invoke(c, who, "quality.qa_get_run", run_id=run_id)["output"]["run"]["state"] == "queued"


def test_a_write_needs_an_idempotency_key_and_its_own_grant(world):
    c, agent, sentinel, qa = world
    reader = agent(["security.read", "quality.read"])
    assert not {n for n in offered(c, reader) if "propose" in n or n in ("quality.qa_start_run", "quality.qa_cancel_run")}
    assert invoke(c, reader, "security.sentinel_propose_ban", ip="198.51.100.7", reason="x", idempotency_key="k-00000001")["status"] == "denied"
    writer = agent(["security.propose"])
    refused = invoke(c, writer, "security.sentinel_propose_ban", ip="198.51.100.7", reason="x")
    assert refused["status"] == "denied" and len(sentinel.actions) == 1 and qa.started == []


def test_a_tenant_not_granted_the_product_reads_and_stages_nothing(world):
    c, agent, sentinel, _ = world
    outsider = agent(["security.read", "security.propose", "quality.read"], tenant="finance")
    for name, args in (("security.sentinel_list_cases", {}), ("quality.qa_list_runs", {}),
                       ("security.sentinel_propose_ban", {"ip": "198.51.100.7", "reason": "x", "idempotency_key": "k-00000002"})):
        got = invoke(c, outsider, name, **args)
        assert got["status"] == "error" and "not granted this product" in got["error"], (name, got)
    assert len(sentinel.actions) == 1 and not [x for x in sentinel.calls if x["method"] == "POST"]


def test_the_products_own_refusal_reaches_the_agent_and_bad_input_never_reaches_the_product(world):
    c, agent, sentinel, _ = world
    who = agent(["security.read", "security.propose"])
    got = invoke(c, who, "security.sentinel_propose_ban", ip="127.0.0.1", reason="x", idempotency_key="k-00000003")
    assert got["status"] == "error" and "protected address" in got["error"]
    posts = len([x for x in sentinel.calls if x["method"] == "POST"])
    bad = invoke(c, who, "security.sentinel_propose_ban", ip="198.51.100.7; rm -rf /", reason="x", idempotency_key="k-00000004")
    assert bad["status"] == "error" and bad["error"].startswith("invalid") and len([x for x in sentinel.calls if x["method"] == "POST"]) == posts
    assert invoke(c, who, "security.sentinel_get_case", case_id="case-ffffffffffffffff")["status"] == "error"


def test_the_shared_bearer_never_sees_a_product_capability(world, monkeypatch):
    monkeypatch.setenv("AGENT_GATEWAY_TOKEN", "shared-bearer-value")
    c = TestClient(create_app(SampleProjectionProvider()), raise_server_exceptions=False)
    assert not [n for n in offered(c, {"Authorization": "Bearer shared-bearer-value"}) if n.split(".")[0] in ("security", "quality", "findings")]

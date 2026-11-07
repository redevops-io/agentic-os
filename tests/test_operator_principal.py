"""Operator actions are bound to an authenticated principal, not to a free-text actor string.

approve / reject / resume / suspend / reconcile / compensate take an ``AuthenticatedPrincipal`` produced by the authentication
layer (``PrincipalIssuer``). The runtime verifies it, checks it against the mission's tenant, and records it in the approval or
resume record. An unauthenticated, forged, foreign or wrong-tenant principal is refused and recorded. Without a principal only an
explicit test policy with ``anonymous_operators`` lets an action through, and it is logged as unauthenticated."""
from __future__ import annotations

import json

import pytest

from agentic_os.evidence_query.authz import StaticCeiling
from agentic_os.mission.authority_policy import AuthorityTestPolicy
from agentic_os.mission.demo import build_fleet
from agentic_os.mission.executor import Executor
from agentic_os.mission.principal import (
    AuthenticatedPrincipal, PrincipalError, PrincipalIssuer, PrincipalRequired, default_issuer, principal_of_caller)
from agentic_os.mission.control_auth import Caller
from agentic_os.mission.runtime import ApprovalError, MissionRuntime, ResumeNotAuthorized, ServiceResume
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import Budget, MissionState
from agentic_os.overlays import Principal

GRANTS = ["billing:write", "support:write", "books:write", "compliance:write"]
CONSENT = "compliance.file_consent"
NAMED = AuthorityTestPolicy("named-operators-only", "operators must be authenticated", allow=("*",), compensate=("*",))


def authn(pid="alice", tenant="acme", roles=("approver",)):
    return default_issuer().issue(Principal(pid, "user", roles, tenant), method="test-login")


def build(*, policy=NAMED, store=None, tenant="acme", **kw):
    reg, client = build_fleet()
    rt = MissionRuntime(reg, Executor(client), store=store or EventStore(), test_policy=policy, **kw)
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding", tenant=tenant, budget=Budget(usd=100.0))
    rt.run(m.id)
    assert m.state is MissionState.WAITING_HUMAN
    return rt, m, rt.repo.pending_human(m.id)["node_id"]


def ev(rt, mid, t):
    return [e.payload for e in rt.store.for_mission(mid) if e.type == t]


# ── approval ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_an_approval_records_the_authenticated_principal():
    rt, m, nid = build()
    rt.approve(m.id, nid, "approve", principal=authn("alice"))
    g = ev(rt, m.id, "ApprovalGranted")[0]
    assert g["approver"] == "alice"
    assert g["principal"] == {"id": "alice", "kind": "user", "roles": ["approver"], "tenant": "acme",
                              "method": "test-login", "issuer": "control-plane"}
    assert g["principal_binding"].startswith("sha256:") and "proof" not in json.dumps(g)
    assert m.state is MissionState.SUCCEEDED


def test_a_rejection_records_the_authenticated_principal():
    rt, m, nid = build()
    rt.approve(m.id, nid, "reject", principal=authn("bob"))
    r = ev(rt, m.id, "ApprovalRejected")[0]
    assert r["principal"]["id"] == "bob" and r["authenticated"] is True and m.state is MissionState.FAILED


@pytest.mark.parametrize("who", ["plain", "forged", "foreign_issuer", "tampered"])
def test_an_unauthenticated_forged_or_foreign_principal_is_refused_and_recorded(who):
    rt, m, nid = build()
    if who == "plain":
        p = Principal("mallory", "user", ("approver",), "acme")
    elif who == "forged":
        p = AuthenticatedPrincipal("mallory", "user", ("approver",), "acme", method="control-token", issuer="control-plane", issued_at=1.0, proof="0" * 64)
    elif who == "foreign_issuer":
        p = PrincipalIssuer("control-plane").issue(Principal("mallory", "user", ("approver",), "acme"), method="control-token")
    else:
        good = authn("alice")
        p = AuthenticatedPrincipal("root", good.kind, good.roles, good.tenant, good.method, good.issuer, good.issued_at, good.proof)
    with pytest.raises(PrincipalError):
        rt.approve(m.id, nid, "approve", principal=p)
    assert not ev(rt, m.id, "ApprovalGranted") and m.state is MissionState.WAITING_HUMAN
    assert ev(rt, m.id, "PrincipalRefused")[-1]["reason"] == "not_authenticated"


def test_a_principal_of_another_tenant_is_refused_but_the_all_tenant_admin_is_not():
    rt, m, nid = build()
    with pytest.raises(PrincipalError):
        rt.approve(m.id, nid, "approve", principal=authn("eve", tenant="globex"))
    assert ev(rt, m.id, "PrincipalRefused")[-1]["reason"] == "tenant_mismatch"
    rt.approve(m.id, nid, "approve", principal=principal_of_caller(Caller(None, "admin")))
    assert ev(rt, m.id, "ApprovalGranted")[0]["principal"]["id"] == "admin" and m.state is MissionState.SUCCEEDED


def test_without_a_principal_the_action_is_refused_unless_a_test_policy_names_anonymous_operators():
    rt, m, nid = build()
    with pytest.raises(PrincipalRequired):
        rt.approve(m.id, nid, "approve", actor="someone typed this")          # a free-text actor is not an identity
    assert ev(rt, m.id, "PrincipalRefused")[-1]["reason"] == "unauthenticated" and not ev(rt, m.id, "ApprovalGranted")
    anon = AuthorityTestPolicy("anon-ok", "fixtures approve without logging in", allow=("*",), anonymous_operators=True)
    rt2, m2, nid2 = build(policy=anon)
    rt2.approve(m2.id, nid2, "approve", actor="fixture")
    g = ev(rt2, m2.id, "ApprovalGranted")[0]
    assert g["authenticated"] is False and g["basis"] == "test_policy:anon-ok" and "principal_binding" not in g


def test_anonymous_operators_are_never_accepted_in_production(tmp_path, monkeypatch):
    from agentic_os.mission.authority import AuthorityGuard, SqliteEnvelopeStore, SqliteLedger
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    db = str(tmp_path / "a.db")
    guard = AuthorityGuard(SqliteEnvelopeStore(db), SqliteLedger(db + ".usage"))
    reg, client = build_fleet()
    rt = MissionRuntime(reg, Executor(client), store=EventStore(), authority=guard)       # the suite's class-level policy is ignored here
    m = rt.create_mission("Onboard", policy_refs=GRANTS, template="onboarding", tenant="acme", budget=Budget(usd=100.0))
    with pytest.raises(PrincipalRequired):
        rt.approve(m.id, "any-node", "approve", actor="anything")


def _rechain(lines):
    """What a writer with access to the log file can do: edit a record and recompute the hash chain behind it."""
    from agentic_os.mission.store import entry_hash
    heads, out = {}, []
    for ln in lines:
        d = json.loads(ln)
        d["prev"] = heads.get(d["mission_id"], "")
        d["hash"] = entry_hash(d["prev"], d["seq"], d["ts"], d["type"], d["mission_id"], d["payload"])
        heads[d["mission_id"]] = d["hash"]
        out.append(json.dumps(d))
    return out


def test_the_approver_recorded_in_the_log_cannot_be_swapped_without_being_noticed(tmp_path):
    path = str(tmp_path / "events.jsonl")
    rt, m, nid = build(store=EventStore(path=path))
    rt.approve(m.id, nid, "approve", principal=authn("alice"))
    lines = open(path).read().splitlines()
    out = []
    for ln in lines:
        d = json.loads(ln)
        if d["type"] == "ApprovalGranted":
            d["payload"]["principal"]["id"] = "mallory"
            d["payload"]["approver"] = "mallory"
        out.append(json.dumps(d))
    forged = tmp_path / "forged.jsonl"
    forged.write_text("\n".join(_rechain(out)) + "\n")       # the chain is valid again; the approval's own binding is not
    # the mission is rebuilt from the edited log, gets a resume grant, and the edited approval is not honoured
    reg, client = build_fleet()
    rt2 = MissionRuntime(reg, Executor(client), store=EventStore(path=str(forged)), test_policy=NAMED)
    rt2.rehydrate(m.id)
    # the approval was given while the mission was live; on the rebuilt one it is checked against its binding
    assert rt2._approved(m.id, rt2._plans[m.id]) == set()
    assert ev(rt2, m.id, "ApprovalNotHonoured")[-1]["reason"] == "principal_binding_mismatch"


# ── resume ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def _reconstructed(tmp_path):
    path = str(tmp_path / "events.jsonl")
    rt, m, nid = build(store=EventStore(path=path))
    reg, client = build_fleet()
    rt2 = MissionRuntime(reg, Executor(client), store=EventStore(path=path), test_policy=NAMED)
    rt2.rehydrate(m.id)
    return rt2, m, nid


def test_a_resume_grant_needs_an_authenticated_principal_and_records_it(tmp_path):
    rt2, m, nid = _reconstructed(tmp_path)
    ceil = StaticCeiling({"evidence.acme"})
    with pytest.raises(ResumeNotAuthorized):
        rt2.grant_resume(m.id, principal=Principal("alice", "user", (), "acme"), ceiling=ceil, scope="acme")
    assert ev(rt2, m.id, "ReplayRefused")[-1]["reason"] == "unauthenticated_principal"
    rt2.grant_resume(m.id, principal=authn("alice"), ceiling=ceil, scope="acme")
    a = ev(rt2, m.id, "ReplayAuthorized")[-1]
    assert a["actor"] == "alice" and a["principal"]["method"] == "test-login"


def test_a_resume_grant_belongs_to_the_principal_that_obtained_it(tmp_path):
    rt2, m, nid = _reconstructed(tmp_path)
    rt2.grant_resume(m.id, principal=authn("alice"), ceiling=StaticCeiling({"evidence.acme"}), scope="acme")
    with pytest.raises(ResumeNotAuthorized):
        rt2.run(m.id, principal=authn("bob"))                                # another principal cannot drive alice's resume
    assert rt2.run(m.id, principal=authn("alice")).state is MissionState.WAITING_HUMAN
    rt2.approve(m.id, nid, "approve", principal=authn("carol"))              # an approval is its own act, by its own principal
    assert ev(rt2, m.id, "ApprovalGranted")[-1]["principal"]["id"] == "carol"


def test_a_service_resume_must_hold_an_authenticated_principal():
    with pytest.raises(ValueError):
        ServiceResume(Principal("svc", "service", ()), StaticCeiling({"evidence"}))
    ServiceResume(default_issuer().issue(Principal("svc", "service", ()), method="service-config"), StaticCeiling({"evidence"}))


def test_suspend_and_resume_record_the_principal_and_refuse_a_missing_one():
    rt, m, nid = build()
    with pytest.raises(PrincipalRequired):
        rt.suspend(m.id, actor="x", reason="stop")
    rt.suspend(m.id, reason="stop", principal=authn("alice"))
    assert ev(rt, m.id, "MissionSuspended")[0]["principal"]["id"] == "alice"
    rt.resume(m.id, principal=authn("alice"))
    assert ev(rt, m.id, "MissionResumed")[0]["principal"]["id"] == "alice"


# ── reconcile and compensation interventions ─────────────────────────────────────────────────────────────────────────
def test_reconcile_requires_and_records_the_principal():
    from agentic_os.mission.reconcile import ReconciliationError
    rt, m, nid = build()
    with pytest.raises(PrincipalRequired):
        rt.reconcile_node(m.id, nid, executed=True, evidence={"kind": "provider_record", "reference": "ledger-line-12"}, actor="a", reason="looked it up")
    # a principal passes the identity check; the node has nothing to reconcile, which is the next (unrelated) refusal
    with pytest.raises(ReconciliationError):
        rt.reconcile_node(m.id, nid, executed=True, evidence={"kind": "provider_record", "reference": "ledger-line-12"}, reason="looked it up", principal=authn("alice"))


def test_compensation_interventions_require_and_record_the_principal():
    from agentic_os.mission.compensation_runner import InterventionError
    rt, m, nid = build()
    m.state = MissionState.FAILED
    with pytest.raises(PrincipalRequired):
        rt.compensation_intervention(m.id, "acknowledge", actor="x", reason="r")
    with pytest.raises(InterventionError):
        rt.compensation_intervention(m.id, "acknowledge", reason="r", principal=authn("alice"))      # authenticated; nothing to acknowledge


# ── the HTTP routes carry the credential's principal ─────────────────────────────────────────────────────────────────
def test_the_approve_route_passes_the_authenticated_caller_as_the_principal(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agentic_os.mission.service import build_pilot_runtime, mount
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "admin-secret")
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", "acme=tok-acme")
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({"acme": {"policy_refs": GRANTS}}))
    monkeypatch.delenv("AGENTIC_OS_APPROVER_TOKENS", raising=False)
    app = FastAPI()
    rt = mount(app, build_pilot_runtime())
    c = TestClient(app)
    H = lambda t: {"Authorization": f"Bearer {t}"}  # noqa: E731
    mid = c.post("/missions", headers=H("tok-acme"), json={"goal": "Onboard a new customer", "template": "onboarding"}).json()["id"]
    node = rt.repo.pending_human(mid)["node_id"]
    r = c.post(f"/missions/{mid}/approve", headers=H("tok-acme"), json={"node_id": node})
    assert r.status_code == 200, r.text
    g = ev(rt, mid, "ApprovalGranted")[0]
    assert g["approver"] == "requester:acme" and g["principal"]["method"] == "control-token" and g["principal"]["tenant"] == "acme"

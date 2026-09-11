"""Governed writes — Phase 3 (plan §3, §5).

prepare_* is non-side-effecting and auto-runs; request_* is a governed side effect that gates on
approval before executing, runs under a GovernedEnvelope, is idempotent on retry, and leaves no
side effect when not approved. Audit carries the evidence + policy provenance.
"""
from __future__ import annotations

from agentic_os.overlays import Principal
from agentic_os.agent_gateway import (
    AgentGateway, DataClass, EgressAction, GatewayPrincipal, GatewayRequest, GatewayStatus,
    InMemoryAuditSink, build_write_registry)


# ── fake write backends ─────────────────────────────────────────────────────────
class _Crm:
    def __init__(self): self.prepared, self.sent = 0, 0
    def prepare_outreach(self, args):
        self.prepared += 1
        return {"draft": f"Hi {args.get('contact')}, ...", "prepared_ref": "prep_1"}
    def send_outreach(self, args, envelope):
        assert envelope is not None, "a send must carry a GovernedEnvelope"
        self.sent += 1
        return {"sent": True, "provider_object_id": "wamid.42"}


class _Projects:
    def __init__(self): self.changes = 0
    def request_change(self, args, envelope):
        assert envelope is not None
        self.changes += 1
        return {"project_id": args.get("project_id"), "applied": True,
                "provider_object_id": "chg_7"}


class _Approve:
    def is_satisfied(self, request, manifest): return True


def _p(grants):
    gp = GatewayPrincipal(Principal("agent", "service", (), "acme"))
    return gp, (lambda pr, perm: perm in set(grants))


# ── prepare_ (non-side-effecting) ─────────────────────────────────────────────────
def test_prepare_outreach_is_read_only_and_needs_no_approval():
    gp, authz = _p(("crm.read",))
    crm = _Crm()
    gw = AgentGateway(registry=build_write_registry(crm=crm), authorize=authz)
    r = gw.invoke(GatewayRequest(gp, "crm.prepare_outreach", {"contact": "Tasha", "goal": "demo"}))
    assert r.status is GatewayStatus.OK and r.output["prepared_ref"] == "prep_1"
    assert crm.prepared == 1 and crm.sent == 0            # drafted, nothing sent


# ── request_ (governed side effect) ────────────────────────────────────────────────
def test_request_send_gates_on_approval_before_any_side_effect():
    gp, authz = _p(("crm.write",))
    crm = _Crm()
    gw = AgentGateway(registry=build_write_registry(crm=crm), authorize=authz)   # no approvals
    r = gw.invoke(GatewayRequest(gp, "crm.request_outreach_send", {"contact": "Tasha", "body": "hi"}))
    assert r.status is GatewayStatus.PENDING_APPROVAL
    assert crm.sent == 0                                  # ungated request ⇒ no side effect


def test_approved_send_runs_under_envelope_and_records_evidence():
    gp, authz = _p(("crm.write",))
    crm = _Crm()
    sink = InMemoryAuditSink()
    gw = AgentGateway(registry=build_write_registry(crm=crm), authorize=authz,
                      approvals=_Approve(), audit=sink)
    r = gw.invoke(GatewayRequest(gp, "crm.request_outreach_send", {"contact": "Tasha", "body": "hi"}))
    assert r.status is GatewayStatus.OK and r.output["sent"] is True and crm.sent == 1
    assert r.decision.risk_tier == 2                      # tier honoured
    assert r.evidence_refs == ("wamid.42",)              # provenance surfaced
    assert sink.events[-1].evidence_refs == ("wamid.42",)   # ...and recorded in the audit


def test_send_is_idempotent_on_retry():
    gp, authz = _p(("crm.write",))
    crm = _Crm()
    gw = AgentGateway(registry=build_write_registry(crm=crm), authorize=authz, approvals=_Approve())
    a = GatewayRequest(gp, "crm.request_outreach_send", {"contact": "T", "body": "x"},
                       idempotency_key="k9")
    first = gw.invoke(a)
    second = gw.invoke(GatewayRequest(gp, "crm.request_outreach_send", {"contact": "T", "body": "x"},
                                      idempotency_key="k9"))
    assert first.ok and second.ok and crm.sent == 1      # sent exactly once
    assert second.request_id == first.request_id         # replayed stored result


def test_projects_request_change_is_governed_like_a_write():
    gp, authz = _p(("projects.write",))
    projects = _Projects()
    gw = AgentGateway(registry=build_write_registry(projects=projects), authorize=authz)
    pending = gw.invoke(GatewayRequest(gp, "projects.request_change",
                                       {"project_id": "p1", "change": "rename"}))
    assert pending.status is GatewayStatus.PENDING_APPROVAL and projects.changes == 0
    gw.approvals = _Approve()
    ok = gw.invoke(GatewayRequest(gp, "projects.request_change",
                                  {"project_id": "p1", "change": "rename"}))
    assert ok.status is GatewayStatus.OK and projects.changes == 1


def test_write_without_permission_is_unknown_and_never_executes():
    gp, authz = _p(("crm.read",))                        # read only, no crm.write
    crm = _Crm()
    gw = AgentGateway(registry=build_write_registry(crm=crm), authorize=authz, approvals=_Approve())
    r = gw.invoke(GatewayRequest(gp, "crm.request_outreach_send", {"contact": "T", "body": "x"}))
    assert r.status is GatewayStatus.DENIED and r.decision.reason == "unknown_capability"
    assert crm.sent == 0


def test_build_write_registry_registers_only_provided_backends():
    reg = build_write_registry(crm=_Crm())               # no projects backend
    names = {m.name for m in reg.all()}
    assert names == {"crm.prepare_outreach", "crm.request_outreach_send"}

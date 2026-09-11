"""Governed Agent Gateway — Phase 0 invariants (plan §13 Phase-1 acceptance, §14 testing).

The external agent is untrusted-but-authenticated: a capability it can't see is neither
enumerable nor invokable; side effects gate on approval before executing; every call audits.
No network here — identity is an in-test authorizer, the Mission Runtime is a fake.
"""
from __future__ import annotations

import pytest

from agentic_os.overlays import Principal
from agentic_os.agent_gateway import (
    AgentGateway, ApprovalPolicy, CapabilityKind, CapabilityManifest, DataClass, EgressAction,
    GatewayPrincipal, GatewayRequest, GatewayStatus, HandlerResult, InMemoryAuditSink,
    MissionDelegation, RiskTier)
from agentic_os.agent_gateway.registry import CapabilityRegistry


# ── fixtures / fakes ──────────────────────────────────────────────────────────────
def _principal(grants=(), scopes=(), tenant="acme"):
    gp = GatewayPrincipal(Principal("agent-user", "service", tuple(grants), tenant),
                          client_id="claude-desktop", scopes=tuple(scopes))
    authorize = lambda p, perm: perm in set(grants)      # deny-by-default over the grant set
    return gp, authorize


class _RecordingHandler:
    def __init__(self, output, data_classes=()):
        self.output, self.data_classes = output, data_classes
        self.calls, self.last_envelope = 0, "unset"
    def __call__(self, request, envelope):
        self.calls += 1
        self.last_envelope = envelope
        return HandlerResult(ok=True, output=self.output, data_classes=self.data_classes,
                             source_refs=("src:1",), evidence_refs=("ev:1",))


class _FakeMission:
    def __init__(self):
        self.delegated = []
    def delegate(self, goal, *, constraints, principal, arguments):
        self.delegated.append(goal)
        return MissionDelegation(mission_id="mission:42", state="running", needs_approval=True)


class _AlwaysApprove:
    def is_satisfied(self, request, manifest):
        return True


class _DenyPII:
    def decide(self, output, data_classes, principal, manifest):
        if DataClass.PII in data_classes:
            return EgressAction.DENY, None, ("egress:no-pii",)
        return EgressAction.ALLOW, output, ()


# capabilities under test
READ = CapabilityManifest("sources.search", "Search sources", CapabilityKind.DIRECT,
                          permissions=("sources.read",), risk_tier=RiskTier.READ)
WRITE = CapabilityManifest("crm.request_outreach_send", "Send outreach", CapabilityKind.DIRECT,
                           permissions=("crm.write",), risk_tier=RiskTier.CONSEQUENTIAL,
                           side_effecting=True, idempotent=True, provider="crm")
MISSION = CapabilityManifest("missions.delegate_goal", "Delegate a goal", CapabilityKind.MISSION,
                             permissions=("missions.delegate",), risk_tier=RiskTier.BOUNDED_WRITE,
                             side_effecting=True, approval_policy=ApprovalPolicy.IF_POLICY)
ADMIN = CapabilityManifest("admin.audit_export", "Export audit", CapabilityKind.DIRECT,
                           permissions=("admin.read",), scopes=("admin",), risk_tier=RiskTier.READ)


def _registry(read_handler=None):
    r = CapabilityRegistry()
    r.register(READ, read_handler or _RecordingHandler({"rows": [1, 2]}))
    r.register(WRITE, _RecordingHandler({"sent": True}))
    r.register(MISSION)
    r.register(ADMIN, _RecordingHandler({"audit": []}))
    return r


# ── manifest validation ────────────────────────────────────────────────────────────
def test_manifest_rejects_tier_side_effect_mismatch():
    with pytest.raises(ValueError):
        CapabilityManifest("x.y", "d", CapabilityKind.DIRECT, side_effecting=True,
                           risk_tier=RiskTier.READ)                 # write can't be READ tier
    with pytest.raises(ValueError):
        CapabilityManifest("x.y", "d", CapabilityKind.DIRECT, side_effecting=False,
                           risk_tier=RiskTier.CONSEQUENTIAL)        # read can't be a write tier


def test_default_approval_policy_follows_tier():
    assert ApprovalPolicy.default_for(RiskTier.READ) is ApprovalPolicy.AUTO
    assert ApprovalPolicy.default_for(RiskTier.CONSEQUENTIAL) is ApprovalPolicy.REQUIRED
    assert ApprovalPolicy.default_for(RiskTier.CRITICAL) is ApprovalPolicy.MANDATORY


# ── discovery / filtering ────────────────────────────────────────────────────────────
def test_capability_list_is_the_filtered_set_only():
    gp, authz = _principal(grants=("sources.read", "missions.delegate"))
    gw = AgentGateway(registry=_registry(), authorize=authz)
    names = {c["name"] for c in gw.capabilities_for(gp)}
    assert names == {"sources.search", "missions.delegate_goal"}   # not crm.write, not admin (no scope)


def test_scoped_capability_hidden_without_the_scope():
    with_scope, authz = _principal(grants=("admin.read",), scopes=("admin",))
    no_scope, authz2 = _principal(grants=("admin.read",), scopes=())
    gw = AgentGateway(registry=_registry(), authorize=authz)
    assert "admin.audit_export" in {c["name"] for c in gw.capabilities_for(with_scope)}
    gw2 = AgentGateway(registry=_registry(), authorize=authz2)
    assert "admin.audit_export" not in {c["name"] for c in gw2.capabilities_for(no_scope)}


def test_public_view_never_leaks_permissions_or_provider():
    view = WRITE.public_view()
    assert "permissions" not in view and "provider" not in view and "egress_class" not in view


# ── the governed path ────────────────────────────────────────────────────────────────
def test_read_capability_with_grant_runs_and_audits():
    gp, authz = _principal(grants=("sources.read",))
    handler = _RecordingHandler({"rows": [1, 2]})
    gw = AgentGateway(registry=_registry(handler), authorize=authz, audit=InMemoryAuditSink())
    r = gw.invoke(GatewayRequest(gp, "sources.search", {"q": "acme"}))
    assert r.status is GatewayStatus.OK and r.output == {"rows": [1, 2]}
    assert handler.calls == 1 and r.audit_ref
    assert gw.audit.events[-1].capability == "sources.search"     # written at the boundary


def test_hidden_capability_is_denied_as_unknown_and_not_executed():
    gp, authz = _principal(grants=())                             # no grants at all
    handler = _RecordingHandler({"rows": []})
    gw = AgentGateway(registry=_registry(handler), authorize=authz)
    r = gw.invoke(GatewayRequest(gp, "sources.search"))
    assert r.status is GatewayStatus.DENIED and r.decision.reason == "unknown_capability"
    assert handler.calls == 0                                     # never reached the handler
    assert r.client_view() == {"status": "denied", "request_id": r.request_id}  # no leak of why/what


def test_permission_denied_and_missing_capability_are_indistinguishable():
    gp, authz = _principal(grants=())
    gw = AgentGateway(registry=_registry(), authorize=authz)
    denied = gw.invoke(GatewayRequest(gp, "sources.search"))
    missing = gw.invoke(GatewayRequest(gp, "does.not.exist"))
    assert denied.decision.reason == missing.decision.reason == "unknown_capability"


def test_side_effecting_write_gates_on_approval_before_executing():
    gp, authz = _principal(grants=("crm.write",))
    handler = _RecordingHandler({"sent": True})
    reg = CapabilityRegistry().register(WRITE, handler)
    gw = AgentGateway(registry=reg, authorize=authz)               # default: no approvals granted
    r = gw.invoke(GatewayRequest(gp, "crm.request_outreach_send", {"to": "x"}))
    assert r.status is GatewayStatus.PENDING_APPROVAL
    assert handler.calls == 0                                      # rejected/pending ⇒ no side effect


def test_approved_write_executes_under_a_governed_envelope():
    gp, authz = _principal(grants=("crm.write",))
    handler = _RecordingHandler({"sent": True})
    reg = CapabilityRegistry().register(WRITE, handler)
    gw = AgentGateway(registry=reg, authorize=authz, approvals=_AlwaysApprove())
    r = gw.invoke(GatewayRequest(gp, "crm.request_outreach_send", {"to": "x"}))
    assert r.status is GatewayStatus.OK and handler.calls == 1
    # a side-effecting DIRECT write runs under a GovernedEnvelope carrying the capability + tier
    env = handler.last_envelope
    assert env is not None and env.capability == "crm.request_outreach_send" and env.tier == 2


def test_idempotent_write_replays_without_re_executing():
    gp, authz = _principal(grants=("crm.write",))
    handler = _RecordingHandler({"sent": True})
    reg = CapabilityRegistry().register(WRITE, handler)
    gw = AgentGateway(registry=reg, authorize=authz, approvals=_AlwaysApprove())
    req = GatewayRequest(gp, "crm.request_outreach_send", {"to": "x"}, idempotency_key="k1")
    first = gw.invoke(req)
    second = gw.invoke(GatewayRequest(gp, "crm.request_outreach_send", {"to": "x"},
                                      idempotency_key="k1"))
    assert first.status is GatewayStatus.OK and second.status is GatewayStatus.OK
    assert handler.calls == 1                                     # deduped server-side
    assert second.request_id == first.request_id                 # same stored result


def test_egress_policy_can_withhold_output():
    gp, authz = _principal(grants=("sources.read",))
    handler = _RecordingHandler({"email": "a@b.com"}, data_classes=(DataClass.PII,))
    gw = AgentGateway(registry=_registry(handler), authorize=authz, egress=_DenyPII())
    r = gw.invoke(GatewayRequest(gp, "sources.search"))
    assert r.status is GatewayStatus.OK
    assert r.decision.egress_action is EgressAction.DENY and r.output is None
    assert "egress:no-pii" in r.decision.policy_rule_ids


def test_mission_delegation_routes_to_the_runtime():
    gp, authz = _principal(grants=("missions.delegate",))
    fake = _FakeMission()
    gw = AgentGateway(registry=_registry(), authorize=authz, mission=fake)
    r = gw.invoke(GatewayRequest(gp, "missions.delegate_goal",
                                 {"goal": "find 5 pilot prospects and prepare outreach"}))
    assert r.status is GatewayStatus.OK and r.mission_id == "mission:42"
    assert fake.delegated == ["find 5 pilot prospects and prepare outreach"]
    assert r.output["needs_approval"] is True


def test_mission_delegation_requires_a_goal():
    gp, authz = _principal(grants=("missions.delegate",))
    gw = AgentGateway(registry=_registry(), authorize=authz, mission=_FakeMission())
    r = gw.invoke(GatewayRequest(gp, "missions.delegate_goal", {}))
    assert r.status is GatewayStatus.ERROR and "goal" in r.error


def test_every_call_produces_one_audit_event():
    gp, authz = _principal(grants=("sources.read",))
    sink = InMemoryAuditSink()
    gw = AgentGateway(registry=_registry(), authorize=authz, audit=sink)
    gw.invoke(GatewayRequest(gp, "sources.search"))
    gw.invoke(GatewayRequest(gp, "nope"))            # denied still audits
    assert len(sink.events) == 2
    assert [e.status for e in sink.events] == [GatewayStatus.OK, GatewayStatus.DENIED]

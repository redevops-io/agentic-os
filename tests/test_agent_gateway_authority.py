"""The gateway with a Mission Authority Envelope configured is fail-closed and the delegating agent
cannot choose its own governance (audit finding F1/F2)."""
from __future__ import annotations

from agentic_os.agent_gateway import (
    AgentGateway, DevTokenVerifier, GatewayPrincipal, GatewayRequest, GatewayStatus, MissionRuntimeAdapter,
    register_mission_capabilities)
from agentic_os.agent_gateway.registry import CapabilityRegistry
from agentic_os.mission.authority import (
    AuthorityEnvelope, AuthorityGuard, AutonomyBudget, Grant, MemoryEnvelopeStore, MemoryLedger)
from agentic_os.overlays import Principal

NOW = 2_000_000.0


class _St:
    def __init__(self, n): self.name = n


class _M:
    def __init__(self, i, s): self.id = i; self.state = _St(s)


class _Rt:
    def __init__(self): self.created = []
    def create_mission(self, goal, *, constraints=None, **kw):
        self.created.append((goal, tuple(constraints or ())))
        return _M("mission:1", "PLANNED")
    def run(self, mid): return _M(mid, "SUCCEEDED")
    def missions(self): return []
    def inbox(self): return []
    def explain(self, mid): return {}
    def suspend(self, mid, *, actor, reason): return _M(mid, "SUSPENDED")
    def resume(self, mid, *, actor): return _M(mid, "RUNNING")


def wire(env=None, observer=None):
    rt = _Rt()
    adapter = MissionRuntimeAdapter(rt)
    dev = DevTokenVerifier()
    reg = register_mission_capabilities(CapabilityRegistry(), adapter)
    store = MemoryEnvelopeStore()
    if env is not None:
        store.put(env)
    guard = AuthorityGuard(store, MemoryLedger(), clock=lambda: NOW + 1)
    gw = AgentGateway(registry=reg, authorize=dev.authorize, mission=adapter, authority=guard, on_authority=observer)
    dev.issue("agent", tenant="acme", grants=("missions.delegate", "missions.read", "missions.control"))
    return rt, gw, store, GatewayPrincipal(Principal("agent", "service", (), "acme"))


def envelope(**kw):
    base = dict(tenant="acme", principal="agent", mission_id="acme:agent", issued_at=NOW, expires_at=NOW + 3600,
                grants=(Grant("missions.*", max_tier=3),), may_delegate=True,
                budget=AutonomyBudget(usd=1, max_actions=50, max_children=1, max_depth=1, wall_clock_s=3600),
                required_constraints=("approval:side_effecting",))
    base.update(kw)
    return AuthorityEnvelope(**base)


def call(gw, gp, cap="missions.delegate_goal", **args):
    return gw.invoke(GatewayRequest(gp, cap, args or {"goal": "do it"}))


def test_no_envelope_means_denied_even_for_a_visible_capability():
    rt, gw, _, gp = wire(env=None)
    r = call(gw, gp)
    assert r.status is GatewayStatus.DENIED and r.decision.reason == "authority:no_authority"
    assert rt.created == []


def test_delegated_mission_inherits_required_constraints_and_agent_cannot_drop_them():
    rt, gw, _, gp = wire(envelope())
    r = call(gw, gp, goal="x", constraints=[])                 # the agent supplies none
    assert r.status is GatewayStatus.OK
    assert rt.created[0][1][0] == "approval:side_effecting"
    rt2, gw2, _, gp2 = wire(envelope(mission_id="acme:agent"))
    call(gw2, gp2, goal="x", constraints=["approval:side_effecting", "approval:all"])
    assert rt2.created[0][1] == ("approval:side_effecting", "approval:all")     # additions kept, no duplicate


def test_child_quota_then_pending_approval_not_unbounded_spawning():
    rt, gw, _, gp = wire(envelope())
    assert call(gw, gp, goal="a").status is GatewayStatus.OK
    r = call(gw, gp, goal="b")                                  # max_children=1
    assert r.status is GatewayStatus.PENDING_APPROVAL and r.decision.reason == "authority:child_quota"
    assert len(rt.created) == 1


def test_delegation_denied_when_envelope_forbids_it():
    rt, gw, _, gp = wire(envelope(may_delegate=False))
    r = call(gw, gp, goal="x")
    assert r.status is GatewayStatus.DENIED and r.decision.reason == "authority:delegation_not_allowed"
    assert rt.created == []


def test_scope_limits_other_capabilities():
    rt, gw, _, gp = wire(envelope(grants=(Grant("missions.delegate_goal"),)))
    assert call(gw, gp, "missions.pause", mission_id="m").decision.reason == "authority:out_of_scope"


def test_revocation_stops_new_dispatch_immediately():
    rt, gw, store, gp = wire(envelope())
    assert call(gw, gp, goal="a").status is GatewayStatus.OK
    store.revoke_all("acme")
    r = call(gw, gp, goal="b")
    assert r.status is GatewayStatus.DENIED and r.decision.reason == "authority:revoked"


def test_expiry_denies():
    rt, gw, _, gp = wire(envelope(expires_at=NOW))
    assert call(gw, gp).decision.reason == "authority:expired"


def test_envelope_is_never_taken_from_the_request():
    """A request that carries its own 'authority' or 'envelope' argument changes nothing."""
    rt, gw, _, gp = wire(env=None)
    r = call(gw, gp, goal="x", authority={"grants": [{"capability": "*"}]}, envelope={"may_delegate": True})
    assert r.status is GatewayStatus.DENIED and rt.created == []


def test_other_tenants_envelope_is_not_usable():
    rt, gw, store, gp = wire(envelope(tenant="evil", mission_id="acme:agent"))
    assert call(gw, gp).decision.reason == "authority:no_authority"


def test_observer_sees_decisions_and_cannot_break_enforcement():
    seen = []
    def obs(req, dec):
        seen.append(dec.rule)
        raise RuntimeError("observer bug")
    rt, gw, _, gp = wire(envelope(), observer=obs)
    assert call(gw, gp, "missions.pause", mission_id="m").status is GatewayStatus.OK
    assert seen == ["granted"]


def test_without_authority_configured_nothing_changes():
    rt = _Rt(); adapter = MissionRuntimeAdapter(rt); dev = DevTokenVerifier()
    gw = AgentGateway(registry=register_mission_capabilities(CapabilityRegistry(), adapter),
                      authorize=dev.authorize, mission=adapter)
    dev.issue("agent", tenant="acme", grants=("missions.delegate",))
    gp = GatewayPrincipal(Principal("agent", "service", (), "acme"))
    assert gw.invoke(GatewayRequest(gp, "missions.delegate_goal", {"goal": "x"})).status is GatewayStatus.OK


# ── standing grants at the gateway: ordinary approval satisfied, mandatory never ────────
from dataclasses import replace as _replace                                    # noqa: E402

from agentic_os.agent_gateway import build_write_registry                      # noqa: E402
from agentic_os.agent_gateway.contracts import ApprovalPolicy                  # noqa: E402
from agentic_os.agent_gateway.registry import HandlerResult                    # noqa: E402
from agentic_os.agent_gateway.write_capabilities import CRM_REQUEST_OUTREACH_SEND   # noqa: E402


class _Crm:
    def __init__(self): self.sent = 0
    def prepare_outreach(self, args): return {"draft": "d", "prepared_ref": "p"}
    def send_outreach(self, args, envelope):
        self.sent += 1
        return {"sent": True, "provider_object_id": "w1"}


def _writes(max_tier, grant_cap="crm.request_outreach_send", resources=()):
    crm = _Crm()
    reg = build_write_registry(crm=crm)
    store = MemoryEnvelopeStore()
    store.put(envelope(grants=(Grant(grant_cap, max_tier=max_tier, resources=resources),), may_delegate=False))
    gw = AgentGateway(registry=reg, authorize=lambda p, perm: True,
                      authority=AuthorityGuard(store, MemoryLedger(), clock=lambda: NOW + 1))
    return crm, gw, GatewayPrincipal(Principal("agent", "service", (), "acme"))


def _send(gw, gp, **kw):
    return gw.invoke(GatewayRequest(gp, "crm.request_outreach_send", {"contact": "a@customer.com", **kw}))


def test_default_grant_tier_still_requires_the_human_approval():
    crm, gw, gp = _writes(max_tier=1)                       # tier-2 action, grant only covers tier 1
    r = _send(gw, gp)
    assert r.status is GatewayStatus.PENDING_APPROVAL and r.decision.reason == "authority:tier_over_grant" and crm.sent == 0


def test_explicit_grant_raised_to_the_tier_satisfies_an_ordinary_approval():
    crm, gw, gp = _writes(max_tier=2)
    r = _send(gw, gp)
    assert r.status is GatewayStatus.OK and crm.sent == 1
    assert "authority:standing_grant" in r.decision.policy_rule_ids


def test_standing_grant_is_checked_against_the_exact_parameters():
    crm, gw, gp = _writes(max_tier=2, resources=(("contact", ("*@customer.com",)),))
    assert _send(gw, gp).status is GatewayStatus.OK
    r = gw.invoke(GatewayRequest(gp, "crm.request_outreach_send", {"contact": "x@evil.com"}))
    assert r.status is GatewayStatus.DENIED and crm.sent == 1


def test_bare_wildcard_grant_does_not_satisfy_an_approval():
    crm, gw, gp = _writes(max_tier=2, grant_cap="*")
    r = _send(gw, gp)
    assert r.status is GatewayStatus.PENDING_APPROVAL and r.decision.reason == "authority:grant_not_explicit" and crm.sent == 0


def test_mandatory_approval_is_never_satisfied_by_a_grant():
    mandatory = _replace(CRM_REQUEST_OUTREACH_SEND, approval_policy=ApprovalPolicy.MANDATORY)
    reg2 = CapabilityRegistry()
    reg2.register(mandatory, lambda req, env: HandlerResult(ok=True, output={"sent": True}, data_classes=mandatory.data_classes))
    store = MemoryEnvelopeStore()
    store.put(envelope(grants=(Grant("crm.request_outreach_send", max_tier=3),), may_delegate=False))
    gw = AgentGateway(registry=reg2, authorize=lambda p, perm: True,
                      authority=AuthorityGuard(store, MemoryLedger(), clock=lambda: NOW + 1))
    r = gw.invoke(GatewayRequest(GatewayPrincipal(Principal("agent", "service", (), "acme")),
                                 "crm.request_outreach_send", {"contact": "a@customer.com"}))
    assert r.status is GatewayStatus.PENDING_APPROVAL and "authority:standing_grant" not in r.decision.policy_rule_ids


# ── reservation lifecycle at the gateway ────────────────────────────────────────────────
from agentic_os.mission.authority import Lease as _Lease                       # noqa: E402


class _FailingCrm(_Crm):
    def send_outreach(self, args, envelope):
        raise RuntimeError("provider timeout after the request was written")


def _lifecycle(crm, max_tier=2, max_calls=5, policy=None, budget=None):
    reg = build_write_registry(crm=crm)
    if policy is not None:
        reg2 = CapabilityRegistry()
        m = _replace(CRM_REQUEST_OUTREACH_SEND, approval_policy=policy)
        reg2.register(m, lambda req, env: HandlerResult(ok=True, output={"sent": True}, data_classes=m.data_classes))
        reg = reg2
    store = MemoryEnvelopeStore()
    store.put(envelope(grants=(Grant("crm.request_outreach_send", max_tier=max_tier, max_calls=max_calls),), may_delegate=False))
    guard = AuthorityGuard(store, MemoryLedger(), clock=lambda: NOW + 1)
    kw = {"budget": budget} if budget is not None else {}
    gw = AgentGateway(registry=reg, authorize=lambda p, perm: True, authority=guard, **kw)
    return gw, guard, GatewayPrincipal(Principal("agent", "service", (), "acme"))


def _lease(guard, req_id):
    return guard.ledger.lease_state(req_id)


def test_success_settles_and_keeps_the_consumption():
    gw, guard, gp = _lifecycle(_Crm())
    req = GatewayRequest(gp, "crm.request_outreach_send", {"contact": "a@customer.com"})
    assert gw.invoke(req).status is GatewayStatus.OK
    assert _lease(guard, req.request_id) == _Lease.SETTLED.value and guard.ledger.used("acme:agent")["g0:calls"] == 1
    assert guard.outstanding("acme:agent") == []


def test_pending_approval_before_dispatch_returns_the_reservation():
    gw, guard, gp = _lifecycle(_Crm(), max_tier=3, policy=ApprovalPolicy.MANDATORY)
    req = GatewayRequest(gp, "crm.request_outreach_send", {"contact": "a@customer.com"})
    assert gw.invoke(req).status is GatewayStatus.PENDING_APPROVAL
    assert _lease(guard, req.request_id) == _Lease.RELEASED.value and guard.ledger.used("acme:agent")["g0:calls"] == 0


def test_side_effecting_error_is_unknown_and_stays_counted_until_reconciled():
    crm = _FailingCrm()
    gw, guard, gp = _lifecycle(crm, max_calls=1)
    req = GatewayRequest(gp, "crm.request_outreach_send", {"contact": "a@customer.com"})
    assert gw.invoke(req).status is GatewayStatus.ERROR
    assert _lease(guard, req.request_id) == _Lease.UNKNOWN.value
    # the allowance is held: a second attempt cannot spend it again while the first is unresolved
    r2 = gw.invoke(GatewayRequest(gp, "crm.request_outreach_send", {"contact": "a@customer.com"}))
    assert r2.status is GatewayStatus.PENDING_APPROVAL and r2.decision.reason == "authority:grant_limit"
    assert guard.outstanding("acme:agent") == [(req.request_id, "unknown")]
    # an independent check (e.g. the provider's sent log) says it did not go out: capacity returns
    assert guard.reconcile(req.request_id, executed=False)
    assert _lease(guard, req.request_id) == _Lease.RELEASED.value and guard.ledger.used("acme:agent")["g0:calls"] == 0


def test_budget_refusal_after_the_authority_check_returns_the_reservation():
    class _NoBudget:
        def check(self, principal, manifest):
            return False, "daily_cap", ()
    gw, guard, gp = _lifecycle(_Crm(), budget=_NoBudget())
    req = GatewayRequest(gp, "crm.request_outreach_send", {"contact": "a@customer.com"})
    assert gw.invoke(req).status is GatewayStatus.DENIED
    assert _lease(guard, req.request_id) == _Lease.RELEASED.value and guard.ledger.used("acme:agent")["g0:calls"] == 0


def test_a_call_counts_once_per_request():
    gw, guard, gp = _lifecycle(_Crm(), max_calls=3)
    for _ in range(3):
        assert gw.invoke(GatewayRequest(gp, "crm.request_outreach_send", {"contact": "a@customer.com"})).status is GatewayStatus.OK
    r = gw.invoke(GatewayRequest(gp, "crm.request_outreach_send", {"contact": "a@customer.com"}))
    assert r.status is GatewayStatus.PENDING_APPROVAL and guard.ledger.used("acme:agent")["g0:calls"] == 3

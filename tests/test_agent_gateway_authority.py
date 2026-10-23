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

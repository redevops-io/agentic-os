"""A direct call whose outcome is unknown is not sent again until someone reconciles it.

Found by putting a real agent loop (agent-harness + context-runtime) in front of the gateway with a model that retries at once after a timeout:
the mission runtime parked such a node, but a DIRECT gateway call was simply sent again (the successful-result replay does not cover a call that
errored), so an effect happened twice."""
from __future__ import annotations

import pytest

from agentic_os.agent_gateway import (
    AgentGateway, CapabilityKind, CapabilityManifest, CapabilityRegistry, GatewayPrincipal, GatewayRequest, GatewayStatus, HandlerResult, RiskTier)
from agentic_os.mission.authority import (
    AuthorityEnvelope, AuthorityGuard, AutonomyBudget, Grant, MemoryEnvelopeStore, MemoryLedger, SqliteEnvelopeStore, SqliteLedger)
from agentic_os.mission.reconcile import ReconciliationConflict, ReconciliationError
from agentic_os.overlays import Principal

NOW = 2_000_000.0
EVIDENCE = {"kind": "provider_record", "reference": "provider dashboard, refund obj_1, 2026-10-24"}


class Provider:
    def __init__(self):
        self.effects = 0
        self.sends = 0
        self.fail_next = False
        self.keys: list = []

    def refund(self, req, env):
        self.sends += 1
        self.keys.append(req.idempotency_key)
        self.effects += 1                                   # the request is written and applied ...
        if self.fail_next:
            self.fail_next = False
            raise TimeoutError("timed out after the request was written")          # ... and the caller never learns it
        return HandlerResult(ok=True, output={"ok": True, "provider_object_id": f"obj_{self.effects}"})


def wire(max_calls=10, db=None):
    prov = Provider()
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("billing.refund", "refund", CapabilityKind.DIRECT, permissions=("billing.refund",),
                                    risk_tier=RiskTier.BOUNDED_WRITE, side_effecting=True, idempotent=True, provider="p"), prov.refund)
    reg.register(CapabilityManifest("billing.credit", "credit", CapabilityKind.DIRECT, permissions=("billing.refund",),
                                    risk_tier=RiskTier.BOUNDED_WRITE, side_effecting=True, idempotent=True, provider="p"), prov.refund)
    reg.register(CapabilityManifest("crm.read", "read", CapabilityKind.DIRECT, permissions=("crm.read",)), lambda r, e: HandlerResult(ok=True, output={"ok": 1}))
    st = SqliteEnvelopeStore(db) if db else MemoryEnvelopeStore()
    st.put(AuthorityEnvelope(tenant="acme", principal="agent", mission_id="acme:agent", issued_at=NOW, expires_at=NOW + 3600,
                             grants=(Grant("billing.refund", max_tier=1, max_calls=max_calls), Grant("billing.credit", max_tier=1), Grant("crm.read")),
                             budget=AutonomyBudget(usd=5, max_actions=50, wall_clock_s=3600)))
    guard = AuthorityGuard(st, SqliteLedger(db + '.usage') if db else MemoryLedger(), clock=lambda: NOW + 1)

    class Approved:
        def is_satisfied(self, request, manifest):
            return True
    gw = AgentGateway(registry=reg, authorize=lambda pr, perm: True, approvals=Approved(), authority=guard)
    return prov, gw, guard, GatewayPrincipal(Principal("agent", "service", (), "acme"))


def refund(gw, gp, key="k1", **kw):
    return gw.invoke(GatewayRequest(gp, "billing.refund", {"invoice": "INV-1", "amount": 40, **kw}, idempotency_key=key))


def test_a_retry_after_an_ambiguous_failure_is_refused_not_sent_again():
    prov, gw, guard, gp = wire()
    prov.fail_next = True
    first = refund(gw, gp)
    assert first.status is GatewayStatus.ERROR and prov.effects == 1
    second = refund(gw, gp)
    assert second.status is GatewayStatus.DENIED and second.decision.reason == "authority:outcome_unknown"
    assert prov.sends == 1 and prov.effects == 1                                      # the provider was not asked again
    for _ in range(3):
        assert refund(gw, gp).decision.reason == "authority:outcome_unknown"
    assert prov.sends == 1


def test_the_refusal_names_the_reservation_to_reconcile_and_is_visible_to_a_monitor():
    prov, gw, guard, gp = wire()
    seen = []
    gw.on_authority = lambda req, dec: seen.append((dec.rule, dec.lease_id))
    prov.fail_next = True
    refund(gw, gp)
    refund(gw, gp)
    pending = gw.unresolved_calls("acme:agent")
    assert len(pending) == 1 and pending[0]["state"] == "unknown" and pending[0]["meta"]["idempotency_key"] == "k1"
    assert ("outcome_unknown", pending[0]["lease_id"]) in seen


def test_confirming_that_it_executed_settles_it_and_a_retry_gets_the_recorded_result_without_a_send():
    prov, gw, guard, gp = wire()
    prov.fail_next = True
    refund(gw, gp)
    (lease,) = gw.unresolved_calls("acme:agent")
    gw.reconcile_call(lease["lease_id"], executed=True, evidence=EVIDENCE, actor="ops@acme", reason="refund obj_1 is in the dashboard",
                      result={"ok": True, "provider_object_id": "obj_1"})
    again = refund(gw, gp)
    assert again.status is GatewayStatus.OK and again.output["provider_object_id"] == "obj_1"
    assert prov.sends == 1 and prov.effects == 1 and gw.unresolved_calls("acme:agent") == []
    assert guard.lease(lease["lease_id"])["state"] == "settled"


def test_confirming_that_it_did_not_execute_returns_the_reservation_and_allows_a_new_attempt():
    prov, gw, guard, gp = wire()
    prov.fail_next = True
    refund(gw, gp)
    (lease,) = gw.unresolved_calls("acme:agent")
    used_before = guard.ledger.used("acme:agent")["actions"]
    gw.reconcile_call(lease["lease_id"], executed=False, evidence=EVIDENCE, actor="ops@acme", reason="no refund exists for this key")
    assert guard.ledger.used("acme:agent")["actions"] < used_before                  # the counted reservation went back
    assert refund(gw, gp).status is GatewayStatus.OK and prov.sends == 2


def test_resolution_needs_evidence_a_named_person_and_a_reason_and_the_first_decision_stands():
    prov, gw, guard, gp = wire()
    prov.fail_next = True
    refund(gw, gp)
    (lease,) = gw.unresolved_calls("acme:agent")
    lid = lease["lease_id"]
    for kw in (dict(evidence={}, actor="a", reason="a good reason here"), dict(evidence=EVIDENCE, actor=" ", reason="a good reason here"),
               dict(evidence=EVIDENCE, actor="a", reason="x")):
        with pytest.raises(ReconciliationError):
            gw.reconcile_call(lid, executed=True, **kw)
    assert guard.lease(lid)["state"] == "unknown"
    gw.reconcile_call(lid, executed=True, evidence=EVIDENCE, actor="ops@acme", reason="refund obj_1 is in the dashboard")
    with pytest.raises(ReconciliationConflict):
        gw.reconcile_call(lid, executed=False, evidence=EVIDENCE, actor="ops2@acme", reason="no, it is not in the dashboard")
    with pytest.raises(ReconciliationError):
        gw.reconcile_call("not-a-lease", executed=True, evidence=EVIDENCE, actor="a", reason="a good reason here")


def test_a_different_action_or_a_read_is_not_blocked_by_an_unknown_one():
    prov, gw, guard, gp = wire()
    prov.fail_next = True
    refund(gw, gp, key="k1")
    assert refund(gw, gp, key="k2", invoice="INV-2").status is GatewayStatus.OK        # another action: its own key
    assert gw.invoke(GatewayRequest(gp, "crm.read", {}, idempotency_key="k1")).status is GatewayStatus.OK
    assert prov.sends == 2


def test_an_ordinary_error_that_never_dispatched_does_not_block_a_retry():
    prov, gw, guard, gp = wire(max_calls=1)
    ok = refund(gw, gp, key="a")
    assert ok.status is GatewayStatus.OK
    over = refund(gw, gp, key="b")                                                     # over the grant's call limit: refused before any send
    assert over.status is not GatewayStatus.OK and prov.sends == 1
    assert gw.unresolved_calls("acme:agent") == []


def test_production_requires_an_idempotency_key_for_a_side_effecting_direct_call(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    prov, gw, guard, gp = wire(db=str(tmp_path / "a.db"))
    r = gw.invoke(GatewayRequest(gp, "billing.refund", {"invoice": "INV-1", "amount": 40}))
    assert r.status is GatewayStatus.DENIED and r.decision.reason == "authority:idempotency_key_required" and prov.sends == 0
    assert gw.invoke(GatewayRequest(gp, "crm.read", {})).status is GatewayStatus.OK        # reads need no key
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "development")
    assert gw.invoke(GatewayRequest(gp, "billing.refund", {"invoice": "INV-1", "amount": 40})).status is GatewayStatus.OK        # unchanged outside production


def test_the_same_key_on_a_different_capability_is_a_different_action():
    prov, gw, guard, gp = wire()
    prov.fail_next = True
    refund(gw, gp, key="shared")
    r = gw.invoke(GatewayRequest(gp, "billing.credit", {"invoice": "INV-1", "amount": 5}, idempotency_key="shared"))
    assert r.status is GatewayStatus.OK and prov.sends == 2

"""InboxApprovalStore: an approval is single-use, expires, and cannot be given by the requester (audit F2)."""
import pytest

from agentic_os.agent_gateway import (
    AgentGateway, ApprovalError, CapabilityKind, CapabilityManifest, GatewayPrincipal, GatewayRequest,
    GatewayStatus, HandlerResult, InboxApprovalStore, RiskTier)
from agentic_os.agent_gateway.registry import CapabilityRegistry
from agentic_os.overlays import Principal


class _Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


# NOT idempotent: the gateway's idempotency cache must not be what stops a replay
_CHARGE = CapabilityManifest("billing.charge", "charge", CapabilityKind.DIRECT, permissions=("billing.write",),
                             risk_tier=RiskTier.CONSEQUENTIAL, side_effecting=True, idempotent=False, provider="billing")


class _Handler:
    def __init__(self): self.calls = 0
    def __call__(self, req, env): self.calls += 1; return HandlerResult(ok=True, output={"charged": True})


def _gw(handler, inbox):
    reg = CapabilityRegistry().register(_CHARGE, handler)
    return AgentGateway(registry=reg, authorize=lambda p, perm: True, approvals=inbox)


def _req(gp, amount=100):
    return GatewayRequest(gp, "billing.charge", {"amount": amount})


GP = GatewayPrincipal(Principal("agent1", "service", (), "acme"))


def test_an_approval_runs_exactly_once_and_the_replay_parks_again():
    inbox, h = InboxApprovalStore(), _Handler()
    gw = _gw(h, inbox)
    assert gw.invoke(_req(GP)).status is GatewayStatus.PENDING_APPROVAL
    inbox.approve(inbox.pending()[0]["id"], by="alice")
    assert gw.invoke(_req(GP)).status is GatewayStatus.OK and h.calls == 1
    again = gw.invoke(_req(GP))                                     # the SAME approved call, replayed
    assert again.status is GatewayStatus.PENDING_APPROVAL and h.calls == 1
    assert len(inbox.pending()) == 1                                # it needs a new human decision


def test_approving_100_does_not_authorize_1000():
    inbox, h = InboxApprovalStore(), _Handler()
    gw = _gw(h, inbox)
    gw.invoke(_req(GP, 100))
    inbox.approve(inbox.pending()[0]["id"], by="alice")
    assert gw.invoke(_req(GP, 1000)).status is GatewayStatus.PENDING_APPROVAL and h.calls == 0


def test_the_requester_cannot_approve_its_own_request():
    inbox, h = InboxApprovalStore(), _Handler()
    _gw(h, inbox).invoke(_req(GP))
    with pytest.raises(ApprovalError):
        inbox.approve(inbox.pending()[0]["id"], by="agent1")
    with pytest.raises(ApprovalError):
        inbox.approve(inbox.pending()[0]["id"], by="")
    assert inbox.pending()[0]["status"] == "pending"


def test_an_approval_expires_and_the_request_is_raised_again():
    clock = _Clock()
    inbox, h = InboxApprovalStore(ttl=60, clock=clock), _Handler()
    gw = _gw(h, inbox)
    gw.invoke(_req(GP))
    inbox.approve(inbox.pending()[0]["id"], by="alice")
    clock.advance(61)
    assert gw.invoke(_req(GP)).status is GatewayStatus.PENDING_APPROVAL and h.calls == 0
    old, new = inbox.pending()[0], None
    assert old["status"] == "pending"                               # a fresh item, not the lapsed approval


def test_a_pending_item_that_lapsed_cannot_be_approved():
    clock = _Clock()
    inbox = InboxApprovalStore(ttl=60, clock=clock)
    _gw(_Handler(), inbox).invoke(_req(GP))
    item = inbox.pending()[0]["id"]
    clock.advance(120)
    with pytest.raises(ApprovalError):
        inbox.approve(item, by="alice")


def test_a_used_approval_cannot_be_decided_again():
    inbox, h = InboxApprovalStore(), _Handler()
    gw = _gw(h, inbox)
    gw.invoke(_req(GP))
    aid = inbox.pending()[0]["id"]
    inbox.approve(aid, by="alice")
    gw.invoke(_req(GP))
    with pytest.raises(ApprovalError):
        inbox.approve(aid, by="bob")
    with pytest.raises(ApprovalError):
        inbox.reject(aid, by="bob")


def test_concurrent_executions_spend_one_approval_exactly_once():
    """Many workers present the same approved call at once: the approval is consumed by exactly one."""
    import threading
    inbox, h = InboxApprovalStore(), _Handler()
    gw = _gw(h, inbox)
    gw.invoke(_req(GP))
    inbox.approve(inbox.pending()[0]["id"], by="alice")
    barrier, results = threading.Barrier(16), []

    def worker():
        barrier.wait()
        results.append(gw.invoke(_req(GP)).status)

    ts = [threading.Thread(target=worker) for _ in range(16)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(GatewayStatus.OK) == 1 and h.calls == 1
    assert results.count(GatewayStatus.PENDING_APPROVAL) == 15


def test_concurrent_approvals_of_one_item_decide_it_once():
    import threading
    inbox = InboxApprovalStore()
    _gw(_Handler(), inbox).invoke(_req(GP))
    aid = inbox.pending()[0]["id"]
    barrier, outcomes = threading.Barrier(8), []

    def worker(i):
        barrier.wait()
        try:
            inbox.approve(aid, by=f"approver{i}")
            outcomes.append("approved")
        except ApprovalError:
            outcomes.append("refused")

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert outcomes.count("approved") == 1 and outcomes.count("refused") == 7

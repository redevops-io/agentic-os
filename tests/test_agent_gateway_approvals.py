"""Approval inbox + undo window — Phase 5 (plan §11)."""
from __future__ import annotations

import pytest

from agentic_os.overlays import Principal
from agentic_os.agent_gateway import (
    AgentGateway, CapabilityKind, CapabilityManifest, GatewayPrincipal, GatewayRequest,
    GatewayStatus, HandlerResult, InboxApprovalStore, RiskTier, UndoWindow)
from agentic_os.agent_gateway.registry import CapabilityRegistry


class _Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


_WRITE = CapabilityManifest("crm.request_outreach_send", "send", CapabilityKind.DIRECT,
                            permissions=("crm.write",), risk_tier=RiskTier.CONSEQUENTIAL,
                            side_effecting=True, idempotent=True, provider="crm")


class _Handler:
    def __init__(self): self.calls = 0
    def __call__(self, req, env): self.calls += 1; return HandlerResult(ok=True, output={"sent": True})


def _gw(handler, approvals):
    reg = CapabilityRegistry().register(_WRITE, handler)
    return AgentGateway(registry=reg, authorize=lambda p, perm: perm == "crm.write",
                        approvals=approvals)


def _req(gp, args):
    return GatewayRequest(gp, "crm.request_outreach_send", args)


# ── the inbox loop ──────────────────────────────────────────────────────────────
def test_gated_request_parks_in_the_inbox_then_runs_when_approved():
    gp = GatewayPrincipal(Principal("agent", "service", (), "acme"))
    inbox, handler = InboxApprovalStore(), _Handler()
    gw = _gw(handler, inbox)

    first = gw.invoke(_req(gp, {"contact": "T"}))
    assert first.status is GatewayStatus.PENDING_APPROVAL and handler.calls == 0
    pend = inbox.pending()
    assert len(pend) == 1 and pend[0]["capability"] == "crm.request_outreach_send"

    inbox.approve(pend[0]["id"], by="alice")
    second = gw.invoke(_req(gp, {"contact": "T"}))          # identical retry
    assert second.status is GatewayStatus.OK and handler.calls == 1
    assert inbox.pending() == []                            # cleared from the inbox


def test_rejected_request_never_executes():
    gp = GatewayPrincipal(Principal("agent", "service", (), "acme"))
    inbox, handler = InboxApprovalStore(), _Handler()
    gw = _gw(handler, inbox)
    gw.invoke(_req(gp, {"contact": "T"}))
    inbox.reject(inbox.pending()[0]["id"], by="alice")
    r = gw.invoke(_req(gp, {"contact": "T"}))
    assert r.status is GatewayStatus.PENDING_APPROVAL and handler.calls == 0


def test_approval_is_bound_to_the_exact_request():
    gp = GatewayPrincipal(Principal("agent", "service", (), "acme"))
    inbox, handler = InboxApprovalStore(), _Handler()
    gw = _gw(handler, inbox)
    gw.invoke(_req(gp, {"contact": "Tasha"}))               # request A
    inbox.approve(inbox.pending()[0]["id"], by="alice")     # approve A
    other = gw.invoke(_req(gp, {"contact": "Bob"}))         # request B — different args
    assert other.status is GatewayStatus.PENDING_APPROVAL and handler.calls == 0   # B not authorized


# ── undo window ─────────────────────────────────────────────────────────────────
def test_undo_runs_the_compensation_within_the_window():
    clk = _Clock()
    win = UndoWindow(default_ttl=60, clock=clk)
    undone = []
    eid = win.record("crm.request_outreach_send", "crm.retract_outreach",
                     lambda: undone.append(True), by="alice")
    assert win.available()[0]["capability"] == "crm.request_outreach_send"
    win.undo(eid, by="alice")
    assert undone == [True]
    with pytest.raises(ValueError):
        win.undo(eid, by="alice")                            # already undone


def test_undo_refused_after_the_window_expires():
    clk = _Clock()
    win = UndoWindow(default_ttl=60, clock=clk)
    eid = win.record("x", "x.undo", lambda: 1)
    clk.advance(120)
    assert win.available() == []
    with pytest.raises(ValueError):
        win.undo(eid)

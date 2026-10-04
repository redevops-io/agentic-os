"""Experiment C (§40.C) — Slack approval → ERP/billing action, on synthetic data.

Proves: a consequential ERP write is gated by a durable approval posted to a chat surface; on approve it
executes and is VERIFIED by read-back (receipt); on reject nothing executes; a silent ERP failure after
approval is caught. The approval surface binds to the common CollaborationProvider contract (swappable).
"""
from __future__ import annotations

from agentic_os.collaboration import InteractionResult, OutboundMessage
from agentic_os.integration import InMemoryIntegrationProvider
from agentic_os.integration.contracts import ExceptionCategory
from agentic_os.integration.experiments.slack_approval import ActionSpec, run_slack_approval


class _FakeCollab:
    """A minimal CollaborationProvider that records approval prompts + messages."""
    provider = "slack"

    def __init__(self):
        self.prompts = []
        self.sent = []

    def entitled(self):
        return True

    def open_interaction(self, channel, prompt, *, actions=()):
        self.prompts.append((channel, prompt, actions))
        return InteractionResult(ok=True, message_id="appr-msg-1")

    def post_message(self, msg: OutboundMessage):
        self.sent.append(msg.text)
        return "mid"


def _erp():
    return InMemoryIntegrationProvider("erp", capabilities=("accounting.journal.create", "object.read"))


def _credit_note_spec():
    return ActionSpec(object_type="credit_note", external_id="cn_42",
                      fields={"customer": "cus_acme", "amount": 5000, "currency": "usd", "reason": "goodwill"},
                      verify_fields={"customer": "cus_acme", "amount": 5000})


def test_approved_action_executes_and_is_verified():
    collab, erp = _FakeCollab(), _erp()
    out = run_slack_approval(collab, erp, _credit_note_spec(), channel="C-finance", decide=lambda a: "approve")
    assert out.posted and collab.prompts[0][2] == ("Approve", "Reject")   # approval prompt was posted
    assert out.approval.status == "approved" and out.executed
    # the ERP object exists AND the receipt proves the observed state (not just a 200)
    cn = erp.read_object("credit_note", "cn_42")
    assert cn is not None and cn.normalized_fields["amount"] == 5000
    assert out.discharge.receipt.observed_state["credit_note"]["customer"] == "cus_acme"
    assert any("Done" in m for m in collab.sent)


def test_rejected_action_executes_nothing():
    collab, erp = _FakeCollab(), _erp()
    out = run_slack_approval(collab, erp, _credit_note_spec(), channel="C-finance", decide=lambda a: "reject")
    assert out.approval.status == "rejected" and not out.executed and out.discharge is None
    assert erp.read_object("credit_note", "cn_42") is None          # nothing written without approval
    assert any("Rejected" in m for m in collab.sent)


def test_silent_erp_failure_after_approval_is_caught():
    collab = _FakeCollab()
    erp = InMemoryIntegrationProvider("erp", drop_writes=True)      # the write 200s but nothing lands
    out = run_slack_approval(collab, erp, _credit_note_spec(), channel="C-finance", decide=lambda a: "approve")
    assert out.approval.status == "approved" and not out.executed
    assert out.exception and out.exception.category == ExceptionCategory.OBLIGATION_UNSATISFIED
    assert any("not verified" in m for m in collab.sent)


def test_authority_is_recorded_on_the_approval_and_receipt():
    collab, erp = _FakeCollab(), _erp()
    out = run_slack_approval(collab, erp, _credit_note_spec(), channel="C-finance",
                             decide=lambda a: "approve", authority_required="finance.controller")
    assert out.approval.authority_required == "finance.controller"
    assert out.discharge.receipt.authority == "finance.controller"

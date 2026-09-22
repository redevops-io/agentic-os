"""Phase C — cross-app Receivables Mission over canonical contracts.

Demonstrates the plan's differentiation acceptance (§31) in canonical form: cross-system evidence
(receivables + CRM + correspondence) → explicit proposal → exact-content approval → governed send →
ActionReceipt → independent verification.
"""
from __future__ import annotations

from agentic_os.integrations.execution import MissionRun, StepRun
from agentic_os.integrations.business import (
    Contact, Message, Provenance, Receivable, VerificationState, canonical_evidence,
    investigate_receivables, propose_followups, receipts_for_run)


def _evidence():
    # Two overdue receivables (one large+old, one small), a CRM contact for each, and prior email.
    return [
        Receivable(prov=Provenance("quickbooks", "cust-acme", known_at=10),
                   invoice_ref="INV-1", customer_ref="cust-acme", amount_outstanding_cents=500000,
                   currency="usd", days_overdue=75),
        Receivable(prov=Provenance("quickbooks", "cust-small", known_at=10),
                   invoice_ref="INV-2", customer_ref="cust-small", amount_outstanding_cents=9000,
                   currency="usd", days_overdue=10),
        Contact(prov=Provenance("hubspot", "cust-acme"), email="ap@acme.com", first_name="Dana"),
        Contact(prov=Provenance("hubspot", "cust-small"), email="jo@small.co", first_name="Jo"),
        Message(prov=Provenance("gmail", "m1", known_at=5), channel="email", thread_ref="cust-acme",
                direction="inbound", from_ref="ap@acme.com", snippet="we'll pay next week"),
    ]


# ── investigation: cross-system, ranked, explainable ─────────────────────────────────
def test_investigate_ranks_material_accounts_with_linked_evidence():
    cands = investigate_receivables(_evidence())
    assert [c.customer_key for c in cands] == ["cust-acme", "cust-small"]   # ranked by materiality
    acme = cands[0]
    assert acme.contact is not None and acme.contact.email == "ap@acme.com"  # CRM linked
    assert acme.last_message is not None and acme.last_message.snippet == "we'll pay next week"  # comms linked
    # materiality = outstanding * days_overdue, and the reasons cite the actual evidence
    assert acme.materiality_cents == 500000 * 75
    assert any("overdue" in r for r in acme.reasons) and "prior correspondence exists" in acme.reasons
    # evidence provenance: the candidate carries digests of every object it reasoned over (§31)
    assert len(acme.evidence_refs) == 3


def test_material_threshold_filters_immaterial_accounts():
    cands = investigate_receivables(_evidence(), material_threshold_cents=1_000_000)
    assert [c.customer_key for c in cands] == ["cust-acme"]      # the small one drops out


def test_unlinked_receivable_is_surfaced_not_silently_dropped():
    ev = [Receivable(prov=Provenance("xero", "cust-x"), invoice_ref="INV-9", customer_ref="cust-x",
                     amount_outstanding_cents=40000, currency="usd", days_overdue=30)]
    cands = investigate_receivables(ev)
    assert len(cands) == 1 and cands[0].contact is None
    assert "no CRM contact linked — needs identity resolution" in cands[0].reasons


# ── proposal: drafted, not sent; approval binds exact content ────────────────────────
def test_propose_drafts_are_approval_gated_and_content_bound():
    cands = investigate_receivables(_evidence())
    proposals = propose_followups(cands)
    assert len(proposals) == 2 and all(p.capability == "email.message.send" for p in proposals)
    acme = next(p for p in proposals if p.to_ref == "ap@acme.com")
    assert acme.subject.startswith("Overdue balance")            # escalated tone (75 days)
    # editing the draft body changes the content digest → prior approval would be invalidated (§22/§7)
    from agentic_os.integrations.business import FollowupProposal
    edited = FollowupProposal(candidate=acme.candidate, channel="email", to_ref=acme.to_ref,
                              subject=acme.subject, body=acme.body + " P.S. thanks!")
    assert edited.content_digest() != acme.content_digest()


def test_unreachable_candidate_yields_a_finding_not_a_send():
    ev = [Receivable(prov=Provenance("xero", "cust-x"), invoice_ref="INV-9", customer_ref="cust-x",
                     amount_outstanding_cents=40000, currency="usd", days_overdue=30)]
    cands = investigate_receivables(ev)
    assert propose_followups(cands) == ()       # no contact → no draft; it stays a surfaced finding


# ── governed execution: the approved send → ActionReceipt + verification ─────────────
def test_approved_followup_send_produces_receipt_and_verification():
    cands = investigate_receivables(_evidence())
    proposal = next(p for p in propose_followups(cands) if p.to_ref == "ap@acme.com")
    # the runner executes the approved send (gmail) and reconciles it; model the governed write outcome:
    sent = StepRun(capability=proposal.capability, provider="gmail", tier=3, ok=True,
                   provider_object_id="MSG-out-1", reconciled=True, write=True,
                   data={"id": "MSG-out-1", "threadId": "cust-acme", "labelIds": ["SENT"],
                         "snippet": proposal.subject})
    run = MissionRun("ih-receivables", (sent,))
    receipts = receipts_for_run(run, decision_ids={proposal.capability: "dec-approve-1"})
    assert len(receipts) == 1
    r = receipts[0]
    assert r.receipt.status == "SUCCEEDED" and r.verification is VerificationState.VERIFIED
    assert r.receipt.decision_id == "dec-approve-1" and r.receipt.provider == "gmail"
    # the sent message is also captured as canonical outcome evidence
    assert any(isinstance(o, Message) and o.direction == "outbound" for o in canonical_evidence(run))

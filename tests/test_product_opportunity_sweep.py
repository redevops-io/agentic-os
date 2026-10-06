"""Product Opportunity Intelligence Phase 6 — monthly sweep + brief.

Proves: a batch of social observations runs the whole pipeline (extract→cluster→map→score→rank) into a
SweepResult; the diff vs the previous sweep flags NEW and ACCELERATING opportunities; and the markdown brief
leads with what changed and renders ranked opportunities with coverage + gap + priority.
"""
from __future__ import annotations

from agentic_os.agent_gateway.social.contracts import SocialObservation
from agentic_os.product_opportunity import diff_sweeps, render_brief, run_sweep


def _o(text, sid, author, published=1000):
    return SocialObservation(provider="reddit", source_type="post", source_ref=sid, text=text, author_ref=author,
                             observed_at=2000, published_at=published, retrieval_method="reddit-api",
                             evidence_ref="ev:" + sid, provider_fields={"community": "r/x"})


def _corpus_month1():
    return [
        _o("I manually reconcile Stripe payouts into QuickBooks every week by hand.", "a", "u1"),
        _o("Matching Stripe deposits to QuickBooks invoices is a manual nightmare, hours lost.", "b", "u2"),
        _o("We manually copy competitor prices from Amazon into a spreadsheet to compare daily.", "c", "u3"),
    ]


def test_run_sweep_pipeline():
    s = run_sweep(_corpus_month1())
    assert s.raw_observations == 3 and s.extracted_pains == 3
    ids = {o.opportunity_id for o in s.opportunities}
    assert any("stripe" in i and "quickbooks" in i for i in ids)
    # ranked by composite, descending
    comps = [o.score.composite for o in s.opportunities]
    assert comps == sorted(comps, reverse=True) and s.capability_leverage


def test_diff_flags_new_and_accelerating():
    m1 = run_sweep(_corpus_month1())
    m2 = run_sweep(_corpus_month1() + [
        # stripe↔quickbooks gains 2 more independent operators (accelerating); a brand-new pain appears
        _o("How do you reconcile Stripe fees when posting payouts to QuickBooks? we do it manually.", "d", "u4"),
        _o("Our bookkeeper spends hours matching Stripe payouts against QuickBooks monthly.", "e", "u5"),
        _o("We manually re-key Zendesk tickets into Jira to escalate to engineering every day.", "f", "u6"),
    ])
    changes = {c.opportunity_id: c for c in diff_sweeps(m1, m2)}
    sq = next(c for i, c in changes.items() if "stripe" in i and "quickbooks" in i)
    assert sq.status == "ACCELERATING" and sq.evidence_delta >= 2
    assert any(c.status == "NEW" and "jira" in i for i, c in changes.items())


def test_brief_leads_with_what_changed():
    m1 = run_sweep(_corpus_month1())
    m2 = run_sweep(_corpus_month1() + [
        _o("We manually re-key Zendesk tickets into Jira to escalate to engineering every day.", "f", "u6")])
    brief = render_brief(m2, previous=m1)
    assert brief.startswith("# Product Opportunity Sweep")
    assert "## What changed" in brief and "## Top opportunities" in brief
    assert "🆕" in brief or "New:" in brief                  # the new zendesk↔jira opportunity surfaces
    assert "Capability leverage" in brief

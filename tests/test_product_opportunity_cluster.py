"""Product Opportunity Intelligence Phase 3 — WorkflowPain clustering + recurrence (with a backtest).

Proves: differently-worded complaints over the same app-pair cluster into one WorkflowPain; genuinely different
workflows stay apart; recurrence uses INDEPENDENT evidence; and the clusterer independently recovers the known
manually-identified integration-pain categories (the plan's §31 backtest).
"""
from __future__ import annotations

from agentic_os.agent_gateway.social.contracts import SocialObservation
from agentic_os.product_opportunity import cluster_pains, extract_pain, workflow_signature


def _pain(text, *, sid, author, published=1000):
    o = SocialObservation(provider="reddit", source_type="post", source_ref=sid, text=text, author_ref=author,
                          observed_at=2000, published_at=published, retrieval_method="reddit-api",
                          evidence_ref="ev:" + sid, provider_fields={"community": "r/x"})
    return extract_pain(o)


def test_same_app_pair_different_wording_clusters():
    ps = [
        _pain("Stripe deposits never match the invoices in QuickBooks, I reconcile by hand every week.",
              sid="a", author="u1"),
        _pain("How do you reconcile Stripe fees when posting payouts to QuickBooks? we do it manually.",
              sid="b", author="u2"),
        _pain("Our bookkeeper spends hours matching Stripe payouts against QuickBooks every month.",
              sid="c", author="u3"),
    ]
    clusters = cluster_pains([p for p in ps if p])
    assert len(clusters) == 1                                 # one workflow: stripe↔quickbooks reconciliation
    c = clusters[0]
    assert set(c.applications) == {"stripe", "quickbooks"} and c.independent_evidence_count == 3 and c.recurs
    assert c.workflow_pain.agentic_fit == "high"              # cross-app + a decision (reconcile/match)


def test_different_workflows_stay_apart():
    ps = [
        _pain("I manually copy orders from Shopify into NetSuite every day.", sid="a", author="u1"),
        _pain("We reconcile Stripe payouts against QuickBooks manually each week.", sid="b", author="u2"),
    ]
    clusters = cluster_pains([p for p in ps if p])
    assert len(clusters) == 2 and {c.signature for c in clusters} == {"netsuite|shopify", "quickbooks|stripe"}


def test_recurrence_needs_independent_evidence():
    # three reposts by different-looking but same-author accounts collapse → not recurring
    ps = [
        _pain("Zapier keeps breaking when I sync Stripe and QuickBooks every day.", sid="v1", author="same"),
        _pain("Zapier keeps breaking when I sync Stripe and QuickBooks every day.", sid="v2", author="same"),
    ]
    clusters = cluster_pains([p for p in ps if p])
    assert clusters[0].independent_evidence_count == 1 and not clusters[0].recurs


def test_backtest_recovers_known_pain_categories():
    # feed observations for several KNOWN manually-identified integration pains; expect them recovered distinctly
    corpus = [
        _pain("I manually reconcile Stripe payouts into QuickBooks every week by hand.", sid="p1", author="a"),
        _pain("Matching Stripe deposits to QuickBooks invoices is a manual nightmare.", sid="p2", author="b"),
        _pain("Every support ticket in Zendesk I manually copy context from Salesforce.", sid="s1", author="c"),
        _pain("We manually re-key Zendesk tickets into Jira to escalate to engineering.", sid="s2", author="d"),
        _pain("Offboarding: I manually revoke each app in Okta when someone leaves, every time.", sid="o1", author="e"),
        _pain("We manually copy competitor prices from Amazon into a spreadsheet to compare daily.", sid="m1", author="f"),
    ]
    clusters = cluster_pains([p for p in corpus if p])
    sigs = {c.signature for c in clusters}
    assert "quickbooks|stripe" in sigs                        # payment↔accounting reconciliation
    assert any("zendesk" in s and ("salesforce" in s or "jira" in s) for s in sigs)  # support↔crm/eng
    assert any("amazon" in s for s in sigs)                   # marketplace price monitoring
    assert len(clusters) >= 4

"""Per-app wiring (§4.1/§7), historical import (WP7), and the provider evaluation harness (§9). All offline."""
from __future__ import annotations

import pytest

from runtime_contracts.protocol import Capability, EvidenceValueRecord, Sensitivity

from agentic_os.intelligence import (
    EvidenceValueStore, HistoricalOutcome, app_capabilities, evaluate, from_intercom, from_klaviyo,
    from_zendesk, report, request_for, seed_value_store,
)
from agentic_os.intelligence.evaluation import DROP, INSUFFICIENT_DATA, NOT_APPLICABLE, RETAIN, REVIEW


# ── per-app capability wiring (§4.1, §7) ─────────────────────────────────────────────────────────────────
def test_request_for_tags_purpose_and_sensitivity():
    req = request_for("twenty", Capability.PERSON_ENRICHMENT,
                      decision_case_id="dc1", subject_refs=("jane@acme.com",), tenant="t1")
    assert req.capability == Capability.PERSON_ENRICHMENT
    assert req.purpose == "crm contact enrichment"        # from the app profile
    assert req.sensitivity == Sensitivity.PII             # PII capability
    assert req.tenant == "t1"


def test_non_pii_capability_is_public():
    req = request_for("umami", Capability.WEB_TRAFFIC_INTELLIGENCE,
                      decision_case_id="dc", subject_refs=("acme.com",), tenant="t")
    assert req.sensitivity == Sensitivity.PUBLIC


def test_app_cannot_request_capability_outside_its_remit():
    # Umami has no business asking for payment fraud scores.
    with pytest.raises(ValueError):
        request_for("umami", Capability.PAYMENT_FRAUD_SCORE,
                    decision_case_id="dc", subject_refs=("x",), tenant="t")


def test_unknown_app_is_rejected():
    with pytest.raises(ValueError):
        app_capabilities("not_an_app")


def test_matrix_entitlements_hold():
    assert Capability.PAYMENT_FRAUD_SCORE in app_capabilities("lago")
    assert Capability.SANCTIONS_RISK in app_capabilities("erpnext")
    assert Capability.THREAT_INTELLIGENCE in app_capabilities("crowdsec")
    assert Capability.VULNERABILITY_EXPLOITABILITY in app_capabilities("openscap")


def test_purpose_override_wins():
    req = request_for("lago", Capability.PAYMENT_FRAUD_SCORE, decision_case_id="d",
                      subject_refs=("ch_1",), tenant="t", purpose="dunning step 3")
    assert req.purpose == "dunning step 3"


# ── historical outcome migration (WP7) ───────────────────────────────────────────────────────────────────
def test_zendesk_import_maps_csat_and_status():
    rows = from_zendesk([
        {"id": 1, "status": "solved", "satisfaction_rating": {"score": "good"}, "tags": ["billing"]},
        {"id": 2, "status": "closed", "satisfaction_rating": {"score": "bad"}},
        {"id": 3, "status": "open"},
    ], tenant="t")
    assert [o.outcome for o in rows] == ["beneficial", "adverse", "neutral"]
    assert rows[0].decision == "billing" and rows[0].action == "resolved"
    assert rows[0].source == "zendesk"


def test_intercom_import_maps_rating():
    rows = from_intercom([
        {"id": "a", "state": "closed", "conversation_rating": {"rating": 5}},
        {"id": "b", "state": "closed", "conversation_rating": {"rating": 1}},
        {"id": "c", "state": "closed"},                      # no rating → closed = beneficial
        {"id": "d", "state": "open"},
    ], tenant="t")
    assert [o.outcome for o in rows] == ["beneficial", "adverse", "beneficial", "neutral"]


def test_klaviyo_import_maps_metric():
    rows = from_klaviyo([
        {"id": "1", "metric": "Placed Order", "profile": {"email": "a@x.com"}},
        {"id": "2", "metric": "Unsubscribed", "profile": {"email": "b@x.com"}},
        {"id": "3", "metric": "Viewed Product"},
    ], tenant="t")
    assert [o.outcome for o in rows] == ["beneficial", "adverse", "neutral"]
    assert rows[0].subject == "a@x.com"


def test_seed_value_store_preserves_experience_without_polluting_paid_metrics(tmp_path):
    store = EvidenceValueStore(str(tmp_path / "vs.jsonl"))
    outcomes = from_zendesk([{"id": 1, "status": "solved", "satisfaction_rating": {"score": "good"}}], tenant="t")
    assert seed_value_store(store, outcomes) == 1
    rec = store.records()[0]
    assert rec.provider == "import:zendesk" and rec.cost == 0.0 and rec.evidence_requested is False
    assert rec.verified_outcome == "beneficial"
    # imported rows are NOT_APPLICABLE for paid verdicts.
    assert evaluate(store)[0].verdict() == NOT_APPLICABLE


# ── provider evaluation harness (§9) ─────────────────────────────────────────────────────────────────────
def _rec(provider, *, received=True, changed=True, cost=1.0, outcome="beneficial", i=0):
    return EvidenceValueRecord(
        decision_case_id=f"dc{provider}{i}", capability=Capability.PERSON_ENRICHMENT, provider=provider,
        evidence_requested=True, evidence_received=received, cost=cost,
        decision_before="A", decision_after=("B" if changed else "A"), action="act", verified_outcome=outcome)


def test_evaluate_computes_paid_rule_metrics(tmp_path):
    store = EvidenceValueStore(str(tmp_path / "vs.jsonl"))
    for i in range(6):
        store.append(_rec("apollo", cost=0.5, outcome="beneficial", i=i))
    e = evaluate(store)[0]
    assert e.provider == "apollo" and e.acquired == 6 and e.changed == 6
    assert e.cost_per_acquired == pytest.approx(0.5)
    assert e.cost_per_verified_beneficial == pytest.approx(0.5)
    assert e.verdict() == RETAIN


def test_verdict_drop_when_no_beneficial_impact(tmp_path):
    store = EvidenceValueStore(str(tmp_path / "vs.jsonl"))
    for i in range(6):
        store.append(_rec("pricey", cost=2.0, changed=True, outcome="adverse", i=i))
    e = evaluate(store)[0]
    assert e.verified_beneficial == 0 and e.verdict() == DROP


def test_verdict_review_when_too_expensive(tmp_path):
    store = EvidenceValueStore(str(tmp_path / "vs.jsonl"))
    # only 1 of 6 changed decisions is beneficial → cost_per_beneficial = 6*2 / 1 = 12 > 5
    for i in range(6):
        store.append(_rec("dnb", cost=2.0, outcome=("beneficial" if i == 0 else "neutral"), i=i))
    e = evaluate(store)[0]
    assert e.verified_beneficial == 1 and e.verdict(max_cost_per_beneficial=5.0) == REVIEW


def test_verdict_insufficient_data_holds(tmp_path):
    store = EvidenceValueStore(str(tmp_path / "vs.jsonl"))
    for i in range(3):
        store.append(_rec("newprovider", cost=1.0, i=i))
    assert evaluate(store)[0].verdict(min_resolved=5) == INSUFFICIENT_DATA


def test_report_renders_table(tmp_path):
    store = EvidenceValueStore(str(tmp_path / "vs.jsonl"))
    for i in range(6):
        store.append(_rec("apollo", cost=0.5, i=i))
    txt = report(store)
    assert "apollo" in txt and "verdict" in txt and RETAIN in txt

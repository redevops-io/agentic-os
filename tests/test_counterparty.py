"""Counterparty intelligence: composition of the register adapters with an explicit coverage statement."""
from __future__ import annotations

from agentic_os.intelligence.adapters.gleif import GleifProvider
from agentic_os.intelligence.adapters.opensanctions import OpenSanctionsProvider
from agentic_os.intelligence.counterparty import (
    build_counterparty_operator, counterparty_profile, normalize_legal_name,
)


def _lei(lei, name, jur="DE"):
    return {"attributes": {"lei": lei, "entity": {"legalName": {"name": name}, "status": "ACTIVE",
                                                   "jurisdiction": jur}, "registration": {"status": "ISSUED"}}}


def _gleif(*recs, status=200):
    return GleifProvider(fetch=lambda url, headers: (status, {"data": list(recs)}))


def _sanctions(results, status=200, key="k"):
    return OpenSanctionsProvider(api_key=key, fetch=lambda url, headers: (status, {"results": results}))


def _hit(score, caption="Bad Actor GmbH"):
    return {"id": "NK-1", "caption": caption, "schema": "Company", "score": score, "datasets": ["eu_fsf"],
            "properties": {"topics": ["sanction"]}}


def test_legal_name_normalisation_separates_identity_from_similarity():
    assert normalize_legal_name("Apple Inc.") == normalize_legal_name("APPLE INCORPORATED")
    assert normalize_legal_name("Apple Inc.") != normalize_legal_name("Apple Ford, Inc.")


def test_clean_only_when_identity_resolved_and_screening_ran():
    r = counterparty_profile("Siemens Aktiengesellschaft", tenant="t",
                             providers=[_gleif(_lei("W38", "Siemens Aktiengesellschaft"),
                                               _lei("529", "Siemens Aktiengesellschaft Österreich", "AT")),
                                        _sanctions([])])
    assert r["assessment"] == "no_adverse_findings"
    assert r["identity"]["status"] == "found" and r["identity"]["matched"][0]["lei"] == "W38"
    assert r["sanctions"]["screened"] is True and r["sanctions"]["hits"] == 0
    assert r["coverage_gaps"] == ["ownership: not_checked (no provider for beneficial_ownership)"]
    src = r["identity"]["sources"][0]
    assert src["provider"] == "gleif" and src["refs"][0] == "gleif:lei:W38" and src["response_digest"]
    assert src["license_scope"] == "GLEIF (CC0)"


def test_unscreened_counterparty_is_never_reported_clean():
    r = counterparty_profile("Siemens Aktiengesellschaft", tenant="t",
                             providers=[_gleif(_lei("W38", "Siemens Aktiengesellschaft")), _sanctions([], key="")])
    assert r["assessment"] == "incomplete"
    assert r["sanctions"]["status"] == "not_checked" and "screened" not in r["sanctions"]
    assert r["sanctions"]["skipped"][0]["provider"] == "opensanctions"
    assert any(g.startswith("sanctions: not_checked") for g in r["coverage_gaps"])


def test_failed_screening_is_unavailable_not_clean():
    r = counterparty_profile("Siemens Aktiengesellschaft", tenant="t",
                             providers=[_gleif(_lei("W38", "Siemens Aktiengesellschaft")), _sanctions([], status=503)])
    assert r["assessment"] == "incomplete" and r["sanctions"]["status"] == "unavailable"
    assert r["sanctions"]["failures"][0]["failure"] == "unavailable"


def test_sanctions_hit_goes_to_review_and_outranks_everything():
    r = counterparty_profile("Bad Actor GmbH", tenant="t",
                             providers=[_gleif(_lei("X1", "Bad Actor GmbH")), _sanctions([_hit(0.93)])])
    assert r["assessment"] == "sanctions_review_required"
    assert r["sanctions"]["review_required"][0]["id"] == "NK-1"
    assert r["sanctions"]["sources"][0]["refs"] == ["opensanctions:NK-1"]


def test_weak_screening_score_is_recorded_but_not_escalated():
    r = counterparty_profile("Bad Actor GmbH", tenant="t",
                             providers=[_gleif(_lei("X1", "Bad Actor GmbH")), _sanctions([_hit(0.31, "Badr Aktor")])])
    assert r["assessment"] == "no_adverse_findings"
    assert r["sanctions"]["hits"] == 1 and r["sanctions"]["review_required"] == []


def test_similar_names_are_ambiguous_not_an_identity():
    r = counterparty_profile("Acme", tenant="t",
                             providers=[_gleif(_lei("A", "ACME BUILDERS"), _lei("B", "ACME FOODS")), _sanctions([])])
    assert r["identity"]["status"] == "ambiguous" and r["assessment"] == "identity_unresolved"
    assert len(r["identity"]["records"]) == 2 and "matched" not in r["identity"]


def test_two_entities_with_the_same_legal_name_are_ambiguous():
    r = counterparty_profile("Acme Ltd", tenant="t",
                             providers=[_gleif(_lei("A", "ACME LIMITED", "GB"), _lei("B", "Acme Ltd.", "IE")),
                                        _sanctions([])])
    assert r["identity"]["status"] == "ambiguous" and r["identity"]["exact_name_matches"] == 2


def test_unknown_entity_is_no_match():
    r = counterparty_profile("Zzqx Holdings", tenant="t", providers=[_gleif(), _sanctions([])])
    assert r["identity"]["status"] == "no_match" and r["assessment"] == "identity_unresolved"


def test_register_outage_is_reported_as_unavailable():
    r = counterparty_profile("Siemens AG", tenant="t", providers=[_gleif(status=503), _sanctions([])])
    assert r["identity"]["status"] == "unavailable" and r["assessment"] == "incomplete"


def test_name_and_tenant_are_required():
    assert counterparty_profile(" ", tenant="t", providers=[])["status"] == "error"
    assert counterparty_profile("X", tenant="", providers=[])["status"] == "error"


def test_operator_capability_is_read_only():
    op = build_counterparty_operator(providers=[_gleif(_lei("W38", "Siemens Aktiengesellschaft")), _sanctions([])],
                                     tenant="t")
    spec = op.manifest.capabilities[0]
    assert spec.name == "counterparty.profile" and not spec.side_effecting and not spec.approval_required
    res = op.invoke("counterparty.profile", {"name": "Siemens Aktiengesellschaft"})
    assert res["counterparty_profiled"] is True and res["assessment"] == "no_adverse_findings"

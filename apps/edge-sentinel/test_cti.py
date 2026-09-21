"""Phase 2 acceptance: seed IOC → evidence-linked STIX graph → ATT&CK mapping → internal impact,
with every derived edge carrying provenance. Chains onto the Phase 1 evidence spine.
"""
from __future__ import annotations

import importlib

stix = importlib.import_module("edge-sentinel.stix")
attack = importlib.import_module("edge-sentinel.attack")
cti = importlib.import_module("edge-sentinel.cti")
incident = importlib.import_module("edge-sentinel.incident")
case_store = importlib.import_module("edge-sentinel.case_store")

normalize_ioc = stix.normalize_ioc
classify_ioc = stix.classify_ioc
CtiGraph = cti.CtiGraph
seed_ioc = cti.seed_ioc
correlate_internal = cti.correlate_internal
ingest_bundle = cti.ingest_bundle
OfflineTaxiiSource = cti.OfflineTaxiiSource
ingest_crowdsec_alert = incident.ingest_crowdsec_alert
CaseStore = case_store.CaseStore

ALERT = {
    "scenario": "crowdsecurity/ssh-bf",
    "source": {"value": "203.0.113.7", "scope": "Ip"},
    "events_count": 12, "created_at": "2026-09-21T10:00:00Z",
}


def _all_edges_provenanced(graph) -> bool:
    return all(r.provenance and r.provenance.source and r.provenance.method
               for r in graph.relationships.values())


def test_the_full_seed_to_internal_impact_path():
    """The headline Phase 2 acceptance."""
    # Phase 1: an internal observation of the same IP, backed by immutable evidence
    store = CaseStore()
    case = ingest_crowdsec_alert(store, ALERT)
    obs = store.observations[case.observation_refs[0]]
    ev_ref = case.evidence_refs[0]

    # Phase 2: seed the IOC + map ATT&CK from the scenario
    g = CtiGraph()
    indicator = seed_ioc(g, "203.0.113.7", scenario=ALERT["scenario"], evidence_refs=(ev_ref,))

    # graph has the SCO, the indicator, and the ATT&CK technique(s) for ssh brute force
    aps = g.objects_of_type("attack-pattern")
    tech_ids = {ap.props["external_references"][0]["external_id"] for ap in aps}
    assert "T1110" in tech_ids                                   # Brute Force
    assert aps[0].props["x_mitre_version"].startswith("ATT&CK")  # version identity recorded
    assert any(r.relationship_type == "indicates" for r in g.relationships.values())  # indicator→technique

    # internal correlation → impact
    impacts = correlate_internal(g, [obs])
    assert len(impacts) == 1
    imp = impacts[0]
    assert imp.ioc_value == "203.0.113.7" and imp.observation_id == obs.id
    assert imp.indicator_id == indicator.id
    assert imp.evidence_refs == (ev_ref,)                        # traces to Phase 1 evidence
    assert any(r.relationship_type == "sighted-in" for r in g.relationships.values())

    # ACCEPTANCE: every derived edge carries provenance, and the sighting traces to evidence
    assert _all_edges_provenanced(g)
    sighting = next(r for r in g.relationships.values() if r.relationship_type == "sighted-in")
    assert sighting.provenance.evidence_refs == (ev_ref,)


def test_ioc_classification_and_defanging():
    assert classify_ioc("1.2.3.4") == ("ipv4-addr", "ipv4")
    assert classify_ioc("evil.example.com") == ("domain-name", "domain")
    assert classify_ioc("http://evil.example.com/x") == ("url", "url")
    assert classify_ioc("a" * 64) == ("file", "sha256")
    # defanged input canonicalizes: hxxp + [.]
    sco, ind = normalize_ioc("hxxp://evil[.]example[.]com/x")
    assert sco.props["value"] == "http://evil.example.com/x"
    assert ind.props["pattern"].startswith("[url:value =")


def test_ioc_normalization_is_deterministic():
    a1, i1 = normalize_ioc("203.0.113.7")
    a2, i2 = normalize_ioc("203.0.113.7")
    assert a1.id == a2.id and i1.id == i2.id                    # content-addressed STIX ids


def test_seeding_is_idempotent():
    g = CtiGraph()
    seed_ioc(g, "203.0.113.7", scenario="crowdsecurity/ssh-bf")
    n_obj, n_rel = len(g.objects), len(g.relationships)
    seed_ioc(g, "203.0.113.7", scenario="crowdsecurity/ssh-bf")
    assert len(g.objects) == n_obj and len(g.relationships) == n_rel  # no duplication


def test_attack_mapping_is_explainable():
    """The scenario→technique mapping records the matched token as its basis (no model guess)."""
    pairs = attack.map_scenario_to_techniques("crowdsecurity/ssh-bf")
    assert ("T1110", "ssh-bf") in pairs
    assert attack.map_scenario_to_techniques("nothing-here") == []


def test_taxii_offline_ingest():
    """A TAXII bundle ingests into the graph; the read seam is offline/testable."""
    _, indicator = normalize_ioc("198.51.100.9")
    bundle = {"type": "bundle", "objects": [
        {"type": "indicator", "id": indicator.id, "x_ioc_value": "198.51.100.9",
         "pattern": "[ipv4-addr:value = '198.51.100.9']"},
    ]}
    g = CtiGraph()
    n = ingest_bundle(g, bundle, source="offline-taxii")
    assert n == 1 and g.objects_of_type("indicator")
    # via the source seam
    src = OfflineTaxiiSource([bundle])
    assert src.poll() == [bundle]


def test_no_impact_when_ioc_not_seen_internally():
    store = CaseStore()
    case = ingest_crowdsec_alert(store, ALERT)
    obs = store.observations[case.observation_refs[0]]
    g = CtiGraph()
    seed_ioc(g, "198.51.100.9", scenario="crowdsecurity/ssh-bf")   # a DIFFERENT IP
    assert correlate_internal(g, [obs]) == []

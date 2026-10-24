"""SOC 2 assessments: every seeded failure ends in the right result, and no scenario with missing, partial, unverifiable or merely claimed evidence ends in PASS."""
from __future__ import annotations

import importlib
import sqlite3

import pytest

A = importlib.import_module("edge-sentinel.soc2.assess")
C = importlib.import_module("edge-sentinel.soc2.controls")
kit = importlib.import_module("edge-sentinel.soc2._testkit")
vaultmod = importlib.import_module("edge-sentinel.soc2.vault")
env, acct, accounts, release, NOW, DAY, PERIOD = kit.env, kit.acct, kit.accounts, kit.release, kit.NOW, kit.DAY, kit.PERIOD


def run(tmp_path, control, *evidence, tenant="acme", scope="prod", clock=None):
    v = kit.make_vault(tmp_path, clock)
    for i, e in enumerate(evidence):
        v.ingest(e)
    return v, A.Assessor(v, clock=kit.Clock()).assess(control, tenant, scope, *PERIOD, owner="owner-1")


def good_changes():
    return env("change_records", {"releases": [release("r1"), release("r2", approvers=("dev-3", "dev-2"))], "population_complete": True})


# ───────────────────────── PASS needs all of it ─────────────────────────
def test_a_complete_verified_compliant_population_passes(tmp_path):
    _, a = run(tmp_path, "SEC-CHG-01", good_changes())
    assert a.result == A.PASS and a.population == 2 and a.tested == 2 and a.failures == () and a.coverage >= 0.99 and len(a.evidence_ids) == 1


def test_each_automated_control_has_a_passing_case(tmp_path):
    cases = {
        "SEC-ACC-01": env("access_review", accounts(acct("a1"), acct("a2", privileged=False, mfa=False))),
        "SEC-ACC-02": env("access_review", accounts(acct("a1"), acct("a2", reviewed_by="reviewer-2"))),
        "SEC-ACC-03": env("access_review", accounts(acct("a1", status="terminated", terminated_at=NOW - 10 * DAY, access_revoked_at=NOW - 10 * DAY + 3600))),
        "SEC-MON-01": env("heartbeat", {"events_seen": 10}),
        "SEC-CFG-01": env("scanner_result", {"target_type": "host", "target": "web-1", "critical_open": 0, "scanned_at": NOW - DAY}),
        "AVL-BCK-01": env("backup_restore", {"backups": [{"id": "b1", "at": NOW - DAY, "ok": True}], "restore_tests": [{"at": NOW - 5 * DAY, "ok": True}],
                                             "population_complete": True}),
        "PIN-REC-01": env("reconciliation", {"outcomes": [{"id": "o1", "state": "resolved", "opened_at": NOW - 3 * DAY, "resolved_at": NOW - 2 * DAY}],
                                             "population_complete": True}),
    }
    for control, e in cases.items():
        _, a = run(tmp_path / control, control, e) if (tmp_path / control).mkdir() is None else (None, None)
        assert a.result == A.PASS, (control, a.reason)


# ───────────────────────── seeded failures ─────────────────────────
@pytest.mark.parametrize("control,evidence,expected,why", [
    ("SEC-ACC-01", env("access_review", accounts(acct("a1", mfa=False))), A.FAIL, "missing MFA on a privileged account"),
    ("SEC-ACC-02", env("access_review", accounts(acct("a1", reviewed_at=NOW - 200 * DAY))), A.FAIL, "stale privileged access review"),
    ("SEC-ACC-02", env("access_review", accounts(acct("a1", reviewed_by=None, reviewed_at=None))), A.FAIL, "no access review at all"),
    ("SEC-ACC-02", env("access_review", accounts(acct("a1", owner="p1", reviewed_by="p1"))), A.FAIL, "a person reviewing their own access"),
    ("SEC-ACC-03", env("access_review", accounts(acct("a1", status="terminated", terminated_at=NOW - 10 * DAY, access_revoked_at=NOW - 5 * DAY))), A.FAIL,
     "a leaver kept access for days"),
    ("SEC-ACC-03", env("access_review", accounts(acct("a1", status="terminated", terminated_at=NOW - 10 * DAY, access_revoked_at=None))), A.FAIL,
     "a leaver whose access was never revoked"),
    ("SEC-CHG-01", env("change_records", {"releases": [release("r1", approvers=())], "population_complete": True}), A.FAIL, "an unreviewed change"),
    ("SEC-CHG-01", env("change_records", {"releases": [release("r1", approvers=("dev-1",))], "population_complete": True}), A.FAIL, "self-approved change"),
    ("SEC-CHG-01", env("change_records", {"releases": [release("r1", passed=("ci",))], "population_complete": True}), A.FAIL, "a required check did not pass"),
    ("SEC-CHG-01", env("change_records", {"releases": [release("r1", required=(), passed=())], "population_complete": True}), A.FAIL, "no required checks recorded"),
    ("AVL-BCK-01", env("backup_restore", {"backups": [{"id": "b1", "at": NOW, "ok": True}], "restore_tests": [], "population_complete": True}), A.FAIL,
     "a successful backup with no restore test"),
    ("AVL-BCK-01", env("backup_restore", {"backups": [{"id": "b1", "at": NOW, "ok": True}], "restore_tests": [{"at": NOW - DAY, "ok": False}],
                                          "population_complete": True}), A.FAIL, "the restore test failed"),
    ("AVL-BCK-01", env("backup_restore", {"backups": [{"id": "b1", "at": NOW, "ok": False}], "restore_tests": [{"at": NOW - DAY, "ok": True}],
                                          "population_complete": True}), A.FAIL, "a backup failed"),
    ("PIN-REC-01", env("reconciliation", {"outcomes": [{"id": "o1", "state": "unknown", "opened_at": NOW - 20 * DAY, "resolved_at": None}],
                                          "population_complete": True}), A.FAIL, "an unknown outcome left open for weeks"),
    ("SEC-CFG-01", env("scanner_result", {"target_type": "host", "target": "web-1", "critical_open": 2, "scanned_at": NOW - DAY}), A.FAIL, "open critical findings"),
])
def test_seeded_control_failures_are_failures(tmp_path, control, evidence, expected, why):
    _, a = run(tmp_path, control, evidence)
    assert a.result == expected, (why, a.result, a.reason)
    assert a.failures, why
    if control == "AVL-BCK-01" and "no restore" in why or "without a restore" in why:
        assert any("no restore test" in f for f in a.failures)


@pytest.mark.parametrize("control,evidence,why", [
    ("SEC-MON-01", env("heartbeat", {}, cov=(NOW - 30 * DAY, NOW - 12 * DAY)), "a missing log window (60% of the period)"),
    ("SEC-CFG-01", env("scanner_result", {"target_type": "container-image", "target": "web:1", "critical_open": 0, "scanned_at": NOW - DAY}),
     "a container-image scan offered for a host control"),
    ("SEC-CFG-01", env("scanner_result", {"target_type": "host", "target": "web-1", "critical_open": 0, "scanned_at": NOW - 90 * DAY}), "a scan outside the period"),
    ("SEC-CHG-01", env("change_records", {"releases": [release("r1")]}), "a producer that did not state the population is complete"),
    ("SEC-CHG-01", env("change_records", {"releases": [release("r1")], "population_complete": False}), "a producer that says the population is incomplete"),
    ("SEC-ACC-01", env("access_review", {"status": "compliant", "all_controls_met": True}), "a bare claim of compliance with no data"),
    ("SEC-ACC-01", env("access_review", accounts()), "an empty privileged population (a collector that found nothing)"),
    ("PIN-REC-01", env("reconciliation", {"outcomes": [], "population_complete": False}), "an empty, unconfirmed population"),
])
def test_missing_partial_or_claimed_evidence_is_inconclusive_never_a_pass(tmp_path, control, evidence, why):
    _, a = run(tmp_path, control, evidence)
    assert a.result == A.INCONCLUSIVE, (why, a.result, a.reason)


def test_no_evidence_at_all_is_not_tested(tmp_path):
    for control in ("SEC-CHG-01", "SEC-ACC-01", "AVL-BCK-01", "SEC-MON-01"):
        _, a = run(tmp_path / control, control) if (tmp_path / control).mkdir() is None else (None, None)
        assert a.result == A.NOT_TESTED


def test_another_tenants_evidence_does_not_count(tmp_path):
    _, a = run(tmp_path, "SEC-CHG-01", env("change_records", {"releases": [release("r1")], "population_complete": True}, tenant="globex", producer="collector-b",
                                           key_id="key-b", secret=kit.SECRET_B))
    assert a.result == A.NOT_TESTED


def test_another_scopes_evidence_does_not_count(tmp_path):
    _, a = run(tmp_path, "SEC-CHG-01", env("change_records", {"releases": [release("r1")], "population_complete": True}, scope="staging"))
    assert a.result == A.NOT_TESTED


def test_tampered_evidence_makes_the_assessment_inconclusive_not_a_pass(tmp_path):
    v = kit.make_vault(tmp_path)
    r = v.ingest(good_changes())
    c = sqlite3.connect(v.path)
    c.execute("DROP TRIGGER evidence_no_update")
    c.execute("UPDATE evidence SET content = CAST('{\"releases\": [], \"population_complete\": true}' AS BLOB)")
    c.commit()
    a = A.Assessor(v, clock=kit.Clock()).assess("SEC-CHG-01", "acme", "prod", *PERIOD)
    assert a.result == A.INCONCLUSIVE and "verification" in a.reason and r.artifact_id in a.evidence_ids


def test_one_bad_artifact_among_good_ones_blocks_a_pass(tmp_path):
    v = kit.make_vault(tmp_path)
    v.ingest(good_changes())
    r2 = v.ingest(env("change_records", {"releases": [release("r9")], "population_complete": True}, nonce="second"))
    c = sqlite3.connect(v.path)
    c.execute("DROP TRIGGER evidence_no_update")
    c.execute("UPDATE evidence SET sha256=? WHERE artifact_id=?", ("0" * 64, r2.artifact_id))
    c.commit()
    assert A.Assessor(v, clock=kit.Clock()).assess("SEC-CHG-01", "acme", "prod", *PERIOD).result == A.INCONCLUSIVE


def test_duplicate_collection_is_not_double_counted(tmp_path):
    e = good_changes()
    v, a = run(tmp_path, "SEC-CHG-01", e, e, e)
    assert len(v.find("acme")) == 1 and a.population == 2 and a.result == A.PASS


def test_evidence_that_arrives_late_still_counts_and_is_visibly_late(tmp_path):
    clock = kit.Clock(NOW + 3 * DAY)
    v, a = run(tmp_path, "SEC-CHG-01", good_changes(), clock=clock)
    assert a.result == A.PASS and v.find("acme")[0].late_by_s == pytest.approx(3 * DAY)


def test_a_failing_item_is_reported_even_if_other_evidence_passes(tmp_path):
    _, a = run(tmp_path, "SEC-CHG-01", good_changes(),
               env("change_records", {"releases": [release("bad", approvers=())], "population_complete": True}, nonce="n2"))
    assert a.result == A.FAIL and any("bad" in f for f in a.failures)


# ───────────────────────── manual dependencies, applicability ─────────────────────────
def attest(control_id, by="reviewer-9", expires=NOW + 30 * DAY, producer="collector-a", **kw):
    return env("attestation", {"control_id": control_id, "attested_by": by, "role": "auditor", "at": NOW - DAY, "expires_at": expires,
                               "statement": "reviewed"}, producer=producer, **kw)


@pytest.mark.parametrize("control", ["SEC-INC-01", "CNF-SEC-01", "PRV-DSR-01"])
def test_a_control_with_a_manual_dependency_is_never_an_automatic_pass(tmp_path, control):
    _, none = run(tmp_path / "a", control) if (tmp_path / "a").mkdir() is None else (None, None)
    assert none.result == A.MANUAL_REVIEW and "needs a person" in none.reason
    _, ok = run(tmp_path / "b", control, attest(control)) if (tmp_path / "b").mkdir() is None else (None, None)
    assert ok.result == A.MANUAL_REVIEW and "reviewer must still accept" in ok.reason and ok.evidence_ids


@pytest.mark.parametrize("kw,why", [(dict(by="owner-1"), "attested by the control owner"), (dict(expires=NOW - DAY), "an expired attestation"),
                                    (dict(by=""), "no named attester")])
def test_an_attestation_that_does_not_qualify_does_not_count(tmp_path, kw, why):
    _, a = run(tmp_path, "SEC-INC-01", attest("SEC-INC-01", **kw))
    assert a.result == A.MANUAL_REVIEW and "needs a person" in a.reason and not a.evidence_ids, why


def test_an_attestation_for_a_different_control_does_not_count(tmp_path):
    _, a = run(tmp_path, "SEC-INC-01", attest("CNF-SEC-01"))
    assert "needs a person" in a.reason


def test_not_applicable_needs_an_explicit_named_declaration_and_is_never_inferred(tmp_path):
    v = kit.make_vault(tmp_path)
    asr = A.Assessor(v, clock=kit.Clock())
    assert asr.assess("AVL-BCK-01", "acme", "prod", *PERIOD).result == A.NOT_TESTED                 # no evidence is not "not applicable"
    with pytest.raises(ValueError):
        asr.declare_not_applicable("acme", "prod", "AVL-BCK-01", by="", reason="we have no stateful data in this scope at all")
    with pytest.raises(ValueError):
        asr.declare_not_applicable("acme", "prod", "AVL-BCK-01", by="owner-1", reason="n/a")
    asr.declare_not_applicable("acme", "prod", "AVL-BCK-01", by="owner-1", reason="this scope holds no stateful data; verified with the architect")
    a = asr.assess("AVL-BCK-01", "acme", "prod", *PERIOD)
    assert a.result == A.NOT_APPLICABLE and "owner-1" in a.reason
    assert asr.assess("AVL-BCK-01", "acme", "staging", *PERIOD).result == A.NOT_TESTED             # only that scope


def test_an_unknown_control_is_not_tested(tmp_path):
    assert A.Assessor(kit.make_vault(tmp_path), clock=kit.Clock()).assess("NOPE-01", "acme", "prod", *PERIOD).result == A.NOT_TESTED


def test_a_leavers_free_period_with_a_complete_population_passes_but_an_unconfirmed_one_does_not(tmp_path):
    _, ok = run(tmp_path / "a", "SEC-ACC-03", env("access_review", accounts(acct("a1")))) if (tmp_path / "a").mkdir() is None else (None, None)
    assert ok.result == A.PASS and ok.population == 0                                               # nobody left in the period, and the producer says that is complete
    _, no = run(tmp_path / "b", "SEC-ACC-03", env("access_review", accounts(acct("a1"), complete=False))) if (tmp_path / "b").mkdir() is None else (None, None)
    assert no.result == A.INCONCLUSIVE


def test_the_catalog_is_well_formed_and_never_claims_more_than_its_references(tmp_path):
    ids = [c.control_id for c in C.CATALOG]
    assert len(ids) == len(set(ids)) and all(c.criterion_refs and c.owner_role and c.evidence_kinds for c in C.CATALOG)
    assert all(c.procedure in A.PROCEDURES or c.procedure == "" for c in C.CATALOG)
    assert all(c.manual_dependency for c in C.CATALOG if not c.procedure)                            # no control is automatic without a procedure
    assert all(r[0] in "CAPR" and any(ch.isdigit() for ch in r) for c in C.CATALOG for r in c.criterion_refs)


def test_a_control_with_both_a_procedure_and_a_manual_dependency_is_never_an_automatic_pass(tmp_path):
    ctl = C.ControlDef("X-1", "1", "changes are approved and the approver list is reviewed", ("CC8.1",), "engineering-owner", 30, "change_management",
                       ("change_records",), manual_dependency="the approver list reviewed by someone other than the owner")
    v = kit.make_vault(tmp_path)
    v.ingest(good_changes())
    a = A.Assessor(v, catalog=(ctl,), clock=kit.Clock()).assess("X-1", "acme", "prod", *PERIOD, owner="owner-1")
    assert a.result == A.MANUAL_REVIEW and a.population == 2 and a.failures == ()          # the automated part held; a person still has to accept it

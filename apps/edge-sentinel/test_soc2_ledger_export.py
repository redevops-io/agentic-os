"""The ledger (append-only assessments, separation of duties, exceptions) and the exports (no certification claim, no leakage)."""
from __future__ import annotations

import importlib
import json
import sqlite3

import pytest

A = importlib.import_module("edge-sentinel.soc2.assess")
C = importlib.import_module("edge-sentinel.soc2.controls")
L = importlib.import_module("edge-sentinel.soc2.ledger")
X = importlib.import_module("edge-sentinel.soc2.export")
COL = importlib.import_module("edge-sentinel.soc2.collectors")
kit = importlib.import_module("edge-sentinel.soc2._testkit")
env, acct, accounts, release, NOW, DAY, PERIOD = kit.env, kit.acct, kit.accounts, kit.release, kit.NOW, kit.DAY, kit.PERIOD


def world(tmp_path, clock=None):
    clock = clock or kit.Clock()
    v = kit.make_vault(tmp_path, clock)
    return v, A.Assessor(v, clock=clock), L.Ledger(str(tmp_path / "ledger.db"), clock=clock), clock


def record(asr, led, control, *evidence_envs, vault=None, owner="owner-1"):
    for e in evidence_envs:
        vault.ingest(e)
    a = asr.assess(control, "acme", "prod", *PERIOD, owner=owner)
    return a, led.record_assessment(a)


def good_changes(nonce=None):
    return env("change_records", {"releases": [release("r1")], "population_complete": True}, nonce=nonce)


def bad_changes():
    return env("change_records", {"releases": [release("r1", approvers=())], "population_complete": True})


# ───────────────────────── append-only, superseding ─────────────────────────
def test_assessments_are_append_only_and_a_reassessment_supersedes_without_erasing(tmp_path):
    v, asr, led, clock = world(tmp_path)
    a1, ref1 = record(asr, led, "SEC-CHG-01", bad_changes(), vault=v)
    assert a1.result == A.FAIL
    clock.t += 60
    a2, ref2 = record(asr, led, "SEC-CHG-01", good_changes("fixed"), vault=v)
    assert a2.result == A.FAIL                                              # the failing evidence is still in the period: remediation needs evidence that supersedes it
    hist = led.history("acme", "SEC-CHG-01", "prod")
    assert [h[0] for h in hist] == [ref1, ref2] and hist[1][1]["supersedes"] == ref1
    c = sqlite3.connect(led.path)
    for stmt in ("UPDATE entries SET body='{}'", "DELETE FROM entries"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute(stmt)
    assert led.verify()["ok"]


def test_a_tampered_ledger_is_detected(tmp_path):
    v, asr, led, _ = world(tmp_path)
    record(asr, led, "SEC-CHG-01", good_changes(), vault=v)
    c = sqlite3.connect(led.path)
    c.execute("DROP TRIGGER entries_no_update")
    c.execute("UPDATE entries SET body = replace(body, 'FAIL', 'PASS')")
    c.execute("UPDATE entries SET body = replace(body, 'PASS', 'PASS ')")
    c.commit()
    assert not led.verify()["ok"]


# ───────────────────────── review: separation of duties ─────────────────────────
def test_review_needs_someone_who_is_neither_the_owner_nor_a_producer_of_the_evidence(tmp_path):
    v, asr, led, _ = world(tmp_path)
    a, ref = record(asr, led, "SEC-CHG-01", good_changes(), vault=v)
    assert a.result == A.PASS
    for kw in (dict(reviewer="owner-1"), dict(reviewer="collector-a"), dict(reviewer=" "), dict(reviewer="rev-1", notes="ok")):
        args = dict(reviewer="rev-1", decision="approve", notes="reviewed the sample and the population", control_owner="owner-1", producers=("collector-a",))
        args.update(kw)
        with pytest.raises(L.ReviewError):
            led.review(ref, "acme", **args)
    led.review(ref, "acme", reviewer="rev-1", decision="approve", notes="reviewed the sample and the population", control_owner="owner-1", producers=("collector-a",))
    assert led.status("acme", ref)["reviewed"] is True


@pytest.mark.parametrize("bad_evidence,result", [(bad_changes, A.FAIL), (lambda: env("change_records", {"releases": []}), A.INCONCLUSIVE)])
def test_a_failed_or_inconclusive_assessment_cannot_be_approved(tmp_path, bad_evidence, result):
    v, asr, led, _ = world(tmp_path)
    a, ref = record(asr, led, "SEC-CHG-01", bad_evidence(), vault=v)
    assert a.result == result
    with pytest.raises(L.ReviewError, match="cannot be approved"):
        led.review(ref, "acme", reviewer="rev-1", decision="approve", notes="looks fine to me really", control_owner="owner-1")
    led.review(ref, "acme", reviewer="rev-1", decision="reject", notes="the evidence does not support it", control_owner="owner-1")
    st = led.status("acme", ref)
    assert st["rejected"] and not st["reviewed"] and st["result"] == result


def test_a_review_of_another_tenants_assessment_is_refused(tmp_path):
    v, asr, led, _ = world(tmp_path)
    _, ref = record(asr, led, "SEC-CHG-01", good_changes(), vault=v)
    with pytest.raises(L.ReviewError, match="no such assessment"):
        led.review(ref, "globex", reviewer="rev-1", decision="approve", notes="cross-tenant approval attempt", control_owner="x")


# ───────────────────────── exceptions never change the result ─────────────────────────
def test_an_exception_records_who_accepted_the_risk_and_never_turns_a_fail_into_anything_else(tmp_path):
    v, asr, led, clock = world(tmp_path)
    a, ref = record(asr, led, "SEC-CHG-01", bad_changes(), vault=v)
    with pytest.raises(L.ReviewError):
        led.open_exception("acme", control_id="SEC-CHG-01", scope="prod", owner="o", accepted_by="o", reason="the change was an emergency fix", expires_at=NOW + DAY,
                           remediation="add a second approver to the emergency path")
    with pytest.raises(L.ReviewError):
        led.open_exception("acme", control_id="SEC-CHG-01", scope="prod", owner="o", accepted_by="cto", reason="x", expires_at=NOW + DAY, remediation="y")
    with pytest.raises(L.ReviewError):
        led.open_exception("acme", control_id="SEC-CHG-01", scope="prod", owner="o", accepted_by="cto", reason="the change was an emergency fix", expires_at=NOW - 1,
                           remediation="add a second approver to the emergency path")
    exc = led.open_exception("acme", control_id="SEC-CHG-01", scope="prod", owner="o", accepted_by="cto", reason="the change was an emergency fix",
                             expires_at=NOW + 10 * DAY, remediation="add a second approver to the emergency path")
    st = led.status("acme", ref)
    assert st["result"] == A.FAIL and st["open_exception"] and not st["expired_exception"] and not st["reviewed"]
    clock.t += 11 * DAY                                                     # the exception expires: it no longer excuses anything
    st = led.status("acme", ref)
    assert st["result"] == A.FAIL and not st["open_exception"] and st["expired_exception"]
    assert [e["state"] for e in led.exceptions("acme")] == ["expired"]


def test_closing_an_exception_needs_remediation_evidence_and_a_person(tmp_path):
    v, asr, led, _ = world(tmp_path)
    exc = led.open_exception("acme", control_id="SEC-CHG-01", scope="prod", owner="o", accepted_by="cto", reason="the change was an emergency fix",
                             expires_at=NOW + 10 * DAY, remediation="add a second approver to the emergency path")
    with pytest.raises(L.ReviewError):
        led.close_exception("acme", exc, by="o", remediation_evidence=())
    with pytest.raises(L.ReviewError):
        led.close_exception("acme", exc, by=" ", remediation_evidence=("ev-1",))
    led.close_exception("acme", exc, by="o", remediation_evidence=("ev-1",))
    assert led.exceptions("acme")[0]["state"] == "closed"


# ───────────────────────── exports ─────────────────────────
def test_the_restricted_pack_carries_references_integrity_and_a_banner_but_no_claim(tmp_path):
    v, asr, led, _ = world(tmp_path)
    record(asr, led, "SEC-CHG-01", good_changes(), vault=v)
    record(asr, led, "SEC-ACC-01", env("access_review", accounts(acct("a1", mfa=False))), vault=v)
    pack = X.restricted_pack(led, v, C.CATALOG, "acme", "prod", *PERIOD)
    by = {c["control_id"]: c for c in pack["controls"]}
    assert pack["banner"].startswith("Readiness evidence") and "Not an attestation" in pack["banner"]
    assert by["SEC-CHG-01"]["result"] == A.PASS and by["SEC-ACC-01"]["result"] == A.FAIL and by["AVL-BCK-01"]["assessed"] is False
    ev = by["SEC-CHG-01"]["evidence"][0]
    assert ev["artifact_id"].startswith("ev-") and len(ev["sha256"]) == 64 and ev["producer"] == "collector-a"
    assert pack["integrity"]["vault"]["ok"] and pack["integrity"]["ledger"]["ok"]
    text = json.dumps(pack)
    assert '"releases"' not in text and "dev-1" not in text                                # references and digests, never the evidence content
    X.assert_no_claim(pack)


def test_the_pack_reports_broken_integrity_instead_of_hiding_it(tmp_path):
    v, asr, led, _ = world(tmp_path)
    record(asr, led, "SEC-CHG-01", good_changes(), vault=v)
    c = sqlite3.connect(v.path)
    c.execute("DROP TRIGGER evidence_no_update")
    c.execute("UPDATE evidence SET scope='elsewhere'")
    c.commit()
    assert X.restricted_pack(led, v, C.CATALOG, "acme", "prod", *PERIOD)["integrity"]["vault"]["ok"] is False


def test_the_benchmark_summary_is_counts_only(tmp_path):
    v, asr, led, _ = world(tmp_path)
    record(asr, led, "SEC-CHG-01", good_changes(), vault=v)
    record(asr, led, "SEC-ACC-01", env("access_review", accounts(acct("a1", mfa=False))), vault=v)
    s = X.benchmark_summary(led, "acme", "prod", *PERIOD, C.CATALOG)
    assert s["controls"] == len(C.CATALOG) and s["by_result"]["PASS"] == 1 and s["by_result"]["FAIL"] == 1 and s["by_result"]["NOT_ASSESSED"] == len(C.CATALOG) - 2
    text = json.dumps(s)
    assert "acme" not in text and "ev-" not in text and "CC8" not in text and "CC6" not in text and "sha256" not in text
    assert set(s) == {"banner", "controls", "by_result", "by_control"}


@pytest.mark.parametrize("claim", [{"soc2_certified": True}, {"overall_score": 0.9}, {"compliant": True}, {"x": {"overall_result": "PASS"}}, {"a": [{"attested": 1}]},
                                   {"note": "SOC2_CERTIFIED"}])
def test_an_export_that_claims_certification_or_an_overall_result_is_refused(claim):
    with pytest.raises(ValueError):
        X.assert_no_claim(claim)


# ───────────────────────── collectors ─────────────────────────
def test_collectors_produce_signed_envelopes_the_vault_accepts_and_the_assessor_reads(tmp_path):
    v, asr, led, _ = world(tmp_path)
    kw = dict(tenant="acme", scope="prod", producer="collector-a", key_id="key-a", secret=kit.SECRET_A, observed_at=NOW, coverage_from=PERIOD[0], coverage_to=PERIOD[1])
    v.ingest(COL.reconciliation_evidence([{"id": "o1", "state": "resolved", "opened_at": NOW - 3 * DAY, "resolved_at": NOW - 2 * DAY}], population_complete=True, **kw))
    assert asr.assess("PIN-REC-01", "acme", "prod", *PERIOD).result == A.PASS
    v.ingest(COL.from_sentinel_events([{"kind": "UNKNOWN_OUTCOME", "severity": "high"}], **kw))
    (e,) = v.find("acme", kind="agent_runtime_events")
    assert json.loads(v.read(e.artifact_id, tenant="acme", actor="t", purpose="t"))["count"] == 1

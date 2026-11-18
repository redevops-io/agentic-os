"""The audit harness must produce evidence the agentic-tests contract can consume.

These tests need no browser: they exercise the contract projection and the gate's rules directly, including
the cases a live run cannot show you — a heuristic that tries to block, an issue whose screenshot was never
written, and a known defect signature coming back after a fix. The live artifacts are checked against the
same rules by `scripts/ui_audit_edge_sentinel.py` itself.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("ui_audit_contract", ROOT / "scripts" / "ui_audit_contract.py")
c = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(c)


def _f(area="x", verdict="fail", severity="low", *, criterion="1.4.10", target="table",
       certainty=None, layer=None, ref="baseline-614-a11y.png", surface="dashboard"):
    certainty = certainty or c.Certainty.CONFIRMED
    f, _ = c.finding(area, "expected", "observed", verdict, severity, criterion=criterion,
                     target=target, surface=surface, certainty=certainty,
                     layer=layer or c.Layer.DETERMINISTIC, evidence_ref=ref)
    return f


# ── the fields the contract requires, and the signature that follows from them ────────────────────────

def test_a_finding_carries_every_field_the_contract_defines():
    f, errors = c.finding("no horizontal overflow", "fits 320px", "947px wide", "fail", "medium",
                          criterion="1.4.10", target="table", surface="dashboard",
                          certainty=c.Certainty.CONFIRMED, evidence_ref="run-overflow-mobile.png")
    assert errors == []
    for field in ("dimension", "summary", "severity", "certainty", "criterion", "recommendation",
                  "evidence_ref", "target", "signature"):
        assert field in f, field
    assert f["severity"] == 1, "medium maps to the contract's substantial, not to a string"
    assert f["signature"] == "1.4.10@table@dashboard"


def test_an_undeclared_certainty_is_a_contract_violation_not_a_default():
    f, errors = c.finding("x", "e", "o", "fail", "low", criterion="1.4.10", target="table")
    assert errors and "certainty must be declared" in errors[0]
    assert f["is_blocker"] is False, "an undeclared certainty can never be powerful enough to block"


def test_an_unknown_severity_is_rejected_and_treated_as_material_not_ignored():
    f, errors = c.finding("x", "e", "o", "fail", "pretty-bad", criterion="1.4.10", target="table",
                          certainty=c.Certainty.CONFIRMED)
    assert errors and "severity 'pretty-bad'" in errors[0]
    assert f["severity"] == 2, "an unparseable severity must not silently become ignorable"


def test_a_criterion_outside_the_known_set_is_rejected():
    # this is the guard against the exact mistake the first audit run made: citing a reflow success as
    # though it had been measured at the width the criterion is about
    _, errors = c.finding("x", "e", "o", "fail", "low", criterion="1.4.99", target="table",
                          certainty=c.Certainty.CONFIRMED)
    assert any("criterion" in e for e in errors)


def test_prose_in_evidence_ref_is_rejected():
    _, errors = c.finding("x", "e", "o", "fail", "low", criterion="1.4.10", target="table",
                          certainty=c.Certainty.CONFIRMED, evidence_ref="see the screenshot I took")
    assert any("evidence_ref" in e for e in errors)


# ── deterministic is authoritative; heuristic interprets ──────────────────────────────────────────────

def test_a_confirmed_deterministic_blocker_forces_needs_work():
    f = _f(severity="block")
    assert f["is_blocker"] is True
    assert c.status_for([f]) == c.Status.NEEDS_WORK


def test_a_heuristic_severity_zero_can_never_be_a_blocker():
    """The contract's hierarchy in one assertion: however certain a heuristic sounds, it does not block."""
    f = _f(severity="block", certainty=c.Certainty.UNCERTAIN, layer=c.Layer.HEURISTIC)
    assert f["is_blocker"] is False
    assert c.status_for([f]) == c.Status.REVIEW_REQUIRED, "it routes to a human instead"


def test_a_heuristic_cannot_upgrade_the_verdict_of_a_clean_deterministic_layer():
    det = _f(area="loads", verdict="pass", severity="info")
    heur = _f(area="error artefacts", severity="medium", certainty=c.Certainty.UNCERTAIN,
              layer=c.Layer.HEURISTIC)
    verdict = c.project([det, heur], url="http://127.0.0.1:8299")
    assert verdict["blockers"] == [], "a heuristic produced no blocker"
    assert verdict["status"] == c.Status.REVIEW_REQUIRED, "it can only ask for a human"
    assert verdict["measured"]["passes"] == 1 and verdict["measured"]["fails"] == 1


def test_an_uncertain_minor_cannot_be_reported_as_a_pass():
    assert c.status_for([_f(certainty=c.Certainty.UNCERTAIN)]) == c.Status.REVIEW_REQUIRED
    assert c.status_for([_f(verdict="pass", severity="info",
                            certainty=c.Certainty.UNCERTAIN)]) == c.Status.PASS, \
        "a pass that was uncertain is not a finding at all"


# ── missing or unverifiable evidence is refused at the gate ───────────────────────────────────────────

def test_an_issue_without_evidence_is_refused(tmp_path):
    f = _f(ref="")
    errors = c.validate_evidence([f], tmp_path)
    assert errors and "no evidence_ref" in errors[0]


def test_an_issue_pointing_at_an_artifact_that_was_never_written_is_refused(tmp_path):
    f = _f(ref="run-somewhere-else.png")
    errors = c.validate_evidence([f], tmp_path)
    assert errors and "was not produced by this run" in errors[0]


def test_an_issue_pointing_at_a_real_artifact_passes(tmp_path):
    (tmp_path / "run-a11y.png").write_bytes(b"not really a png")
    assert c.validate_evidence([_f(ref="run-a11y.png")], tmp_path) == []


def test_a_url_reference_is_its_own_artifact(tmp_path):
    # a workflow-run link is evidence and cannot be a local file; refusing it would push people back to prose
    assert c.validate_evidence([_f(ref="https://github.com/redevops-io/agentic-os/actions/runs/1")],
                              tmp_path) == []


def test_a_pass_needs_no_evidence_reference(tmp_path):
    assert c.validate_evidence([_f(verdict="pass", severity="info", ref="")], tmp_path) == []


# ── the four reported defects, and what a regression looks like ───────────────────────────────────────

def test_the_four_reported_defects_have_distinct_signatures():
    sigs = list(c.DEFECT_SIGNATURES.values())
    assert len(sigs) == 4 and len(set(sigs)) == 4, "two defects sharing a signature cannot be told apart"
    # both table defects cite the same SC and target; they stay apart because the criterion differs from
    # the landmark one and the target differs from the other reflow one
    assert c.DEFECT_SIGNATURES["table-reflow-floor"] == "1.4.10@table.table@dashboard"
    assert c.DEFECT_SIGNATURES["unnamed-data-table"] == "1.3.1@table@dashboard"
    assert c.DEFECT_SIGNATURES["missing-main-landmark"] == "1.3.1@body@dashboard"


def test_a_signature_is_stable_across_runs_and_words():
    """Stability is the point: the summary text will be reworded, the run label changes, the severity may
    be re-judged. If the signature moved with any of that, regressions could not be asserted on."""
    a, _ = c.finding("no horizontal overflow", "expected fits 320px", "observed 947px at 320", "fail",
                     "medium", criterion="1.4.10", target="table.table", surface="dashboard",
                     certainty=c.Certainty.CONFIRMED)
    b, _ = c.finding("table reflow", "different expectation", "completely different observation",
                     "fail", "block", criterion="1.4.10", target="table.table", surface="dashboard",
                     certainty=c.Certainty.UNCERTAIN)
    assert a["signature"] == b["signature"] == c.DEFECT_SIGNATURES["table-reflow-floor"]


def test_a_known_defect_coming_back_after_a_fix_is_reported_as_a_regression():
    hits = c.regression_hits(["1.4.10@table.table@dashboard", "2.4.7@:focus@dashboard"])
    assert hits == ["1.4.10@table.table@dashboard"], "the fix that did not hold must be named, not counted"
    assert c.regression_hits(["2.4.7@:focus@dashboard"]) == []


def test_the_remediated_run_reports_no_known_signature():
    """What a genuinely fixed dashboard looks like to the gate: findings exist, none of them known."""
    fixed = [_f(area="keyboard focus", criterion="2.4.7", target=":focus")]
    verdict = c.project(fixed + [_f(area="loads", verdict="pass", severity="info")],
                        url="http://127.0.0.1:8299")
    assert c.regression_hits(verdict["signatures"]) == verdict["blockers"] == []


# ── the harness itself must speak the contract ────────────────────────────────────────────────────────

def test_the_harness_routes_every_record_through_the_contract():
    src = (ROOT / "scripts" / "ui_audit_edge_sentinel.py").read_text()
    assert "import ui_audit_contract" in src
    assert "ui_audit_contract.finding(" in src, "records must be built by the contract, not by hand"
    assert "ui_audit_contract.project(" in src and "validate_evidence(" in src
    assert "regression_hits(" in src, "the gate must check known signatures, not only severity words"
    # the old gate counted scary words; that must not come back alongside the real one
    assert 'f["severity"] in ("block", "high")' not in src


"""Rollback reporting: a step is reversible, compensated or irreversible-uncertain, and a rollback is clean only without residuals.

``rolled_back`` is true only when every applied step is compensated AND verified by an independent read-back AND has no residual
effect (declared by the capability, reported by its undo, or unknown/unverified) AND no compensation was denied or left incomplete.
The per-step classification and the residual list are in the outcome, the receipt and (folded from the log) after a restart."""
from __future__ import annotations

import pytest

from agentic_os.mission import compensation as C
from agentic_os.mission.compensation_runner import outcome_from_log
from agentic_os.mission.receipt import mission_receipt
from agentic_os.mission.types import MissionState
from tests.test_compensation_contracts import _calls, _h, _readback, _reserve_specs, _rt, _spec


def steps(m):
    return {s["capability"]: s for s in m.outcome["step_classification"]}


def clean(rel):
    return _h(rel, **{"res.verify": _readback})


# ── the clean case still reads as a clean rollback ───────────────────────────────────────────────────────────────────
def test_a_verified_compensation_without_residuals_is_a_clean_rollback():
    box, rel = _calls()
    rt, m = _rt(_reserve_specs(verify="res.verify"), clean(rel), ["reserved", "finished"])
    assert m.outcome["rolled_back"] is True and m.outcome["residual_effects"] == []
    s = steps(m)["res.reserve"]
    assert s["classification"] == "compensated" and s["verification"] == "readback" and s["residual_effects"] == []


# ── residual effects ─────────────────────────────────────────────────────────────────────────────────────────────────
def test_residuals_reported_by_the_undo_result_make_it_compensated_but_not_clean():
    undo = lambda i: {"undone": True, "executed": True, "residual_effects": ["a 'deleted' tombstone remains", {"effect": "the invoice number is consumed"}]}  # noqa: E731
    rt, m = _rt(_reserve_specs(verify="res.verify"), clean(undo), ["reserved", "finished"])
    s = steps(m)["res.reserve"]
    assert s["classification"] == "compensated"
    assert [r["effect"] for r in s["residual_effects"]] == ["a 'deleted' tombstone remains", "the invoice number is consumed"]
    assert all(r["source"] == "undo_result" and r["kind"] == "residual" for r in s["residual_effects"])
    assert m.outcome["rolled_back"] is False
    assert [(r["capability"], r["effect"]) for r in m.outcome["residual_effects"]][0] == ("res.reserve", "a 'deleted' tombstone remains")
    assert len(m.outcome["compensated"]) == 1 and m.state == MissionState.FAILED          # compensated and read back, just not clean


def test_residuals_declared_on_the_capability_make_it_not_clean_even_if_the_undo_reports_none():
    box, rel = _calls()
    rt, m = _rt(_reserve_specs(verify="res.verify", residual_effects=["an audit-log entry of the reservation remains"]), clean(rel), ["reserved", "finished"])
    s = steps(m)["res.reserve"]
    assert s["classification"] == "compensated"
    assert s["residual_effects"] == [{"effect": "an audit-log entry of the reservation remains", "kind": "residual", "source": "declared"}]
    assert m.outcome["rolled_back"] is False


def test_an_undo_that_says_residuals_are_unknown_is_not_clean():
    undo = lambda i: {"undone": True, "executed": True, "residual_unknown": True}  # noqa: E731
    rt, m = _rt(_reserve_specs(verify="res.verify"), clean(undo), ["reserved", "finished"])
    r = steps(m)["res.reserve"]["residual_effects"]
    assert r and r[0]["kind"] == "unknown" and m.outcome["rolled_back"] is False


def test_a_manual_verification_leaves_an_unverified_residual():
    box, rel = _calls()
    rt, m = _rt(_reserve_specs(), _h(rel), ["reserved", "finished"])
    rt.compensation_intervention(m.id, "verify_manually", actor="alice", reason="checked the console")
    s = steps(m)["res.reserve"]
    assert s["classification"] == "compensated" and s["verification"] == "manual"
    assert [r["kind"] for r in s["residual_effects"]] == ["unverified"] and m.outcome["rolled_back"] is False


# ── uncertain and irreversible ───────────────────────────────────────────────────────────────────────────────────────
def test_an_undo_nobody_verified_is_irreversible_uncertain():
    box, rel = _calls()
    rt, m = _rt(_reserve_specs(), _h(rel), ["reserved", "finished"])                      # no read-back declared
    s = steps(m)["res.reserve"]
    assert s["classification"] == "irreversible-uncertain" and s["compensation"] == "executed_unverified"
    assert s["residual_effects"][0]["kind"] == "unverified" and m.outcome["rolled_back"] is False


def test_a_failed_undo_leaves_the_forward_effect_in_place():
    rt, m = _rt(_reserve_specs(), _h(lambda i: {"status": "error", "error": "gateway"}), ["reserved", "finished"])
    s = steps(m)["res.reserve"]
    assert s["classification"] == "irreversible-uncertain" and s["compensation"] == "failed"
    assert s["residual_effects"][0]["kind"] == "retained" and "compensation failed" in s["residual_effects"][0]["effect"]
    assert m.outcome["rolled_back"] is False


def test_an_irreversible_step_is_irreversible_uncertain_and_blocks_a_clean_rollback():
    specs = [_spec("mail.send", "sent", side_effecting=True, irreversible=True), _spec("crm.fail", "failed", side_effecting=True, irreversible=True)]
    box, h = _calls()

    def boom(i):
        raise RuntimeError("downstream failed")
    rt, m = _rt(specs, {"mail.send": lambda i: {"sent": True, "executed": True}, "crm.fail": boom}, ["sent", "failed"])
    assert steps(m)["mail.send"]["classification"] == "irreversible-uncertain" and m.outcome["rolled_back"] is False


def test_a_step_that_was_never_compensated_is_reversible_and_the_mission_is_not_rolled_back():
    box, rel = _calls()
    rt, m = _rt(_reserve_specs(verify="res.verify"), clean(rel), ["reserved", "finished"], failure_policy="PRESERVE_ONLY")
    assert steps(m)["res.reserve"]["classification"] == "reversible" and steps(m)["res.reserve"]["compensation"] == "not_attempted"
    assert m.outcome["rolled_back"] is False and box["n"] == 0


# ── one function, live and after a restart; and the receipt ──────────────────────────────────────────────────────────
def test_the_classification_is_rebuilt_from_the_log_and_shown_in_the_receipt():
    undo = lambda i: {"undone": True, "executed": True, "residual_effects": ["a tombstone remains"]}  # noqa: E731
    rt, m = _rt(_reserve_specs(verify="res.verify"), clean(undo), ["reserved", "finished"])
    again = outcome_from_log(rt, m.id)
    assert again["step_classification"] == m.outcome["step_classification"] and again["rolled_back"] is False
    assert again["residual_effects"] == m.outcome["residual_effects"]
    rb = mission_receipt(rt, m.id)["rollback"]
    assert rb["rolled_back"] is False and rb["residual_effects"][0]["effect"] == "a tombstone remains"
    assert {s["classification"] for s in rb["steps"]} >= {"compensated"}


# ── declarations are validated ───────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["one string", [""], [3], {"a": 1}])
def test_a_residual_declaration_must_be_a_list_of_texts(bad):
    with pytest.raises(ValueError):
        C.coerce({"supported": True, "capability": "x.undo", "residual_effects": bad})


def test_a_residual_declaration_round_trips():
    spec = C.coerce({"supported": True, "capability": "x.undo", "residual_effects": ["a", "b"]})
    assert spec.residual_effects == ["a", "b"] and C.coerce(spec.to_dict()).residual_effects == ["a", "b"]

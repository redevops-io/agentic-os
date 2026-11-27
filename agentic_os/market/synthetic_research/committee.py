"""Transparent buying-committee DECISION MODEL (structural / qualitative; NOT a probability).

Individual SSR results are evaluated SEPARATELY per role. This module does not average Likert scores into a
purchase probability and outputs no probability or score: it applies explicit, inspectable gate rules to the per-role
SSR means and reports a categorical outcome plus how that outcome changes when thresholds move, when a gate is
removed, and when any single role's distribution moves to the end of its 95% interval.

Gates (roles come from the frame's ``committee_gates``):
* VETO roles (security, legal/data governance): ``trust`` mean below ``neg`` => blocked; below ``pos`` => conditional.
* CHAMPION roles: best ``purchase_intent`` mean at or above ``pos`` => a champion exists ("strong"); otherwise weak.
* BUDGET role (finance/procurement): ``purchase_intent`` mean below ``neg`` => contested; below ``pos`` => conditional.
* FEASIBILITY roles (platform/app owner): ``relevance`` mean below ``pos`` => migration/fit at risk.
Thresholds are HYPOTHETICAL (defaults 3.4 / 2.8 on the 1-5 synthetic SSR scale, which is known to compress toward
the middle; on real dry-run data every concept fell below 2.8, so prefer :func:`standardise` with thresholds in sd units) and
exist only to make the structure explicit; the sensitivity output shows how much hangs on them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

OUTCOMES = ("blocked_by_veto", "stalls_weak_champion", "advances_with_conditions", "advances")
Cell = Mapping[str, Mapping[str, float]]    # construct -> {"mean": x, "lo": x, "hi": x}


@dataclass(frozen=True)
class Gates:
    veto: Tuple[str, ...]
    champion: Tuple[str, ...]
    budget: Tuple[str, ...]
    feasibility: Tuple[str, ...]


@dataclass(frozen=True)
class Thresholds:
    pos: float = 3.4
    neg: float = 2.8


def standardise(roles: Mapping[str, Cell], mu: Mapping[str, float], sd: Mapping[str, float]) -> Dict[str, Dict[str, Dict[str, float]]]:
    """Express cell means and interval ends in standard-deviation units of the study's OWN panel distribution
    (``mu``/``sd`` per construct, e.g. over all personas and all real variants). Absolute 1-5 SSR values are compressed
    and construct-specific, so absolute gates (3.4 / 2.8) can classify every concept alike; a standardised reading says
    only how a role's response to a concept compares with the average response of the whole panel: a RELATIVE reading
    among the concepts tested, not a readiness verdict. Use with ``Thresholds(pos=0.25, neg=-0.25)`` or similar."""
    out: Dict[str, Dict[str, Dict[str, float]]] = {}
    for r, cons in roles.items():
        out[r] = {}
        for c, d in cons.items():
            s_ = sd.get(c) or 1.0
            out[r][c] = {k: ((v - mu.get(c, 0.0)) / s_ if k in ("mean", "lo", "hi") else v) for k, v in d.items()}
    return out


def _get(role: Optional[Cell], construct: str, which: str = "mean") -> Optional[float]:
    if not role or construct not in role:
        return None
    return role[construct].get(which)


def evaluate(roles: Mapping[str, Cell], gates: Gates, thr: Thresholds = Thresholds(), *, disabled: Sequence[str] = (),
             shift: Optional[Tuple[str, str]] = None) -> Dict[str, Any]:
    """Apply the gate rules. ``shift=(role_id, 'lo'|'hi')`` evaluates that role at an interval end;
    ``disabled`` lists gate names ('veto', 'champion', 'budget', 'feasibility') to switch off."""
    def val(role_id: str, construct: str) -> Optional[float]:
        which = "mean"
        if shift and shift[0] == role_id:
            which = shift[1]
        return _get(roles.get(role_id), construct, which)

    detail: Dict[str, Any] = {}
    veto_states = {}
    for r in gates.veto:
        t = val(r, "trust")
        veto_states[r] = "unknown" if t is None else ("blocked" if t < thr.neg else "conditional" if t < thr.pos else "clear")
    detail["veto"] = veto_states
    champs = {r: val(r, "purchase_intent") for r in gates.champion}
    strong = [r for r, v in champs.items() if v is not None and v >= thr.pos]
    detail["champion"] = {"strong": strong, "n_champion_roles": len(gates.champion), "state": "strong" if strong else "weak"}
    bstate = {}
    for r in gates.budget:
        v = val(r, "purchase_intent")
        bstate[r] = "unknown" if v is None else ("contested" if v < thr.neg else "conditional" if v < thr.pos else "ok")
    detail["budget"] = bstate
    fstate = {}
    for r in gates.feasibility:
        v = val(r, "relevance")
        if v is None:
            v = val(r, "purchase_intent")
        fstate[r] = "unknown" if v is None else ("at_risk" if v < thr.pos else "feasible")
    detail["feasibility"] = fstate

    blocked = "veto" not in disabled and any(s == "blocked" for s in veto_states.values())
    weak = "champion" not in disabled and not strong
    conds = []
    if "veto" not in disabled and any(s == "conditional" for s in veto_states.values()):
        conds.append("veto role conditional")
    if "budget" not in disabled and any(s in ("contested", "conditional") for s in bstate.values()):
        conds.append("budget not clear")
    if "feasibility" not in disabled and any(s == "at_risk" for s in fstate.values()):
        conds.append("migration/fit at risk")
    outcome = ("blocked_by_veto" if blocked else "stalls_weak_champion" if weak
               else "advances_with_conditions" if conds else "advances")
    return {"outcome": outcome, "conditions": conds, "gates": detail}


def committee_outcome(roles: Mapping[str, Cell], gates: Gates, thr: Thresholds = Thresholds()) -> Dict[str, Any]:
    """Base outcome + sensitivity to thresholds, each gate and each role's interval ends."""
    base = evaluate(roles, gates, thr)
    sens: Dict[str, str] = {}
    for d in (-0.25, 0.25):
        sens[f"thresholds {d:+.2f}"] = evaluate(roles, gates, Thresholds(thr.pos + d, thr.neg + d))["outcome"]
    for g in ("veto", "champion", "budget", "feasibility"):
        sens[f"without {g} gate"] = evaluate(roles, gates, thr, disabled=(g,))["outcome"]
    for r in sorted(roles):
        for w in ("lo", "hi"):
            sens[f"{r} at {w} end of 95% interval"] = evaluate(roles, gates, thr, shift=(r, w))["outcome"]
    changes = sorted(k for k, o in sens.items() if o != base["outcome"])
    return {**base, "structural_qualitative": True,
            "note": "structural reading of per-role synthetic SSR results; not a probability and not a forecast",
            "sensitivity": sens, "outcome_changes_when": changes, "robust": not changes, "synthetic": True}


__all__ = ["OUTCOMES", "Gates", "Thresholds", "standardise", "evaluate", "committee_outcome"]

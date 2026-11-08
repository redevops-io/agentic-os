"""The agentic-tests UI-audit evidence contract, projected for this repository's audit harness.

`ui_agent/contracts/audit.py` in redevops-io/agentic-tests (pinned at 249969e) defines what a UI-audit
finding must carry for a result to be comparable: `criterion`, `target`, `signature`, `certainty`, integer
`severity`, `evidence_ref`, `measured`, and a blocker-driven `status`. agentic-os cannot depend on
agentic-tests — different product, different release cadence — so this module reimplements the contract's
*rules*, and `test_ui_audit_contract.py` pins those rules against the contract's documented semantics so the
two cannot drift silently.

The three rules that matter here, and that this harness did not previously enforce:

1. **An issue without an evidence reference is not evidence.** `validate_evidence()` rejects a finding whose
   `evidence_ref` is empty or points at a file the run did not write, and the gate refuses to pass on a
   finding set it cannot verify.
2. **Certainty decides what a finding can do.** A CONFIRMED deterministic severity-0 finding forces
   `NEEDS_WORK`; an UNCERTAIN one can only ever force `REVIEW_REQUIRED`. The heuristic layer can never mark
   a release blocker, however confident it sounds — deterministic instruments are authoritative and
   heuristics interpret them.
3. **A signature is what makes a regression visible.** `criterion @ target @ surface` is stable across runs;
   if the same string returns after a fix, the fix did not hold.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


class Certainty:
    CONFIRMED = "confirmed"        # a deterministic instrument established it → can force NEEDS_WORK
    UNCERTAIN = "uncertain"        # heuristic / incomplete / flaky → routes to REVIEW_REQUIRED


class Status:
    PASS = "PASS"
    NEEDS_WORK = "NEEDS_WORK"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class Layer:
    DETERMINISTIC = "deterministic"    # measured from DOM/HTTP: reflow, landmarks, names, statuses
    HEURISTIC = "heuristic"            # interpretation: "looks like an error artefact", axe needs-review
    FRICTION = "friction"              # journey-level effort observations


#: The harness's human-readable severities mapped onto the contract's integers (0 = blocker).
SEVERITY_INT = {"block": 0, "high": 1, "medium": 1, "low": 2, "info": 3, "observe": 3}
VERDICTS = ("pass", "fail", "observe")

#: Criterion names this audit may cite: WCAG SC ids, plus the harness's own measured criteria named out
#: rather than left blank. A criterion outside this set is a standard invented on the spot — which is how
#: an audit starts disagreeing with itself, and why a 947 px floor was once cited against a reflow check
#: that had never been run at the width the check is about.
KNOWN_CRITERIA = {
    "1.3.1", "1.4.3", "1.4.4", "1.4.10", "1.4.11", "2.1.1", "2.4.4", "2.4.6", "2.4.7", "2.5.8",
    "3.1.1", "4.1.2", "4.1.3",
    "demo-label", "no-side-effect", "refusal-state", "health-honesty", "load", "no-error-artefacts",
    "no-native-alert", "control-naming", "fixture-data", "unauth-parity", "staging", "execution-offer",
}


def severity_int(severity: str) -> Optional[int]:
    """Map a severity label to the contract integer, or None when the label is unknown."""
    return SEVERITY_INT.get(str(severity).strip().lower())


def signature(criterion: str, target: str = "", surface: str = "") -> str:
    """`criterion @ target @ surface` — the contract's stable regression key."""
    return f"{criterion or 'unknown'}@{target or '*'}@{surface or '*'}"


def is_valid_ref_name(name: str) -> bool:
    """A reference is a file name or a URL, not prose — prose in this field is how evidence quietly turns
    into a claim."""
    return bool(re.fullmatch(r"[\w.\-/]+\.(png|json|txt|html|log|md)|https?://\S+", name))


def finding(area: str, expected: str, observed: str, verdict: str, severity: str,
            *, criterion: str = "", target: str = "", surface: str = "",
            certainty: Optional[str] = None, layer: str = Layer.DETERMINISTIC,
            evidence_ref: str = "", recommendation: str = "") -> "tuple[Dict[str, Any], List[str]]":
    """Build one contract finding, plus the list of ways it fails to be evidence.

    Errors are returned rather than raised so a run can report every invalid finding at once: a harness that
    stops at the first problem tells you less than one that counts them.
    """
    errors: List[str] = []
    sev = severity_int(severity)
    if sev is None:
        errors.append(f"{area}: severity {severity!r} is not one of {sorted(SEVERITY_INT)}")
        sev = 2                                   # unknown severities are treated as material, not ignorable
    if verdict not in VERDICTS:
        errors.append(f"{area}: verdict {verdict!r} is not one of {list(VERDICTS)}")
    if certainty not in (Certainty.CONFIRMED, Certainty.UNCERTAIN):
        errors.append(f"{area}: certainty must be declared confirmed or uncertain, got {certainty!r}")
    if layer not in (Layer.DETERMINISTIC, Layer.HEURISTIC, Layer.FRICTION):
        errors.append(f"{area}: layer {layer!r} is not deterministic/heuristic/friction")
    if criterion and criterion not in KNOWN_CRITERIA:
        errors.append(f"{area}: criterion {criterion!r} is not a known WCAG SC id or harness criterion")
    if evidence_ref and not is_valid_ref_name(evidence_ref):
        errors.append(f"{area}: evidence_ref {evidence_ref!r} is not a file name or URL")
    f: Dict[str, Any] = {
        "dimension": area, "summary": f"expected {expected}; observed {observed}",
        "severity": sev, "severity_label": str(severity), "certainty": certainty,
        "criterion": criterion, "target": target, "surface": surface, "layer": layer,
        "recommendation": recommendation, "evidence_ref": evidence_ref,
        "signature": signature(criterion, target, surface), "verdict": verdict,
        "is_blocker": sev == 0 and certainty == Certainty.CONFIRMED and layer == Layer.DETERMINISTIC,
    }
    return f, errors


def validate_evidence(findings: Iterable[Dict[str, Any]], evidence_dir, *,
                      exists: Optional[Callable[[Any], bool]] = None) -> List[str]:
    """Every reported issue must point at something the run actually produced."""
    from pathlib import Path
    errors: List[str] = []
    root = Path(evidence_dir)
    check = exists or (lambda p: p.is_file())
    for f in findings:
        if f["verdict"] != "fail":
            continue                              # only issues are required to carry evidence
        ref = f.get("evidence_ref") or ""
        if not ref:
            errors.append(f"{f['dimension']}: a reported issue has no evidence_ref")
        elif not ref.startswith(("http://", "https://")) and not check(root / ref):
            errors.append(f"{f['dimension']}: evidence_ref {ref!r} was not produced by this run")
    return errors


def status_for(findings: List[Dict[str, Any]]) -> str:
    """The contract's blocker-driven status, including the rule that heuristics cannot block.

    The order is the one in `UIAuditVerdict.finalize()`: a confirmed blocker wins, then anything that needs
    a human, then a pass. A severity-0 that was *not* confirmed must never read as a pass — it reads as
    "a person should look at this", which is what the contract does with it.
    """
    active = [f for f in findings if f["verdict"] == "fail"]
    if any(f["is_blocker"] for f in active):
        return Status.NEEDS_WORK
    unconfirmed_zero = [f for f in active if f["severity"] == 0 and not f["is_blocker"]]
    uncertain_material = [f for f in active
                          if f["certainty"] == Certainty.UNCERTAIN and f["severity"] <= 2]
    if unconfirmed_zero or uncertain_material:
        return Status.REVIEW_REQUIRED
    if any(f["severity"] <= 1 for f in active):
        return Status.NEEDS_WORK              # a confirmed substantial finding is still work to do
    if any(f["severity"] == 2 for f in active):
        return Status.REVIEW_REQUIRED         # minor findings get a human, not a green light
    return Status.PASS


def project(findings: List[Dict[str, Any]], *, url: str, service_type: str = "web-app",
            journey: str = "", surfaces: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Project this run's records into the contract's verdict shape."""
    by_layer: Dict[str, List[Dict[str, Any]]] = {
        Layer.DETERMINISTIC: [], Layer.HEURISTIC: [], Layer.FRICTION: []}
    for f in findings:
        by_layer.setdefault(f["layer"], []).append(f)
    active = [f for f in findings if f["verdict"] == "fail"]
    return {
        "contract": "ui_agent/contracts/audit.py@249969e",
        "target": {"url": url, "service_type": service_type, "journey": journey},
        "status": status_for(findings),
        "blockers": [f for f in active if f["is_blocker"]],
        # a severity-0 that could not block is surfaced rather than hidden: it deserves a human, and
        # hiding it would be exactly the kind of quiet downgrade this contract exists to prevent.
        "escalations": [f for f in active if f["severity"] == 0 and not f["is_blocker"]],
        "findings": active,
        "observations": [f for f in findings if f["verdict"] == "observe"],
        "measured": {
            "journeys": len(findings),
            "passes": sum(1 for f in findings if f["verdict"] == "pass"),
            "fails": len(active),
            "observes": sum(1 for f in findings if f["verdict"] == "observe"),
            "surfaces": sorted(set(surfaces or ()) | {f["surface"] for f in findings if f["surface"]}),
            "confirmed": sum(1 for f in findings if f["certainty"] == Certainty.CONFIRMED),
            "uncertain": sum(1 for f in findings if f["certainty"] == Certainty.UNCERTAIN),
            "with_evidence": sum(1 for f in active if f["evidence_ref"]),
        },
        "by_layer": by_layer,
        "signatures": sorted({f["signature"] for f in active}),
    }


#: The four dashboard defects the UI audit reported and the dashboard-fix branch addresses. Pinned so a run
#: that still produces one of these strings fails the gate instead of reading as progress. The signatures
#: were assigned when the contract was adopted (the original run predates them) from the criterion and the
#: element each fix actually changed.
DEFECT_SIGNATURES = {
    # the 947 px floor: the dashboard's data table (class `table`) forced automatic layout wider than the
    # viewport. The selector is the one the harness measured, not the one assumed — `table.table`, because
    # the element is `<table class="table">`.
    "table-reflow-floor": signature("1.4.10", "table.table", "dashboard"),
    # the scenario-bar label column kept a fixed 200px grid track (`grid-template-columns:200px 1fr 56px`).
    # The signature is pinned so a regression is caught, but the browser harness does not currently report
    # it: at every width where the row is too wide, the 947px table dominates the measurement, and the
    # cause rule below classifies only self-overflow, min-width and auto-layout tables — a fixed grid track
    # inside an element that itself reports no self-overflow is invisible to it. The defect is asserted by
    # the DOM unit test `test_the_scenario_bars_can_shrink_below_the_fixed_column`. Recorded here rather
    # than hidden, because a guard that silently never fires is worse than a documented one.
    "scenario-bar-fixed-column": signature("1.4.10", "div.barlist__row", "dashboard"),
    "unnamed-data-table": signature("1.3.1", "table", "dashboard"),
    "missing-main-landmark": signature("1.3.1", "body", "dashboard"),
}


def regression_hits(signatures: Iterable[str]) -> List[str]:
    """Which known-defect signatures are still being reported. Non-empty means a fix did not hold."""
    known = set(DEFECT_SIGNATURES.values())
    return sorted(set(signatures) & known)



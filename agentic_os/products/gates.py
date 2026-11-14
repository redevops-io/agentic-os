"""Feature gates for the product integration.

``QA_EXTERNAL_TARGET_ACCEPTANCE_MET`` is the owner's gate (decision of 2026-11-11, P2-1 section 6.2): the redevops-qa
tools in Sidekick, the QA projection in Projects and the QA catalog entry are enabled only when P2-1 acceptance
criteria AC-1 to AC-12 pass IN CI on the loopback Edge Sentinel fixture target in redevops-qa. It is a code
constant, flipped by a reviewed change after that CI run exists; there is deliberately no environment variable,
registry key or request value that opens it, so configuration cannot bypass the owner's decision. Until then every
QA surface reports the explicit unavailable state below instead of a dead control or a fake row.
"""
from __future__ import annotations

from typing import Any, Dict

#: Flipped by this change, citing the green redevops-qa CI run covering AC-1..AC-12 on the loopback
#: Edge Sentinel fixture target (see docs/products/README.md, "Gates"): the "tests" workflow run
#: https://github.com/redevops-io/redevops-qa/actions/runs/43551629834 on main @ 9e495bdf (merge of
#: redevops-qa#67), whose "runner (Python 3.12; ...)" job ran
#: packages/qa-runner/tests/test_p21_external_target_acceptance.py (AC-1..AC-5, AC-9..AC-12) and
#: whose egress lane ran packages/qa-egress/tests/test_eg_p21_acceptance.py (AC-6..AC-8), all green.
QA_EXTERNAL_TARGET_ACCEPTANCE_MET = True

QA_GATE_CODE = "external-target-acceptance-not-met"
QA_GATE_REASON = "external-target acceptance not met"
QA_GATE_DETAIL = ("redevops-qa P2-1 acceptance criteria AC-1 to AC-12 have not passed in CI on the loopback Edge "
                  "Sentinel fixture target, so quality tools and the quality projection stay off in Projects and Sidekick.")
QA_GATE_REMEDY = ("Use the redevops-qa web UI directly for the bundled fixture target; Projects and Sidekick enable "
                  "quality tools after the acceptance run is recorded.")


def qa_gate_open() -> bool:
    return QA_EXTERNAL_TARGET_ACCEPTANCE_MET is True


def gate_for(kind: str) -> Dict[str, Any]:
    """``{"open": bool, ...}`` for a product kind; only ``quality`` is gated."""
    if kind == "quality" and not qa_gate_open():
        return {"open": False, "code": QA_GATE_CODE, "reason": QA_GATE_REASON, "detail": QA_GATE_DETAIL,
                "remedy": QA_GATE_REMEDY}
    return {"open": True}


__all__ = ["QA_EXTERNAL_TARGET_ACCEPTANCE_MET", "QA_GATE_CODE", "QA_GATE_REASON", "qa_gate_open", "gate_for"]

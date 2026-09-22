"""Phase B — turn a governed Integration-Plane run into canonical evidence + receipts.

The Integration-Plane runner (:func:`agentic_os.integrations.execution.run_test_mission`) executes a
plan's steps under governed envelopes and reconciles writes by re-observation. This module reads a
:class:`~agentic_os.integrations.execution.MissionRun` and produces the two things Mission logic and the
UI actually consume:

  * **canonical evidence** — every step's provider payload normalized into a typed business object
    (:mod:`.contracts`), so reasoning is over ``Charge``/``Contact``/``Message``… not provider JSON;
  * **receipts** — for each governed WRITE step, the canonical ``projects.ActionReceipt`` + an
    independent ``VerificationState`` (provider success is not real success).

This is the seam that makes a connector run project the plan's evidence → action → receipt → verification
lifecycle in canonical form, without any provider-specific logic here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple

from .contracts import BusinessObject
from .normalize import normalize
from .receipts import VerificationState, receipt_for_step


@dataclass(frozen=True)
class GovernedStep:
    """One governed write's outcome in canonical terms."""
    capability: str
    provider: str
    receipt: object                 # projects.contracts.ActionReceipt
    verification: VerificationState

    @property
    def succeeded(self) -> bool:
        return getattr(self.receipt, "status", "") == "SUCCEEDED"


@dataclass(frozen=True)
class GovernedMissionResult:
    intent_hash: str
    evidence: Tuple[BusinessObject, ...]     # canonical objects normalized from every step's payload
    receipts: Tuple[GovernedStep, ...]       # one per governed write step

    @property
    def all_writes_verified(self) -> bool:
        return bool(self.receipts) and all(s.succeeded for s in self.receipts)

    def to_dict(self) -> dict:
        return {"intent_hash": self.intent_hash,
                "evidence": [o.to_dict() for o in self.evidence],
                "receipts": [{"capability": s.capability, "provider": s.provider,
                              "verification": s.verification.value,
                              "receipt": s.receipt.to_dict()} for s in self.receipts]}


def canonical_evidence(run: object) -> Tuple[BusinessObject, ...]:
    """Normalize every step's provider payload into canonical business objects (skips steps whose shape
    has no registered normalizer). The raw payload stays available on the step as evidence."""
    out = []
    for step in getattr(run, "steps", ()):
        data = getattr(step, "data", {}) or {}
        if not data:
            continue
        obj = normalize(getattr(step, "provider", ""), data,
                        provider_ref=str(getattr(step, "provider_object_id", "")))
        if obj is not None:
            out.append(obj)
    return tuple(out)


def receipts_for_run(run: object, *,
                     decision_ids: Optional[Mapping[str, str]] = None) -> Tuple[GovernedStep, ...]:
    """A canonical ActionReceipt + verification for each governed WRITE step. Read steps produce evidence,
    not receipts. ``decision_ids`` maps a capability → the Decision id that authorized it (when known)."""
    dids = dict(decision_ids or {})
    out = []
    for step in getattr(run, "steps", ()):
        if not getattr(step, "write", False):
            continue
        receipt, v = receipt_for_step(step, decision_id=dids.get(getattr(step, "capability", ""), ""))
        out.append(GovernedStep(capability=getattr(step, "capability", ""),
                                provider=getattr(step, "provider", ""), receipt=receipt, verification=v))
    return tuple(out)


def govern_run(run: object, *, decision_ids: Optional[Mapping[str, str]] = None) -> GovernedMissionResult:
    """The whole projection: canonical evidence + write receipts for a governed MissionRun."""
    return GovernedMissionResult(
        intent_hash=str(getattr(run, "intent_hash", "")),
        evidence=canonical_evidence(run),
        receipts=receipts_for_run(run, decision_ids=decision_ids))

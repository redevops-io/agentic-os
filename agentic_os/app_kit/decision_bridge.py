"""Decision -> Mission bridge (plan §3.4, "the missing piece").

The Priority Engine decides *what* to do (a ``SelectedAction``); the Mission Runtime *does* it as a
governed node. Nothing connected the two. This bridge is that connection:

- **ABSTAIN / do-nothing / DEFER** -> write an ``InterventionRecord`` and launch nothing (N1: even a
  decision to do nothing is recorded).
- **ACT / REQUEST_APPROVAL** -> author a mission for the selected capability, carrying the
  ``intervention_id`` so every node and receipt links back to the decision, and set the node's
  approval gate to be *never weaker* than the capability's own policy (plan §3.4).

The bridge is pure and planner-agnostic: it emits a typed :class:`MissionRequest` and, if given a
:class:`MissionLauncher` (the thin adapter onto ``MissionRuntime``), launches it and records the
``mission_id``. With no launcher it runs plan-only (record written, request returned) for dry runs
and tests. The phantom-capability guard here complements the registry's registration-time check: an
actionable candidate with no capability cannot author a mission and is rejected.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Protocol, Tuple

from agentic_os.agent_gateway.contracts import ApprovalPolicy
from agentic_os.intervention_record import InterventionRecord, record_from_selection
from agentic_os.priority_engine import Action, SelectedAction

BRIDGE_CONTRACT_VERSION = "decision-bridge/v1"

# Approval policies that force a human gate regardless of what the scorer decided.
_GATE_POLICIES = frozenset({ApprovalPolicy.REQUIRED, ApprovalPolicy.MANDATORY})


class BridgeError(ValueError):
    """Raised when a selection cannot be bridged (e.g. an actionable candidate with no capability)."""


class BridgeOutcome(str, Enum):
    ABSTAINED = "abstained"              # do-nothing / ABSTAIN: recorded, no mission
    DEFERRED = "deferred"               # DEFER: recorded, no mission now
    LAUNCHED = "launched"               # mission authored, ungated (ACT)
    LAUNCHED_GATED = "launched_gated"   # mission authored behind an approval gate


@dataclass(frozen=True)
class MissionRequest:
    """What the bridge asks the runtime to author for one selected action."""

    goal: str
    capability: str                                  # the primary required capability
    required_capabilities: Tuple[str, ...]
    inputs: Mapping[str, Any]
    gated: bool
    intervention_id: str
    opportunity_id: str
    action_kind: str
    subject: str
    evidence_refs: Tuple[str, ...] = ()


class MissionLauncher(Protocol):
    """The seam onto the Mission Runtime. An adapter authors a (single-capability, optionally gated)
    mission from the request, carrying ``intervention_id``, and returns the new ``mission_id``."""

    def launch(self, request: "MissionRequest") -> str: ...


@dataclass(frozen=True)
class BridgeResult:
    outcome: BridgeOutcome
    record: InterventionRecord
    mission_id: str = ""
    request: Optional[MissionRequest] = None


def _is_actionable(sel: SelectedAction) -> bool:
    """ACT and REQUEST_APPROVAL author a mission; ABSTAIN/DEFER and the do-nothing candidate do not."""
    if sel.action.candidate_id == "do-nothing":
        return False
    return sel.decision.action in (Action.ACT, Action.REQUEST_APPROVAL)


def _should_gate(sel: SelectedAction) -> bool:
    """The node's approval requirement is never weaker than the capability's own (plan §3.4)."""
    if sel.decision.action is Action.REQUEST_APPROVAL or sel.decision.requires_approval:
        return True
    return sel.action.effective_approval_policy() in _GATE_POLICIES


def _goal_for(sel: SelectedAction) -> str:
    c = sel.action
    label = c.action_kind or c.proposed_action or c.candidate_id or "act"
    return f"{label} for {c.subject}" if c.subject else label


def build_request(sel: SelectedAction, *, intervention_id: str,
                  inputs: Optional[Mapping[str, Any]] = None,
                  evidence_refs: Tuple[str, ...] = ()) -> MissionRequest:
    """Build the MissionRequest for an actionable selection. Raises BridgeError if the candidate
    carries no capability (a phantom actionable candidate the plan wants eliminated)."""
    c = sel.action
    if not c.required_capabilities:
        raise BridgeError(
            f"actionable candidate '{c.candidate_id or c.proposed_action}' for '{c.subject}' has no "
            f"required_capabilities — cannot author a mission (fix the producer or make it ABSTAIN/DEFER)"
        )
    return MissionRequest(
        goal=_goal_for(sel),
        capability=c.required_capabilities[0],
        required_capabilities=tuple(c.required_capabilities),
        inputs=dict(inputs or {}),
        gated=_should_gate(sel),
        intervention_id=intervention_id,
        opportunity_id=sel.opportunity_id,
        action_kind=c.action_kind or c.proposed_action[:40],
        subject=c.subject,
        evidence_refs=tuple(evidence_refs),
    )


def bridge_selection(sel: SelectedAction, *, store, policy_version: str,
                     launcher: Optional[MissionLauncher] = None,
                     inputs: Optional[Mapping[str, Any]] = None,
                     evidence_refs: Tuple[str, ...] = (),
                     intervention_id: Optional[str] = None,
                     now: Optional[float] = None) -> BridgeResult:
    """Bridge one Priority-Engine selection to a mission (or to nothing), always writing a record.

    ``store`` is an ``InterventionStore`` (``.append(InterventionRecord)``). ``launcher`` is optional:
    with one, an actionable selection is launched and the ``mission_id`` captured; without one, the
    bridge is plan-only (record written with an empty mission_id, request returned).
    """
    import time

    ts = time.time() if now is None else now
    iid = intervention_id or uuid.uuid4().hex
    refs = tuple(evidence_refs) or tuple(sel.action.observation_refs)

    if not _is_actionable(sel):
        outcome = (BridgeOutcome.DEFERRED if sel.decision.action is Action.DEFER
                   else BridgeOutcome.ABSTAINED)
        record = record_from_selection(sel, intervention_id=iid, policy_version=policy_version,
                                        proposed_at=ts, evidence_refs=refs)
        store.append(record)
        return BridgeResult(outcome=outcome, record=record)

    request = build_request(sel, intervention_id=iid, inputs=inputs, evidence_refs=refs)
    mission_id = launcher.launch(request) if launcher is not None else ""
    record = record_from_selection(sel, intervention_id=iid, policy_version=policy_version,
                                    proposed_at=ts, evidence_refs=refs, mission_id=mission_id)
    store.append(record)
    outcome = BridgeOutcome.LAUNCHED_GATED if request.gated else BridgeOutcome.LAUNCHED
    return BridgeResult(outcome=outcome, record=record, mission_id=mission_id, request=request)


__all__ = [
    "BRIDGE_CONTRACT_VERSION",
    "BridgeError",
    "BridgeOutcome",
    "MissionRequest",
    "MissionLauncher",
    "BridgeResult",
    "build_request",
    "bridge_selection",
]

"""Governed Assist — the first mode in which the runtime ACTS on its own, confined to the safest tier
(PR-sequence.odt PR 8, the final step of the sequence).

Everything before this only ever RECOMMENDED (Shadow) or LEARNED offline (the promotion gate). Governed
Assist is the first mode where a recommendation can auto-execute — and it does so under a deliberately
narrow rule: **only READ-tier interventions auto-run; everything else stays approval-gated**, exactly as
the odt scopes the first live step.

The guarantee is stronger than the base policy's own auto-execute gate. `decide` may return ``ACT`` for a
BOUNDED_WRITE when a deployment opts into ``allow_auto_bounded_writes``; Governed Assist's first cut does
NOT honour that — it caps auto-execution at ``max_auto_tier = READ`` regardless, so no write of any kind
runs without a human. Raising that cap is a later, explicit decision, not a default.

Fail-safe by construction:
  * an ``ACT`` above the auto cap → routed for approval (never silently dropped, never executed);
  * an ``ACT`` at/below the cap with NO registered capability → routed for approval (we can't run what we
    can't do — so we ask a human rather than pretend);
  * a capability that raises → recorded as attempted-but-not-executed and routed for approval;
  * ABSTAIN / DEFER / REQUEST_APPROVAL pass through unchanged.

Every path records a durable, immutable :class:`~agentic_os.intervention_record.InterventionRecord`
BEFORE anything is surfaced or executed — an auto-executed one carries ``executed_at`` + ``execution_ref``
at creation (records are immutable; we never mutate one after the fact). Governance is preserved: the
action still goes through ``decide``; Assist only chooses whether an already-``ACT`` decision at the
safest tier is run now or handed to a human.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Optional, Tuple

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.intervention_record import (
    InterventionRecord, InterventionStore, record_from_selection)
from agentic_os.priority_engine import (
    Action, DecisionOpportunity, PriorityPolicy, SelectedAction, UtilityFn, select_action)

#: a capability runs a READ-tier action and returns an execution reference (an id / handle for what it
#: did). Raising signals the action could not be performed — Assist then routes it to a human.
Capability = Callable[[SelectedAction], str]


class AssistOutcome(Enum):
    AUTO_EXECUTED = "auto_executed"                 # ran automatically (READ-tier, capability present)
    ROUTED_FOR_APPROVAL = "routed_for_approval"     # a human must approve before it runs
    DEFERRED = "deferred"                           # worth doing, not now / beyond the budget
    ABSTAINED = "abstained"                         # do nothing — the Priority Engine's baseline


@dataclass(frozen=True)
class AssistResult:
    """What Governed Assist did with one opportunity — explainable by construction."""
    selection: SelectedAction
    outcome: AssistOutcome
    record: InterventionRecord
    reason: str
    execution_ref: str = ""

    @property
    def executed(self) -> bool:
        return self.outcome is AssistOutcome.AUTO_EXECUTED


@dataclass
class GovernedAssist:
    """Runs the safest recommendations automatically and hands everything else to a human. Capabilities
    are keyed by ``action_kind``; only READ-tier actions with a registered capability auto-execute."""
    intervention_store: InterventionStore
    policy_version: str = "assist-1"
    max_auto_tier: RiskTier = RiskTier.READ         # the hard cap — deliberately READ for the first cut
    capabilities: Dict[str, Capability] = field(default_factory=dict)
    policy: Optional[PriorityPolicy] = None
    _clock: Callable[[], float] = field(default=lambda: __import__("time").time())

    def register(self, action_kind: str, capability: Capability) -> None:
        """Register the handler that performs a READ-tier ``action_kind`` (e.g. gather evidence)."""
        self.capabilities[action_kind] = capability

    def handle(self, opportunity: DecisionOpportunity, *, proposed_at: float,
               utility_fn: Optional[UtilityFn] = None, evidence_refs: Tuple[str, ...] = (),
               now: Optional[float] = None) -> AssistResult:
        """Select an action, then decide whether to run it automatically. Records a durable, immutable
        InterventionRecord for whatever happens (executed or not) before returning."""
        sel = select_action(opportunity, self.policy, utility_fn=utility_fn)
        action = sel.decision.action
        kind = sel.action.action_kind

        if action is Action.ABSTAIN:
            return self._record(sel, AssistOutcome.ABSTAINED, proposed_at, evidence_refs,
                                reason="Priority Engine chose do-nothing")
        if action is Action.DEFER:
            return self._record(sel, AssistOutcome.DEFERRED, proposed_at, evidence_refs,
                                reason="worth doing but not now / beyond the attention budget")
        if action is Action.REQUEST_APPROVAL:
            return self._record(sel, AssistOutcome.ROUTED_FOR_APPROVAL, proposed_at, evidence_refs,
                                reason="consequential — a human must approve")

        # action is ACT — but Governed Assist auto-runs only the safest tier, with a real capability.
        if int(sel.action.risk_tier) > int(self.max_auto_tier):
            return self._record(sel, AssistOutcome.ROUTED_FOR_APPROVAL, proposed_at, evidence_refs,
                                reason=(f"{sel.action.risk_tier.name} above the auto cap "
                                        f"{self.max_auto_tier.name} — approval required"))
        capability = self.capabilities.get(kind)
        if capability is None:
            return self._record(sel, AssistOutcome.ROUTED_FOR_APPROVAL, proposed_at, evidence_refs,
                                reason=f"no capability registered for '{kind}' — routed to a human")
        try:
            execution_ref = capability(sel) or uuid.uuid4().hex
        except Exception as e:                       # fail safe: attempted, not executed → ask a human
            return self._record(sel, AssistOutcome.ROUTED_FOR_APPROVAL, proposed_at, evidence_refs,
                                reason=f"capability for '{kind}' failed ({e!r}) — routed to a human")
        at = self._clock() if now is None else now
        return self._record(sel, AssistOutcome.AUTO_EXECUTED, proposed_at, evidence_refs,
                            reason=f"READ-tier '{kind}' ran automatically", executed_at=at,
                            execution_ref=execution_ref)

    def _record(self, sel: SelectedAction, outcome: AssistOutcome, proposed_at: float,
                evidence_refs: Tuple[str, ...], *, reason: str, executed_at: Optional[float] = None,
                execution_ref: str = "") -> AssistResult:
        rec = record_from_selection(
            sel, intervention_id=uuid.uuid4().hex, policy_version=self.policy_version,
            proposed_at=proposed_at, evidence_refs=evidence_refs, executed_at=executed_at,
            execution_ref=execution_ref)
        self.intervention_store.append(rec)          # durable BEFORE anything is surfaced
        return AssistResult(selection=sel, outcome=outcome, record=rec, reason=reason,
                            execution_ref=execution_ref)

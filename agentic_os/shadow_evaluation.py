"""Shadow evaluation — prospective agreement + outcome-conditioned analysis (PR-sequence.odt PR 6).

In Shadow mode the runtime RECOMMENDS but does not act; the human acts; the outcome is observed and
attributed. Over time that gives, per opportunity, the triple the odt calls far richer than passively
accumulating outcomes:

    (runtime recommendation, human's actual action, observed outcome)

`run_shadow_step` captures the first two for one opportunity (record the runtime recommendation, then
the human's action — both durable, PR2/PR3). Outcomes flow in via the PR4 derivation as observations
arrive. `evaluate_shadow` then joins all three and reports:

  * agreement / override rate (does the runtime agree with expert behaviour?);
  * mean reward AFTER AGREEMENT vs AFTER DISAGREEMENT;
  * abstention_vindicated — the runtime recommended NOT acting, the human acted anyway, and it went
    badly (evidence, not proof, that doing nothing was better — the Priority Engine's key invariant).

Honesty: in Shadow we only observe the outcome of the action ACTUALLY taken (the human's); the
counterfactual of the runtime's recommendation when they disagree is not observed. Humans are not
treated as ground truth. No learning happens here — this only measures.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from agentic_os.intervention_record import (
    ActorType, InterventionRecord, record_human_action, select_and_record)
from agentic_os.priority_engine import DecisionOpportunity, PriorityPolicy, SelectedAction, UtilityFn

#: action labels that mean "the runtime chose NOT to intervene".
NON_ACTION = {"wait", "do_not_contact", "monitor", "do nothing", "abstain"}


def run_shadow_step(opportunity: DecisionOpportunity, intervention_store, *, human_action: str,
                    policy_version: str, proposed_at: float, human_at: float,
                    policy: Optional[PriorityPolicy] = None, utility_fn: Optional[UtilityFn] = None,
                    evidence_refs: Tuple[str, ...] = ()) -> Tuple[SelectedAction, InterventionRecord, InterventionRecord]:
    """One shadow-mode step: record the runtime's recommendation (durable, not executed) AND the human's
    actual action for the same opportunity. Returns (selection, runtime_record, human_record)."""
    sel, runtime_rec = select_and_record(opportunity, intervention_store, policy_version=policy_version,
                                         proposed_at=proposed_at, policy=policy, utility_fn=utility_fn,
                                         evidence_refs=evidence_refs)
    human_rec = record_human_action(intervention_store, opportunity_id=opportunity.opportunity_id,
                                    action_kind=human_action, at=human_at)
    return sel, runtime_rec, human_rec


@dataclass(frozen=True)
class ShadowOutcome:
    opportunity_id: str
    runtime_action: str
    human_action: str
    reward: Optional[float]           # realized outcome of the opportunity (attributed to the action taken)

    @property
    def agreed(self) -> bool:
        return self.runtime_action == self.human_action


@dataclass(frozen=True)
class ShadowEvaluationReport:
    outcomes: Tuple[ShadowOutcome, ...]

    @property
    def paired(self) -> int:
        return len(self.outcomes)

    @property
    def agreement_rate(self) -> float:
        return (sum(o.agreed for o in self.outcomes) / self.paired) if self.paired else 0.0

    @property
    def override_rate(self) -> float:
        return 1.0 - self.agreement_rate if self.paired else 0.0

    def _mean_reward(self, agreed: bool) -> Optional[float]:
        rs = [o.reward for o in self.outcomes if o.agreed is agreed and o.reward is not None]
        return (sum(rs) / len(rs)) if rs else None

    @property
    def mean_reward_when_agreed(self) -> Optional[float]:
        return self._mean_reward(True)

    @property
    def mean_reward_when_disagreed(self) -> Optional[float]:
        return self._mean_reward(False)

    @property
    def abstention_vindicated(self) -> int:
        """Runtime recommended NOT acting, the human acted anyway, and the outcome was negative — a
        (non-causal) signal that doing nothing would have been better."""
        return sum(1 for o in self.outcomes
                   if o.runtime_action in NON_ACTION and o.human_action not in NON_ACTION
                   and o.reward is not None and o.reward < 0)


def evaluate_shadow(intervention_store, outcome_store) -> ShadowEvaluationReport:
    """Join runtime recommendation + human action + realized outcome per opportunity. The opportunity's
    reward is the attribution-weighted reward of the outcomes attributed to ANY of its interventions
    (in Shadow, that's the human's action)."""
    runtime: Dict[str, str] = {}
    human: Dict[str, str] = {}
    iids_by_opp: Dict[str, set] = {}
    for r in intervention_store.all():
        iids_by_opp.setdefault(r.opportunity_id, set()).add(r.intervention_id)
        (human if r.actor_type == ActorType.HUMAN else runtime).setdefault(r.opportunity_id, r.selected_action)

    reward_by_iid: Dict[str, float] = {}
    for ev in outcome_store.load():
        if ev.selected_intervention_id:
            reward_by_iid[ev.selected_intervention_id] = (
                reward_by_iid.get(ev.selected_intervention_id, 0.0) + ev.scalar_reward())

    outcomes: List[ShadowOutcome] = []
    for opp in sorted(set(runtime) & set(human)):          # only opportunities with BOTH
        iids = iids_by_opp.get(opp, set())
        rewarded = [reward_by_iid[i] for i in iids if i in reward_by_iid]
        outcomes.append(ShadowOutcome(opp, runtime[opp], human[opp],
                                      sum(rewarded) if rewarded else None))
    return ShadowEvaluationReport(tuple(outcomes))

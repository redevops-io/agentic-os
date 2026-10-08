"""Decision producer for agentic-crm (runtime-native, plan §2.1/§3.4).

The kernel's ``crm_nba`` producer turns a deal's state into a ``DecisionOpportunity`` of deal-stage
actions (send_proposal, schedule_call, nurture_email, …). agentic-crm can only *score*, *research*,
*draft* (never auto-send — a human sends) and *qualify*. So this adapter remaps each producer action
to the capability the app ACTUALLY performs, which is also what kills the phantom capabilities: the
candidates leave here pointing only at capabilities the operator registers.
"""
from __future__ import annotations

from dataclasses import replace

from agentic_os.crm_nba import DealSignals, next_best_action
from agentic_os.priority_engine import DecisionOpportunity

# producer action_kind -> the agentic-crm capability that realises it (honest: we draft, never send).
_CAP_MAP = {
    "answer_question": "crm.research",
    "send_proposal": "crm.draft_outreach",
    "schedule_call": "crm.draft_outreach",
    "nurture_email": "crm.draft_outreach",
    "renewal_outreach": "crm.draft_outreach",
    # 'monitor' maps to no capability -> dropped; select_action re-injects the do-nothing baseline.
}

#: the capabilities this producer's candidates may require (used by the AppManifest + phantom check).
EMITTED_CAPABILITIES = tuple(sorted(set(_CAP_MAP.values())))


def crm_opportunity(signals: DealSignals) -> DecisionOpportunity:
    """Build the agentic-crm decision opportunity: crm_nba's candidates, remapped to real capabilities."""
    opp = next_best_action(signals)
    remapped = []
    for cand in opp.candidate_actions:
        cap = _CAP_MAP.get(cand.action_kind)
        if cap is None:
            continue  # drop unmapped (e.g. 'monitor') — the engine keeps do-nothing as the baseline
        remapped.append(replace(cand, required_capabilities=(cap,)))
    return replace(opp, candidate_actions=tuple(remapped))


__all__ = ["DealSignals", "EMITTED_CAPABILITIES", "crm_opportunity"]

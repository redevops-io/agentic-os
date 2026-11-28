"""Live commercial-outcome connector for the CRM producer (Stage A, 2026-11-15).

``agentic_os.crm_nba`` already maps observed commercial events (reply, meeting,
conversion, loss) into the shared :class:`~agentic_os.priority_engine.OutcomeEvent`
shape via :func:`~agentic_os.crm_nba.crm_outcome` — but nothing has ever called that
mapping function from a real event source; every existing caller is a simulation
harness (``outcome_learning_backtest.py``'s acceptance test, this module's own tests
below). This module is the data-source connection, and nothing else: it reads real
opportunity-stage changes from Twenty (the read-only sensor in
``agentic_os.integrations.twenty``, already live and already used by the revenue
leakage detectors) and turns a terminal stage into one real, attributed
``OutcomeEvent`` recorded into an existing ``OutcomeLog``.

Why Twenty READ access is enough — no write path or CRM-migration connector needed:
Twenty's opportunity object already carries the stage, including the terminal
``won``/``customer``/``lost`` forms that :func:`~agentic_os.integrations.twenty.to_opportunity`
normalizes to the canonical ``closed_won``/``closed_lost`` — exactly what
``crm_outcome``'s ``converted``/``lost`` dimensions need. Richer mid-funnel signals
(``replied``, ``meeting``) aren't exposed by Twenty's opportunity object alone and are
left for a future, separately-scoped connector; this module wires exactly the signal
that is already real and already reachable today.

What this module deliberately does NOT do (Stage A PR scope,
``~/Documents/REDEVOPS_STAGE_A_PR_SCOPE_2026-11-15.md``):
- It does not change ``priority_engine.select_action``, ``outcome_learning.UtilityModel``,
  or either ``policy_promotion.py`` gate. It only appends real ``OutcomeEvent``s to the
  ``OutcomeLog`` those already read.
- It does not call any promotion path. Recording a real outcome is not a promotion.
- It does not write to Twenty. It only reads opportunities, exactly as
  ``integrations.twenty.scan_stalled`` already does.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from agentic_os.crm_nba import crm_outcome
from agentic_os.integrations.twenty import TwentyClient, to_opportunity
from agentic_os.priority_engine import InterventionCandidate, OutcomeEvent, OutcomeLog, SelectedAction

_MS_PER_HOUR = 3_600_000.0


@dataclass(frozen=True)
class PendingCrmSelection:
    """One outstanding CRM selection awaiting a real-world commercial outcome."""

    action: InterventionCandidate
    selected_at_ms: int


@dataclass
class CrmSelectionTracker:
    """Join key between 'which action the loop selected for this deal' and 'what Twenty
    later reports for that same deal' — the correlation a real outcome needs that a
    simulation harness doesn't. In-memory here, matching ``OutcomeLog``'s own documented
    pattern ("in-memory here; a deployment persists it"); a real deployment would persist
    this the same way it persists the log.
    """

    _pending: Dict[str, PendingCrmSelection] = field(default_factory=dict)

    def record_selection(self, opportunity_ref: str, selected: SelectedAction, *, now_ms: int) -> None:
        """Call this right after ``select_action`` for a real deal tied to a real Twenty
        opportunity id. Does not call ``select_action`` itself — the caller already did."""
        self._pending[opportunity_ref] = PendingCrmSelection(selected.action, now_ms)

    def peek(self, opportunity_ref: str) -> Optional[PendingCrmSelection]:
        return self._pending.get(opportunity_ref)

    def resolve(self, opportunity_ref: str) -> None:
        self._pending.pop(opportunity_ref, None)

    def __len__(self) -> int:
        return len(self._pending)


def poll_twenty_outcomes(client: TwentyClient, tracker: CrmSelectionTracker, log: OutcomeLog,
                          *, now_ms: int, limit: int = 200) -> List[OutcomeEvent]:
    """Read real Twenty opportunities; for each one with an outstanding selection that has
    now reached a terminal stage, map it to a real ``OutcomeEvent`` via
    ``crm_nba.crm_outcome`` and record it into ``log``. Opportunities with no outstanding
    selection, or not yet terminal, are left untouched (not removed from ``tracker``) —
    they're observed again on a later poll.

    Calls nothing from ``priority_engine``, ``outcome_learning``, or ``policy_promotion``.
    """
    recorded: List[OutcomeEvent] = []
    for raw in client.opportunities(limit=limit):
        opportunity_ref = str(raw.get("id", "") or "")
        if not opportunity_ref:
            continue
        pending = tracker.peek(opportunity_ref)
        if pending is None:
            continue  # no selection ever recorded for this opportunity -- nothing to attribute
        opportunity, _last_activity_ms = to_opportunity(raw)
        delay_hours = max(0.0, (now_ms - pending.selected_at_ms) / _MS_PER_HOUR)
        if opportunity.stage == "closed_won":
            ev = crm_outcome(pending.action, converted=True, delay_hours=delay_hours)
        elif opportunity.stage == "closed_lost":
            ev = crm_outcome(pending.action, lost=True, delay_hours=delay_hours)
        else:
            continue  # still open -- leave the pending selection in place, poll again later
        recorded.append(log.record(ev))
        tracker.resolve(opportunity_ref)
    return recorded

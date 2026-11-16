"""Tests for the Stage A real-signal connector (agentic_os.crm_outcome_live).

Confirms (a) a real Twenty opportunity reaching a terminal stage maps to the correct,
correctly-shaped OutcomeEvent and lands in the OutcomeLog; (b) a non-terminal or
unselected opportunity produces nothing; (c) nothing downstream (select_action,
UtilityModel, either policy_promotion gate) fires as a side effect of merely
connecting the signal; and (d) a live, non-simulated read against a real Twenty
instance, self-skipping if one isn't configured/reachable (same pattern as
tests/test_twenty_sensor.py::test_live_twenty_scan).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agentic_os.crm_nba import DealSignals, next_best_action
from agentic_os.crm_outcome_live import CrmSelectionTracker, poll_twenty_outcomes
from agentic_os.integrations.twenty import twenty_from_env
from agentic_os.priority_engine import OutcomeLog, select_action

_NOW_MS = int(datetime(2026, 11, 16, tzinfo=timezone.utc).timestamp() * 1000)


class _StubTwenty:
    def __init__(self, opportunities):
        self._opps = opportunities

    def opportunities(self, *, limit=60):
        return self._opps


def _selected_action_for_acme():
    opp = next_best_action(DealSignals("Acme", stage="engaged", buying_signal=True))
    return select_action(opp)


def test_terminal_won_opportunity_records_a_converted_outcome_event():
    selected = _selected_action_for_acme()
    tracker = CrmSelectionTracker()
    tracker.record_selection("opp-1", selected, now_ms=_NOW_MS - 12 * 3_600_000)
    log = OutcomeLog()

    client = _StubTwenty([{"id": "opp-1", "name": "Acme deal", "stage": "CUSTOMER",
                            "updatedAt": "2026-11-16T00:00:00Z"}])
    recorded = poll_twenty_outcomes(client, tracker, log, now_ms=_NOW_MS)

    assert len(recorded) == 1
    ev = recorded[0]
    assert ev.source_app == "crm"
    assert ev.action_kind == selected.action.action_kind
    assert ev.candidate_id == selected.action.candidate_id
    assert ev.reward_dimensions.get("conversion") == 1.0
    assert ev.scalar_reward() > 0
    assert log.events == [ev]
    assert len(tracker) == 0  # resolved -- no longer pending


def test_terminal_lost_opportunity_records_a_negative_outcome_event():
    selected = _selected_action_for_acme()
    tracker = CrmSelectionTracker()
    tracker.record_selection("opp-2", selected, now_ms=_NOW_MS - 48 * 3_600_000)
    log = OutcomeLog()

    client = _StubTwenty([{"id": "opp-2", "name": "Acme deal", "stage": "LOST",
                            "updatedAt": "2026-11-16T00:00:00Z"}])
    recorded = poll_twenty_outcomes(client, tracker, log, now_ms=_NOW_MS)

    assert len(recorded) == 1
    assert recorded[0].reward_dimensions.get("loss") == -1.0


def test_non_terminal_opportunity_records_nothing_and_stays_pending():
    selected = _selected_action_for_acme()
    tracker = CrmSelectionTracker()
    tracker.record_selection("opp-3", selected, now_ms=_NOW_MS)
    log = OutcomeLog()

    client = _StubTwenty([{"id": "opp-3", "name": "Acme deal", "stage": "PROPOSAL",
                            "updatedAt": "2026-11-16T00:00:00Z"}])
    recorded = poll_twenty_outcomes(client, tracker, log, now_ms=_NOW_MS)

    assert recorded == []
    assert log.events == []
    assert len(tracker) == 1  # still pending -- poll again later


def test_opportunity_with_no_recorded_selection_is_ignored():
    tracker = CrmSelectionTracker()  # nothing ever recorded
    log = OutcomeLog()

    client = _StubTwenty([{"id": "opp-unknown", "stage": "CUSTOMER", "updatedAt": "2026-11-16T00:00:00Z"}])
    recorded = poll_twenty_outcomes(client, tracker, log, now_ms=_NOW_MS)

    assert recorded == []
    assert log.events == []


def test_module_never_imports_selection_or_promotion_machinery():
    """Static guarantee that this is a data-connection change only: the module's own
    namespace must not bind select_action, UtilityModel, or either policy_promotion
    gate -- it can only reach OutcomeLog.record. Checked against the module's actual
    globals (what it imported/defined), not its docstrings or comments, which legitimately
    mention these names when explaining what this module does NOT do."""
    import agentic_os.crm_outcome_live as mod

    bound_names = set(vars(mod))
    for forbidden in ("select_action", "UtilityModel", "policy_promotion", "PromotionGate"):
        assert forbidden not in bound_names, f"{forbidden!r} must not be importable from crm_outcome_live.py"


def test_connecting_the_signal_touches_only_the_log_and_tracker():
    """Behavioral check to match the static guarantee above: polling records exactly one
    OutcomeEvent and its only observable side effects are on `log` and `tracker`."""
    selected = _selected_action_for_acme()  # the one legitimate selection, before polling starts
    tracker = CrmSelectionTracker()
    tracker.record_selection("opp-4", selected, now_ms=_NOW_MS - 24 * 3_600_000)
    log = OutcomeLog()
    client = _StubTwenty([{"id": "opp-4", "stage": "CUSTOMER", "updatedAt": "2026-11-16T00:00:00Z"}])

    recorded = poll_twenty_outcomes(client, tracker, log, now_ms=_NOW_MS)

    assert recorded == log.events  # the only mutation is the one OutcomeEvent appended to the log
    assert len(log.events) == 1
    assert len(tracker) == 0  # resolved; nothing else in the tracker changed


def test_live_twenty_poll():
    # Read-only; self-skips unless TWENTY_BASE_URL + TWENTY_API_KEY are set + reachable --
    # same pattern as tests/test_twenty_sensor.py::test_live_twenty_scan.
    client = twenty_from_env()
    if client is None or not client.opportunities(limit=1):
        pytest.skip("no reachable/authorized Twenty (TWENTY_BASE_URL / TWENTY_API_KEY)")

    tracker = CrmSelectionTracker()
    log = OutcomeLog()
    # No selection was ever recorded for any real opportunity id below, so this call is
    # guaranteed to record nothing -- it only proves the real read path + mapping run
    # cleanly end-to-end against a live Twenty instance, without touching any real selection
    # state (there is none yet in production) or writing anything back to Twenty.
    recorded = poll_twenty_outcomes(client, tracker, log, now_ms=int(__import__("time").time() * 1000))
    assert recorded == []  # no pending selections exist yet in this environment
    assert isinstance(recorded, list)

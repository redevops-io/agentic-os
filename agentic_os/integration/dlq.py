"""Integration plane — exception / dead-letter plane with replay (§9).

Failures become durable domain objects, not log lines: rankable by business impact, and replayable from the
failed point after the mapping/credential/outage is fixed — without blindly rerunning the whole workflow.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Callable, Optional

from .contracts import ExceptionCategory, IntegrationException
from .obligations import DischargeResult

# How urgent each failure class is (higher = surfaced first). Money + overdue SLAs rank highest.
_PRIORITY: dict[ExceptionCategory, int] = {
    ExceptionCategory.RECONCILIATION_VARIANCE: 100,
    ExceptionCategory.OBLIGATION_OVERDUE: 90,
    ExceptionCategory.OBLIGATION_UNSATISFIED: 80,
    ExceptionCategory.OBLIGATION_CONFLICT: 70,
    ExceptionCategory.IDENTITY_CONFLICT: 60,
    ExceptionCategory.ACTION_FAILED: 50,
}


def priority_score(exc: IntegrationException) -> int:
    base = _PRIORITY.get(exc.category, 40)
    return base + (5 if exc.business_impact else 0) + min(10, exc.retry_count)


# replay_fn(exception) -> DischargeResult   (re-attempt the obligation after a fix)
ReplayFn = Callable[[IntegrationException], DischargeResult]


class ExceptionStore:
    """Durable, rankable, replayable exceptions (in-memory with a persistence seam)."""

    def __init__(self) -> None:
        self._by_id: dict[str, IntegrationException] = {}

    def append(self, exc: IntegrationException) -> IntegrationException:
        self._by_id[exc.exception_id] = exc
        return exc

    def get(self, exception_id: str) -> Optional[IntegrationException]:
        return self._by_id.get(exception_id)

    def open(self) -> list[IntegrationException]:
        return [e for e in self._by_id.values() if e.status == "open"]

    def ranked(self) -> list[IntegrationException]:
        """Open exceptions, most-urgent first — the 'things that should have happened but haven't' queue."""
        return sorted(self.open(), key=priority_score, reverse=True)

    def resolve(self, exception_id: str) -> None:
        exc = self._by_id.get(exception_id)
        if exc is not None:
            self._by_id[exception_id] = replace(exc, status="resolved")

    def replay(self, exception_id: str, replay_fn: ReplayFn) -> "DischargeResult | None":
        """Re-attempt the obligation behind this exception. On success, mark it resolved (and leave a new
        satisfied obligation/receipt); on continued failure it stays open with an incremented retry count."""
        exc = self._by_id.get(exception_id)
        if exc is None or exc.status != "open":
            return None
        result = replay_fn(exc)
        if result.satisfied:
            self.resolve(exception_id)
        else:
            self._by_id[exception_id] = replace(exc, retry_count=exc.retry_count + 1)
        return result

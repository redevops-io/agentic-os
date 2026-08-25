"""Budget Governor — the cross-channel spend cap (GTM doc §35-51 paid-acquisition plane).

Per-placement approval (a founder saying yes to *this* ad, *this* sponsorship) is necessary but not
sufficient: a founder can approve five individually-reasonable placements that together blow the campaign
budget. The Budget Governor is the single source of truth for "can we still spend": it holds a total budget
and optional per-channel ceilings, and every commit must pass ``authorize`` first. Spend that would exceed
the remaining total, or a channel's ceiling, is refused — so overspend is structurally 0 even when every
individual placement was approved. It composes with, and never replaces, the per-action approval gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass(frozen=True)
class SpendAuthorization:
    allowed: bool
    reason: str
    channel: str
    amount: float

    def to_dict(self) -> Dict[str, object]:
        return dict(self.__dict__)


@dataclass
class BudgetGovernor:
    """Tracks committed spend against a total budget and optional per-channel ceilings. ``authorize`` is a
    pure check (no side effect); ``commit`` authorizes then records. The ceilings are set once and never
    widened by the agent — the governor reads them, it does not negotiate them."""
    total_budget: float
    channel_ceilings: Dict[str, float] = field(default_factory=dict)
    period: str = "campaign"
    _committed: Dict[str, float] = field(default_factory=dict)

    def committed_total(self) -> float:
        return round(sum(self._committed.values()), 2)

    def remaining(self) -> float:
        return round(self.total_budget - self.committed_total(), 2)

    def remaining_for(self, channel: str) -> float:
        room = self.remaining()
        cap = self.channel_ceilings.get(channel)
        if cap is not None:
            room = min(room, cap - self._committed.get(channel, 0.0))
        return round(max(0.0, room), 2)

    def authorize(self, channel: str, amount: float) -> SpendAuthorization:
        """May this spend proceed? Refuses non-positive amounts, anything over the remaining total, and
        anything over the channel's remaining ceiling — each with a named reason."""
        if amount <= 0:
            return SpendAuthorization(False, "non-positive amount", channel, amount)
        if amount > self.remaining():
            return SpendAuthorization(False, f"exceeds remaining budget ${self.remaining():.0f}",
                                      channel, amount)
        cap = self.channel_ceilings.get(channel)
        if cap is not None and self._committed.get(channel, 0.0) + amount > cap:
            return SpendAuthorization(False, f"exceeds {channel} channel ceiling ${cap:.0f}", channel, amount)
        return SpendAuthorization(True, "authorized", channel, amount)

    def commit(self, channel: str, amount: float) -> SpendAuthorization:
        """Authorize, and on success record the spend. A refused commit changes nothing."""
        auth = self.authorize(channel, amount)
        if auth.allowed:
            self._committed[channel] = round(self._committed.get(channel, 0.0) + amount, 2)
        return auth

    def ledger(self) -> Dict[str, object]:
        return {"total_budget": self.total_budget, "committed": dict(self._committed),
                "committed_total": self.committed_total(), "remaining": self.remaining(),
                "channel_ceilings": dict(self.channel_ceilings), "period": self.period}

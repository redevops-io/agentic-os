"""Provider-value learning + cost-aware routing (Intelligence-APIs plan §4, §10 — Broker learning, Phase 6).

The EvidenceValueStore records, per lookup, whether the evidence changed the decision and whether the
verified outcome was beneficial. This layer turns that ledger into learned priors per (provider,
capability) and a value estimate the broker's gate uses to stop spending on evidence that has repeatedly
failed to earn its cost — the "prove when a paid call changed a decision and whether the outcome justified
it" loop.

It changes no governance: `value_fn_from_store` returns a value estimate for `resolve_decision_need`'s
existing decision-value gate, so a low-value capability is skipped (continue with local evidence) rather
than bought. Capabilities without enough history default to explore (value 1.0), so the loop keeps
learning instead of freezing early.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from runtime_contracts.protocol import EvidenceRequest

from .value_store import EvidenceValueStore

_INF = float("inf")


@dataclass(frozen=True)
class ProviderValuePrior:
    provider: str
    capability: str
    lookups: int
    total_cost: float
    changed_rate: float           # changed decisions / lookups
    beneficial_rate: float        # verified-beneficial changed decisions / lookups
    cost_per_beneficial: float    # total_cost / verified-beneficial (inf when none)
    value_estimate: float         # [0,1] learned decision-value prior
    recommend: str                # buy | trial | stop


def learn_priors(store: EvidenceValueStore, *, min_lookups: int = 5,
                 stop_threshold: float = 0.05) -> dict[tuple[str, str], ProviderValuePrior]:
    """Aggregate the value ledger into a prior per (provider, capability). `recommend`:
    trial (too little history), stop (enough history, beneficial rate below threshold), else buy."""
    priors: dict[tuple[str, str], ProviderValuePrior] = {}
    for (provider, capability), a in store.summary().items():
        n = a["lookups"] or 0
        changed_rate = a["changed"] / n if n else 0.0
        beneficial_rate = a["verified_beneficial"] / n if n else 0.0
        cpb = (a["cost"] / a["verified_beneficial"]) if a["verified_beneficial"] else _INF
        value = round(0.7 * beneficial_rate + 0.3 * changed_rate, 4)
        if n < min_lookups:
            rec = "trial"
        elif beneficial_rate < stop_threshold:
            rec = "stop"
        else:
            rec = "buy"
        priors[(provider, capability)] = ProviderValuePrior(
            provider, capability, n, round(a["cost"], 6), round(changed_rate, 4), round(beneficial_rate, 4),
            round(cpb, 6) if cpb != _INF else _INF, value, rec)
    return priors


def value_fn_from_store(store: EvidenceValueStore, *, min_lookups: int = 5,
                        default: float = 1.0) -> Callable[[EvidenceRequest], float]:
    """A learned decision-value function for `resolve_decision_need(value_fn=…)`. For a capability with
    enough history it returns the best provider's learned value; otherwise `default` (explore)."""
    priors = learn_priors(store, min_lookups=min_lookups)
    best: dict[str, float] = {}
    learned: set[str] = set()
    for (_provider, capability), p in priors.items():
        best[capability] = max(best.get(capability, 0.0), p.value_estimate)
        if p.lookups >= min_lookups:
            learned.add(capability)

    def value_fn(request: EvidenceRequest) -> float:
        cap = request.capability.value
        return best[cap] if cap in learned else default

    return value_fn


def routing_report(store: EvidenceValueStore, **kw) -> list[ProviderValuePrior]:
    """Priors ordered for review — the ones to stop buying first, then by ascending learned value."""
    order = {"stop": 0, "trial": 1, "buy": 2}
    return sorted(learn_priors(store, **kw).values(),
                  key=lambda p: (order.get(p.recommend, 3), p.value_estimate))

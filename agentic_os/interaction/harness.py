"""Customer-service scenario harness for the interaction stack (AGPL, offline).

NVIDIA's NeMo Voice Agent ships an evaluation harness of 328 customer-service scenarios across airline
/ retail / telecom (eva_airline 50, tau2_airline 50, tau2_retail 114, tau2_telecom 114). This module is
the ReDevOps-side harness that runs such scenarios through *our* interaction runtime and scores them —
so the same corpus measures the governed path, not just a bare voice agent.

What it measures: **intent-resolution accuracy** — given a scenario's user turns, does the resolver
reach the right closed-vocabulary objective (the thing the customer actually wanted)? That is exactly
what the NeMo scenarios grade, and it is the one decision that determines whether the right Mission
opens. The resolver is a seam: a deterministic ``KeywordIntentResolver`` baseline ships here, and a
real Discovery/LLM resolver drops into the same interface so a run reports *baseline vs learned*, the
edge-sentinel pattern.

Two honesty rules:
  * The 328 real scenarios are NOT bundled (they live in the NeMo repo, pinned during the dependency
    spike). ``load_scenarios`` reads them from a JSONL path when present; otherwise the harness runs a
    small, explicitly-labelled set of *representative* built-in fixtures. No real dataset is fabricated.
  * The boundary holds: the resolver reads the utterance ABOVE the boundary and emits a sealed
    objective; the utterance never crosses into the Mission (asserted in the interaction tests).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from runtime_contracts import Channel


@dataclass(frozen=True)
class InteractionScenario:
    """One graded conversation: user turns in, expected resolved objective out."""

    scenario_id: str
    domain: str                       # airline | retail | telecom | …
    turns: Tuple[str, ...]            # the customer's utterances (the "sentence")
    expected_objective: str          # the closed-vocab objective a correct handling resolves to
    channel: Channel = Channel.PHONE
    expected_fields: Mapping[str, str] = field(default_factory=dict)
    source: str = "builtin"          # "builtin" fixture vs a loaded dataset name


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    domain: str
    expected: str
    resolved: Optional[str]
    passed: bool
    reason: str = ""


@dataclass(frozen=True)
class SuiteReport:
    results: Tuple[ScenarioResult, ...]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def accuracy(self) -> float:
        return (self.passed / self.total) if self.total else 0.0

    def by_domain(self) -> Dict[str, Tuple[int, int]]:
        """domain -> (passed, total)."""
        out: Dict[str, List[int]] = {}
        for r in self.results:
            agg = out.setdefault(r.domain, [0, 0])
            agg[0] += int(r.passed)
            agg[1] += 1
        return {d: (p, t) for d, (p, t) in out.items()}

    def summary(self) -> str:
        parts = [f"{d}: {p}/{t}" for d, (p, t) in sorted(self.by_domain().items())]
        return f"{self.passed}/{self.total} resolved ({self.accuracy:.0%}) — " + "; ".join(parts)


# --- resolver seam ---------------------------------------------------------------------------------
# A resolver reads the user turns and returns (objective, fields), or None to abstain. Reading prose is
# its job — it sits ABOVE the boundary and its output (a sealed objective) is what crosses down.

Resolver = Callable[[Sequence[str], str], Optional[Tuple[str, Mapping[str, str]]]]


class KeywordIntentResolver:
    """Deterministic baseline: first matching keyword per domain wins. Intentionally imperfect — some
    scenarios are phrased to defeat keyword matching, so the baseline accuracy is a real number below
    100%, the floor a learned resolver must beat."""

    #: domain -> ordered (objective, keywords) rules
    RULES: Mapping[str, Sequence[Tuple[str, Tuple[str, ...]]]] = {
        "airline": (
            ("cancel_flight", ("cancel", "cancellation")),
            ("change_seat", ("seat", "aisle", "window")),
            ("check_baggage", ("bag", "baggage", "luggage", "suitcase")),
            ("book_flight", ("book", "flight", "fly", "ticket")),
        ),
        "retail": (
            ("return_item", ("return", "send back")),
            ("refund_status", ("refund", "money back")),
            ("exchange_item", ("exchange", "swap", "different size")),
            ("track_order", ("track", "where", "order", "delivery", "shipment")),
        ),
        "telecom": (
            ("report_outage", ("outage", "no signal", "down", "not working")),
            ("pay_bill", ("bill", "pay", "invoice", "charge")),
            ("port_number", ("port", "transfer my number", "keep my number")),
            ("upgrade_plan", ("upgrade", "plan", "more data", "faster")),
        ),
    }

    def __call__(self, turns: Sequence[str], domain: str) -> Optional[Tuple[str, Mapping[str, str]]]:
        text = " ".join(turns).lower()
        for objective, keywords in self.RULES.get(domain, ()):  # first rule that hits wins
            if any(k in text for k in keywords):
                return objective, {}
        return None


def run_scenario(scenario: InteractionScenario, *, resolver: Resolver) -> ScenarioResult:
    resolved = resolver(scenario.turns, scenario.domain)
    objective = resolved[0] if resolved else None
    passed = objective == scenario.expected_objective
    reason = "resolved" if passed else (f"got {objective!r}" if objective else "abstained")
    return ScenarioResult(scenario.scenario_id, scenario.domain, scenario.expected_objective,
                          objective, passed, reason)


def run_suite(scenarios: Sequence[InteractionScenario], *, resolver: Resolver) -> SuiteReport:
    return SuiteReport(tuple(run_scenario(s, resolver=resolver) for s in scenarios))


# --- dataset loading -------------------------------------------------------------------------------

def load_scenarios(path: str, *, source: str = "") -> List[InteractionScenario]:
    """Load scenarios from a JSONL file (one object per line) — how the real NeMo corpus plugs in once
    the dependency spike pins it. Each line: {scenario_id, domain, turns[], expected_objective,
    channel?, expected_fields?}. Unknown fields are ignored; malformed lines are skipped."""
    out: List[InteractionScenario] = []
    with open(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                out.append(InteractionScenario(
                    scenario_id=str(d["scenario_id"]), domain=str(d["domain"]),
                    turns=tuple(d.get("turns", [])), expected_objective=str(d["expected_objective"]),
                    channel=Channel(d.get("channel", "phone")),
                    expected_fields=dict(d.get("expected_fields", {})),
                    source=source or "loaded",
                ))
            except (KeyError, ValueError, TypeError):
                continue
    return out


def builtin_scenarios() -> List[InteractionScenario]:
    """A small, explicitly-illustrative set across the three NeMo domains — NOT the real 328. Enough to
    exercise the harness offline and give the baseline resolver a realistic (sub-100%) score."""
    raw: Sequence[Tuple[str, str, str, Tuple[str, ...]]] = (
        # (id, domain, expected_objective, turns)
        ("air-1", "airline", "book_flight", ("I'd like to book a flight to Denver next Tuesday",)),
        ("air-2", "airline", "cancel_flight", ("I need to cancel my reservation",)),
        ("air-3", "airline", "change_seat", ("Can I move to a window seat",)),
        ("air-4", "airline", "check_baggage", ("How many bags can I bring",)),
        ("air-5", "airline", "book_flight", ("What's the earliest I can get to Boston tomorrow morning",)),
        ("ret-1", "retail", "track_order", ("Where is my order, it hasn't arrived",)),
        ("ret-2", "retail", "return_item", ("I want to return the jacket I bought",)),
        ("ret-3", "retail", "refund_status", ("Has my refund gone through yet",)),
        ("ret-4", "retail", "exchange_item", ("Can I exchange this for a different size",)),
        ("tel-1", "telecom", "report_outage", ("My internet is down and there's no signal",)),
        ("tel-2", "telecom", "pay_bill", ("I want to pay my bill",)),
        ("tel-3", "telecom", "upgrade_plan", ("I'd like to upgrade to more data",)),
        ("tel-4", "telecom", "port_number", ("I'm switching but want to keep my number",)),
    )
    return [InteractionScenario(scenario_id=i, domain=d, expected_objective=o, turns=t)
            for (i, d, o, t) in raw]

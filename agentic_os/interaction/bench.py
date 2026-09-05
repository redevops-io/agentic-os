"""Offline runner for the customer-service interaction harness.

`python -m agentic_os.interaction.bench` runs the baseline resolver over the built-in representative
scenarios and prints a per-domain score. Point ``INTERACTION_SCENARIOS`` at a JSONL export of the real
NeMo corpus (once the dependency spike pins it) to score that instead — the same harness, real data.
"""
from __future__ import annotations

import os
from typing import List

from .harness import (
    InteractionScenario,
    KeywordIntentResolver,
    SuiteReport,
    builtin_scenarios,
    load_scenarios,
    run_suite,
)


def load() -> List[InteractionScenario]:
    path = os.environ.get("INTERACTION_SCENARIOS", "")
    if path and os.path.exists(path):
        loaded = load_scenarios(path, source=os.path.basename(path))
        if loaded:
            return loaded
    return builtin_scenarios()


def run() -> SuiteReport:
    return run_suite(load(), resolver=KeywordIntentResolver())


def main() -> None:  # pragma: no cover - thin CLI
    scenarios = load()
    src = scenarios[0].source if scenarios else "none"
    report = run_suite(scenarios, resolver=KeywordIntentResolver())
    print(f"interaction harness — source: {src}")
    print(f"  baseline resolver: {report.summary()}")
    misses = [r for r in report.results if not r.passed]
    if misses:
        print("  misses (headroom for a learned/LLM resolver):")
        for r in misses[:10]:
            print(f"    {r.scenario_id} [{r.domain}] expected {r.expected} — {r.reason}")


if __name__ == "__main__":  # pragma: no cover
    main()

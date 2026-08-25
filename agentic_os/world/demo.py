"""Headless runner: `python -m agentic_os.world.demo [world_id]` — prints the trace + benchmark scorecard.

Offline, no network, no OSS core: the governed loop runs over a dataset world and emits the visual trace,
outcome, metrics and baseline comparison. The animated canvas demo consumes the same WorldRun JSON.
"""
from __future__ import annotations

import json
import sys

from runtime_contracts import AuthorityContext, PrincipalRef

from . import ALL_WORLDS, BenchmarkRunner, ScenarioOrchestrator

_ANSWERS = {"What is the roof pitch?": "6/12", "Approve invoice correction?": "yes"}
_SCOPES = ("read:crm", "read:geo", "write:quote", "write:crm", "read:secrets", "write:vendor", "write:billing")


def _authority():
    return AuthorityContext(authority_id="ctx", principal=PrincipalRef(id="operator", tenant="demo"),
                            purpose="run world", scope=_SCOPES)


def run(world_id: str = "after-hours-lead") -> dict:
    world = ALL_WORLDS[world_id]
    auth = _authority()
    run = ScenarioOrchestrator().run(world, seed="8842", authority=auth, answers=_ANSWERS)
    card = BenchmarkRunner().run(world, seed="8842", authority=auth, answers=_ANSWERS)
    return {"run": run.to_dict(), "scorecard": card.to_dict()}


def _print(world_id: str) -> None:
    out = run(world_id)
    r, card = out["run"], out["scorecard"]
    print(f"\n=== {world_id} · {r['mode']} · verified={r['metrics']['verified']} ===")
    for m in r["trace"]["milestones"]:
        ny = f"  ⚠ {m['needs_you']}" if m.get("needs_you") else ""
        print(f"  {m['t_offset_s']:5.1f}s [{m['block']:20}] {m['kind']:9} {m['label']}{ny}")
    print(f"\noutcome: {json.dumps(r['outcome'])}")
    print("\nbenchmark (ground_truth_met=%s):" % card["ground_truth_met"])
    for a in card["arms"]:
        print(f"  {a['arm']:20} reached={a['outcome_reached']!s:5} t_outcome={a['metrics']['time_to_outcome_s']}s "
              f"guesses={a['metrics']['unsupported_guesses']} :: {a['notes']}")


if __name__ == "__main__":
    _print(sys.argv[1] if len(sys.argv) > 1 else "after-hours-lead")

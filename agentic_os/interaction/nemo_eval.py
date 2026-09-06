"""Export NVIDIA NeMo Voice Agent's 328 eval scenarios into the harness's scenario JSONL.

The `labs-Voice-Agent` repo ships 328 customer-service scenarios (eva_airline 50, tau2_airline 50,
tau2_retail 114, tau2_telecom 114) under ``nemo_voice_agent/evaluation/data/``. This reads them and
emits the flat ``{scenario_id, domain, turns[]}`` shape our harness loads — so the harness runs on the
REAL corpus, not just the built-in fixtures.

Honesty: these scenarios do NOT carry a closed-vocabulary objective label in our vocabulary — the real
NeMo evaluation is a two-bot, LLM-judged run against a live agent (needs the deployed runtime). So the
export leaves ``expected_objective`` empty and the harness scores them **reference-free** (resolver
coverage: does it confidently classify a real utterance, or abstain?), never a fabricated accuracy. The
dataset itself is MIT-licensed (ServiceNow/eva + sierra-research/tau2-bench) and is NOT vendored here —
this exporter regenerates the JSONL from a local checkout.
"""
from __future__ import annotations

import glob
import json
import os
from typing import Any, Dict, List


def _one_line(s: Any) -> str:
    return " ".join(str(s or "").split())


def _eva_airline(data_dir: str) -> List[Dict[str, Any]]:
    path = os.path.join(data_dir, "eva_airline", "eva_airline_dataset.jsonl")
    out: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return out
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        goal = d.get("user_goal")
        # user_goal is sometimes a JSON-ish string; take the high-level goal text out of it
        text = goal if isinstance(goal, str) else json.dumps(goal)
        out.append({"scenario_id": f"eva_airline:{d.get('id')}", "domain": "airline",
                    "turns": [_one_line(text)[:600]], "source": "eva_airline"})
    return out


def _tau2(data_dir: str, domain_dir: str, domain: str) -> List[Dict[str, Any]]:
    path = os.path.join(data_dir, domain_dir, "tasks.json")
    out: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return out
    try:
        tasks = json.load(open(path))
    except ValueError:
        return out
    for t in tasks:
        ins = (t.get("user_scenario") or {}).get("instructions") or {}
        reason = ins.get("reason_for_call") or ""
        out.append({"scenario_id": f"{domain_dir}:{t.get('id')}",
                    "domain": ins.get("domain") or domain,
                    "turns": [_one_line(reason)[:600]], "source": domain_dir})
    return out


def export_nemo_scenarios(data_dir: str) -> List[Dict[str, Any]]:
    """Read a labs-Voice-Agent ``evaluation/data`` dir → the harness's scenario dicts (all 328)."""
    rows: List[Dict[str, Any]] = []
    rows += _eva_airline(data_dir)
    rows += _tau2(data_dir, "tau2_airline", "airline")
    rows += _tau2(data_dir, "tau2_retail", "retail")
    rows += _tau2(data_dir, "tau2_telecom", "telecom")
    return rows


def write_jsonl(rows: List[Dict[str, Any]], out_path: str) -> int:
    with open(out_path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return len(rows)


def main() -> None:  # pragma: no cover - CLI
    import argparse
    ap = argparse.ArgumentParser(description="Export NeMo Voice Agent eval scenarios to harness JSONL.")
    ap.add_argument("data_dir", help="labs-Voice-Agent/nemo_voice_agent/evaluation/data")
    ap.add_argument("out", help="output .jsonl path")
    args = ap.parse_args()
    n = write_jsonl(export_nemo_scenarios(args.data_dir), args.out)
    by: Dict[str, int] = {}
    for r in export_nemo_scenarios(args.data_dir):
        by[r["domain"]] = by.get(r["domain"], 0) + 1
    print(f"wrote {n} scenarios to {args.out} — " + ", ".join(f"{d}:{c}" for d, c in sorted(by.items())))


if __name__ == "__main__":  # pragma: no cover
    main()

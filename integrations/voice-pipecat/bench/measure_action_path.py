"""Measure the governance overhead of the ACTION path, separately from any speech path.

What is timed: ``VoiceActionBridge.submit`` (validation, session and identity checks, journal, the Agent Gateway pipeline
with a real authority guard, the in-process mock CRM) minus the time the mock CRM itself spent. The speech path (audio,
speech-to-text, the model, text-to-speech, the network) is NOT part of this and is not measured anywhere in this repository.

Run: ``python bench/measure_action_path.py [--n 300] [--json]``. The numbers depend on the machine and on fsync; they are a
regression guard and a description of this code's own cost, not a claim about any deployment or product.
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import time
from typing import Dict, List

from dataclasses import replace

from agentic_os_voice.capabilities import default_envelope, default_grants
from agentic_os_voice.contracts import ActionRequest, ConfirmationState, IdentityLevel, SessionBinding
from agentic_os_voice.demo.run import SUBJECT, build_env


def _pct(xs: List[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100.0 * (len(xs) - 1))))]


def _stats(ms: List[float]) -> Dict[str, float]:
    return {"n": len(ms), "p50_ms": round(_pct(ms, 50), 3), "p95_ms": round(_pct(ms, 95), 3), "p99_ms": round(_pct(ms, 99), 3),
            "max_ms": round(max(ms), 3), "mean_ms": round(statistics.fmean(ms), 3)}


def measure(n: int = 200, durable: bool = True) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    with tempfile.TemporaryDirectory(prefix="voice-bench-") as d:
        # The owner's default limits (a rate limit, a call cap) are part of the product and would turn most writes of a tight loop into
        # 'pending approval'; the bench lifts them so that it times the path that executes, and checks that it did.
        grants = tuple(replace(g, max_calls=None, rate_calls=None) for g in default_grants())
        env = build_env(d, durable=durable, install_envelope=False)
        env.envelopes.put(default_envelope(env.tenant, subject=SUBJECT, now=env.clock(), grants=grants, max_actions=10_000_000))
        env.bridge.open_session(SessionBinding("bench-1", env.tenant, env.clock() + 3600, IdentityLevel.VERIFIED, ("lead_dana01",), "", SUBJECT))
        C = ConfirmationState.CONFIRMED

        def run(kind: str, mk, expect: str) -> None:
            ms: List[float] = []
            for i in range(-min(20, n), n):                      # the first iterations are a warm-up and are not reported
                req = mk(i)
                before = len(env.crm.service_s)
                t0 = time.perf_counter()
                res = env.bridge.submit(req)
                if res.status.value != expect:
                    raise RuntimeError(f"{kind}: expected {expect}, got {res.status.value}/{res.reason}")
                total = (time.perf_counter() - t0) * 1000.0
                crm = sum(env.crm.service_s[before:]) * 1000.0
                if i >= 0:
                    ms.append(max(0.0, total - crm))
            out[kind] = _stats(ms)

        run("read (lookup)", lambda i: ActionRequest("bench-1", env.tenant, "lookup_lead", {"lead_id": "lead_dana01"}), "completed")
        run("write (new intent)", lambda i: ActionRequest("bench-1", env.tenant, "update_lead", {
            "lead_id": "lead_dana01", "status": "qualified", "budget_band": "10k_50k", "timeline": "now", "need_summary": f"n{i + 1000}"}, "", (), C), "completed")
        run("replay (completed write asked again)", lambda i: ActionRequest("bench-1", env.tenant, "update_lead", {
            "lead_id": "lead_dana01", "status": "qualified", "budget_band": "10k_50k", "timeline": "now", "need_summary": "n0"}, "", (), C), "completed")
        run("refusal (not allowlisted)", lambda i: ActionRequest("bench-1", env.tenant, "export_all", {}), "denied")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res = {"in_memory": measure(a.n, durable=False), "durable": measure(a.n, durable=True)}
    meta = {"python": sys.version.split()[0], "platform": platform.platform(), "machine": platform.machine(), "n_per_case": a.n}
    if a.json:
        print(json.dumps({"meta": meta, "results": res}, indent=2))
    else:
        print(meta)
        for cfg, rows in res.items():
            print(f"[{cfg}]")
            for k, v in rows.items():
                print(f"  {k:<40} p50={v['p50_ms']:>8.3f}  p95={v['p95_ms']:>8.3f}  p99={v['p99_ms']:>8.3f}  max={v['max_ms']:>8.3f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())

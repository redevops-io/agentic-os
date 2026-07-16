"""Placement learning — the measured-outcome reward loop for the `sky` operator.

SkyPilot's optimizer ranks candidates by sticker price. This ledger records what each placement
*actually* delivered — real cost, whether the region had capacity, spot-preemption rate, time-to-
ready — as an EMA reward keyed by (workload shape × candidate), so ``sky.optimize`` can **re-rank**
SkyPilot's candidates by what wins for *this* kind of workload. It is the deployment analog of the
v4 measured-cost loop: the runtime drifts toward the config that delivers, not the cheapest guess.
JSON-backed (CR_SKY_LEDGER) so the learning survives restarts.
"""
from __future__ import annotations

import json
import os


def workload_key(spec: dict) -> str:
    """Key the reward on the workload *shape* (resource profile), not the app name."""
    gpus = str(spec.get("gpus") or "cpu")
    spot = "spot" if spec.get("spot") else "on-demand"
    return f"{gpus}|{spot}"


def candidate_key(cand: dict) -> str:
    return f"{cand.get('cloud', '?')}:{cand.get('region', '?')}:{cand.get('instance', '?')}"


def reward_from_outcome(outcome: dict) -> float:
    """Map a measured launch outcome → reward in [0,1]: available + stable + fast scores high; a
    failed launch is 0, capacity miss (needed failover) halves it, spot churn and slow ttr discount it."""
    if not outcome.get("launched", True):
        return 0.0
    r = 1.0
    r *= max(0.0, 1.0 - float(outcome.get("preemption_rate", 0.0) or 0.0))
    if not outcome.get("had_capacity", True):
        r *= 0.5
    if float(outcome.get("time_to_ready_s", 0) or 0) > 600:
        r *= 0.8
    return round(max(0.0, min(1.0, r)), 4)


class PlacementLedger:
    """EMA reward per (workload shape, placement candidate). ``value`` reads the learned reward,
    ``record`` folds a measured outcome in, ``rerank`` reorders SkyPilot's candidates by it."""

    def __init__(self, path: "str | None" = None, beta: float = 0.3):
        self.path = path if path is not None else os.getenv("CR_SKY_LEDGER", "")
        self.beta = beta
        self._v: dict = {}  # workload_key → candidate_key → [ema, n]
        if self.path and os.path.isfile(self.path):
            try:
                self._v = json.load(open(self.path))
            except Exception:
                self._v = {}

    def _save(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            json.dump(self._v, open(self.path, "w"))
        except Exception:
            pass

    def value(self, spec: dict, cand: dict) -> "float | None":
        e = self._v.get(workload_key(spec), {}).get(candidate_key(cand))
        return e[0] if e else None

    def record(self, spec: dict, cand: dict, outcome: dict) -> float:
        reward = reward_from_outcome(outcome)
        wk, ck = workload_key(spec), candidate_key(cand)
        cur = self._v.setdefault(wk, {}).get(ck)
        if cur:
            self._v[wk][ck] = [round((1 - self.beta) * cur[0] + self.beta * reward, 4), cur[1] + 1]
        else:
            self._v[wk][ck] = [reward, 1]
        self._save()
        return reward

    def rerank(self, spec: dict, candidates: list) -> list:
        """Annotate candidates with the learned reward and reorder — learned-good first, then
        cheapest. Unseen candidates keep a neutral 0.5 prior so a good-but-untried option is still
        explored, and the top after re-rank becomes ``chosen``."""
        out = [dict(c) for c in candidates]
        for c in out:
            c["learned_reward"] = self.value(spec, c)
        out.sort(key=lambda c: (-(0.5 if c["learned_reward"] is None else c["learned_reward"]),
                                float(c.get("hourly_usd", 1e9))))
        for i, c in enumerate(out):
            c["chosen"] = (i == 0)
        return out

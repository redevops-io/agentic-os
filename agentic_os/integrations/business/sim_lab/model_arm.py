"""Real frozen-model arm for the Fraud world — the second-family same-model gate.

The Receivables track showed the same frozen model makes far lower-regret follow-up decisions when given
the Runtime's retrieved verified Experience. The Simulation Lab's job is to test whether that mechanism
holds on a *different* decision family. This runs the SAME frozen model (same OpenAI-compatible Qwen
endpoint and identity as ``..receivables_model_arm``) on fraud-review cases, with and without a compact
summary of what actually worked per observable bucket.

LIVE, not a golden: the model is non-deterministic, so results are captured and reported, not asserted
tight. The frozen-model identity is (base_url, model, temperature); only behavioral signals are ever put in
the prompt — no protected traits (mirrors :data:`fraud_world.PROTECTED_TRAITS_NEVER_USED`).
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

from ..receivables_model_arm import DEFAULT_BASE_URL, DEFAULT_MODEL, endpoint_reachable
from .fraud_world import (
    APPROVE, DECLINE, HOLD, MANUAL_REVIEW, REQUEST_VERIFICATION, FraudWorld)
from .harness import ArmReport, DecisionCase, Proposal, evaluate

_ACTIONS = (APPROVE, HOLD, REQUEST_VERIFICATION, MANUAL_REVIEW, DECLINE)
_MENU = (
    "approve = fulfil the order now\n"
    "hold = let it sit / delay without contacting the customer\n"
    "request_verification = ask the customer to step up (3-D Secure / re-auth)\n"
    "manual_review = route to a human analyst (only for high-value or address-mismatch orders)\n"
    "decline = refuse the order")
_SYSTEM = (
    "You are an order-fraud analyst. For one order, choose the single best action. Declining a genuine "
    "customer is a real, costly mistake, and so is approving fraud — the middle actions exist for ambiguous "
    "orders. Use ONLY the behavioral signals given; never infer from identity, geography, or demographics. "
    "Reply with ONLY one action token from the menu on the last line.")


@dataclass
class FrozenChatModel:
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    temperature: float = 0.0

    def choose(self, user_prompt: str, *, timeout: float = 30.0) -> str:
        import httpx  # noqa: PLC0415
        r = httpx.post(self.base_url.rstrip("/") + "/chat/completions", timeout=timeout, json={
            "model": self.model, "temperature": self.temperature, "max_tokens": 200,
            "messages": [{"role": "system", "content": _SYSTEM},
                         {"role": "user", "content": user_prompt}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def _describe(observable: Mapping[str, object]) -> str:
    flags = [k.replace("_flag", "") for k in (
        "velocity_flag", "mismatch_flag", "new_account_flag", "high_value_flag", "reship_flag")
        if observable.get(k)]
    return (f"order value band: {observable.get('amount_band')}; "
            f"risk signals present: {', '.join(flags) if flags else 'none'}")


def _prompt(observable: Mapping[str, object], *, experience: str = "") -> str:
    exp = f"\nWhat has actually worked on similar orders:\n{experience}\n" if experience else ""
    return f"Order under review:\n  {_describe(observable)}\n{exp}\nActions:\n{_MENU}\n"


def _parse(raw: str) -> str:
    low = raw.lower()
    for a in (REQUEST_VERIFICATION, MANUAL_REVIEW, APPROVE, DECLINE, HOLD):  # multiword first
        if re.search(rf"\b{re.escape(a)}\b", low):
            return a
    return REQUEST_VERIFICATION            # unparseable → the conservative-but-non-harmful middle


def build_experience(world: FraudWorld, train: Sequence[DecisionCase], *, k: int = 40) -> str:
    """A compact, human-readable summary of the best VERIFIED action per observable bucket — the exact
    Experience the learned arm uses, rendered for the model prompt. Reads only buckets + oracle outcomes on
    training cases (never latent per eval case)."""
    tally: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for c in train:
        b = world.bucket(c.observable)
        for a in world.actions():
            if world.admissible_action(c.observable, a):
                tally[b][a].append(world.net_value(c.latent, a))
    lines = []
    for b, per in sorted(tally.items(), key=lambda kv: -len(next(iter(kv[1].values()))))[:k]:
        best = max(per, key=lambda a: statistics.mean(per[a]))
        lines.append(f"- band+signals {b}: best outcome from '{best}'")
    return "\n".join(lines)


def run_model_experiment(*, n: int = 24, seed: int = 3, timeout: float = 30.0
                         ) -> dict[str, ArmReport]:
    """Run the frozen model on fraud cases with vs without verified Experience; return both ArmReports.

    Uses a small n by default because it makes one live call per case per arm.
    """
    world = FraudWorld()
    train = world.cases(seed=seed, n=400)
    eval_cases = world.cases(seed=seed + 10_000, n=n)
    model = FrozenChatModel()
    exp = build_experience(world, train)

    def make_arm(experience: str) -> Callable[[DecisionCase], Proposal]:
        def arm(c: DecisionCase) -> Proposal:
            raw = model.choose(_prompt(c.observable, experience=experience), timeout=timeout)
            return Proposal(_parse(raw), rationale="frozen model")
        return arm

    return {
        "model_no_experience": evaluate(world, make_arm(""), eval_cases, arm_name="model_no_experience"),
        "model_with_experience": evaluate(
            world, make_arm(exp), eval_cases, arm_name="model_with_experience"),
    }

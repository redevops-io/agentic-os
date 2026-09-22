"""Live frozen-model arms for Stale-Quote — the S0–S3 experiment the reframe specified.

Same frozen model (same Qwen endpoint/identity as the receivables and fraud gates), four conditions that
differ ONLY in what is placed in the prompt:

  S0  no experience.
  S1  + Stale-Quote-native verified experience (best action per observable bucket, from training).
  S2  + the Receivables-derived TRANSFERABLE lesson, stated *semantically* (a principle about readiness /
        elapsed time / prior touches / cost of an unnecessary intervention) — NOT literal thresholds, and
        NOT any Stale-Quote outcome.
  S3  + both.

Preregistered reading (identical to ``transfer``): S1>S0 ⇒ local model-policy gap; S2>S0 ⇒ zero-shot
lesson transfer; S3>S1 ⇒ transferred prior is a useful initialisation; S2<S0 ⇒ negative transfer.

LIVE and non-deterministic: results are captured and reported, not asserted tight.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from ..receivables_model_arm import DEFAULT_BASE_URL, DEFAULT_MODEL, endpoint_reachable
from .harness import ArmReport, DecisionCase, Proposal, evaluate
from .stale_quote_world import (
    CALL, CLOSE_LOST, DIRECT_FOLLOWUP, HOLD, HUMAN_REVIEW, OFFER_ALTERNATIVE, REQUEST_TIMELINE,
    SOFT_FOLLOWUP, StaleQuoteWorld)

_MENU = (
    "hold = do nothing yet\n"
    "soft_followup = a light check-in\n"
    "direct_followup = a firm 'are you ready to proceed?'\n"
    "call = phone the prospect (high effort)\n"
    "request_timeline = ask when they plan to decide\n"
    "offer_alternative = offer a reduced scope / financing (costs margin)\n"
    "human_review = hand to a sales manager (high-value or contested deals only)\n"
    "close_lost = mark the deal dead and free the pipeline")
_SYSTEM = (
    "You are a contractor's sales operations analyst. For one stale quote, choose the single best next "
    "action to maximise the eventual value of the deal — not merely to get a reply. Chasing a prospect who "
    "needs time can lose the deal; doing nothing is often right. Reply with ONLY one action token from the "
    "menu on the last line.")

# The transferable lesson — SEMANTIC, domain-agnostic, no literal thresholds and no sales outcomes.
_TRANSFER_LESSON = (
    "A general principle learned on a different follow-up problem (overdue-invoice collections): the value "
    "of intervening depends on the evidence of readiness, the time elapsed, how many times you have already "
    "reached out, and the cost of an unnecessary contact. When there is clear positive engagement, a "
    "decisive, direct action tends to pay off. When engagement is ambiguous and you have already reached "
    "out several times, another push usually does more harm than good — restraint beats activity. Route "
    "genuinely blocked or contested cases to a person rather than pushing.")


@dataclass
class FrozenChatModel:
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    temperature: float = 0.0

    def choose(self, user_prompt: str, *, timeout: float = 30.0) -> str:
        import httpx  # noqa: PLC0415
        r = httpx.post(self.base_url.rstrip("/") + "/chat/completions", timeout=timeout, json={
            "model": self.model, "temperature": self.temperature, "max_tokens": 220,
            "messages": [{"role": "system", "content": _SYSTEM},
                         {"role": "user", "content": user_prompt}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def _describe(o: Mapping[str, object]) -> str:
    return (f"quote value band: {o.get('quote_value_band')}; days since quote: {o.get('days_since_quote')}; "
            f"prior follow-ups already sent: {o.get('prior_touches')}; "
            f"last signal from prospect: {o.get('last_signal')}; "
            f"competing bid present: {'yes' if o.get('competing_bid_flag') else 'no'}")


def _prompt(o: Mapping[str, object], *, native_exp: str = "", transfer_lesson: str = "") -> str:
    parts = [f"Stale quote under review:\n  {_describe(o)}"]
    if transfer_lesson:
        parts.append(f"\nTransferable principle:\n{transfer_lesson}")
    if native_exp:
        parts.append(f"\nWhat has actually worked on similar stale quotes:\n{native_exp}")
    parts.append(f"\nActions:\n{_MENU}\n")
    return "\n".join(parts)


def _parse(raw: str) -> str:
    low = raw.lower()
    for a in (REQUEST_TIMELINE, OFFER_ALTERNATIVE, HUMAN_REVIEW, CLOSE_LOST, SOFT_FOLLOWUP,
              DIRECT_FOLLOWUP, CALL, HOLD):     # multiword / more-specific first
        if re.search(rf"\b{re.escape(a)}\b", low):
            return a
    return SOFT_FOLLOWUP                          # unparseable → a low-cost, low-harm default


def build_native_experience(world: StaleQuoteWorld, train: Sequence[DecisionCase], *, k: int = 40) -> str:
    tally: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for c in train:
        b = world.bucket(c.observable)
        for a in world.actions():
            if world.admissible_action(c.observable, a):
                tally[b][a].append(world.net_value(c.latent, a))
    lines = []
    for b, per in sorted(tally.items(), key=lambda kv: -len(next(iter(kv[1].values()))))[:k]:
        best = max(per, key=lambda a: statistics.mean(per[a]))
        lines.append(f"- {b}: best outcome from '{best}'")
    return "\n".join(lines)


def run_s0_to_s3(*, n: int = 20, seed: int = 5, timeout: float = 30.0) -> dict[str, ArmReport]:
    world = StaleQuoteWorld()
    train = world.cases(seed=seed, n=400)
    eval_cases = world.cases(seed=seed + 10_000, n=n)
    model = FrozenChatModel()
    native = build_native_experience(world, train)

    def make(native_exp: str, lesson: str) -> Callable[[DecisionCase], Proposal]:
        def arm(c: DecisionCase) -> Proposal:
            raw = model.choose(_prompt(c.observable, native_exp=native_exp, transfer_lesson=lesson),
                               timeout=timeout)
            return Proposal(_parse(raw), rationale="frozen model")
        return arm

    return {
        "S0_no_experience": evaluate(world, make("", ""), eval_cases, arm_name="S0_no_experience"),
        "S1_native": evaluate(world, make(native, ""), eval_cases, arm_name="S1_native"),
        "S2_transfer_lesson": evaluate(
            world, make("", _TRANSFER_LESSON), eval_cases, arm_name="S2_transfer_lesson"),
        "S3_transfer_plus_native": evaluate(
            world, make(native, _TRANSFER_LESSON), eval_cases, arm_name="S3_transfer_plus_native"),
    }

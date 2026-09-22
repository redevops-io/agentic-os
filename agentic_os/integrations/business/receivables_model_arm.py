"""Real-frozen-model arm for the Receivables benchmark — the same-model decision-quality gate.

Phases E–G established the harness + metric offline with policy stand-ins. This closes the gate the
addendum named: run the **same frozen model** on the same cases, with and without the Runtime's retrieved
**verified Experience**, and measure whether experience improves the model's decisions — the business
analogue of the chess result.

This is a LIVE experiment, not a frozen golden: the model is non-deterministic, so results are captured,
not asserted tight. The model is an OpenAI-compatible local endpoint (Qwen), not Anthropic — a minimal
httpx client, no SDK. The frozen-model identity is (base_url, model, temperature).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from .decisions import InterventionKind as K
from .receivables_benchmark import (
    LatentAccount, Signals, _ACTIONS, _bucket, _score, make_corpus, net_value, optimal_action,
    signals_for, ArmReport)

DEFAULT_BASE_URL = os.environ.get("RDO_BENCH_MODEL_BASE_URL", "http://192.168.40.105:8000/v1")
DEFAULT_MODEL = os.environ.get("RDO_BENCH_MODEL", "qwen3.8-27b")

_ACTION_TOKENS = {a.value: a for a in _ACTIONS}
_ACTION_MENU = (
    "hold = do nothing yet\n"
    "soft_reminder = a gentle nudge\n"
    "direct_reminder = a firm reminder\n"
    "payment_plan = offer a structured plan\n"
    "escalation = escalate/collections\n"
    "human_review = route to a person (e.g. a dispute)")


def endpoint_reachable(base_url: str = DEFAULT_BASE_URL, *, timeout: float = 5.0) -> bool:
    try:
        import httpx  # noqa: PLC0415
        return httpx.get(base_url.rstrip("/") + "/models", timeout=timeout).status_code == 200
    except Exception:
        return False


@dataclass
class FrozenModel:
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    temperature: float = 0.0

    def choose(self, prompt: str, *, timeout: float = 30.0) -> str:
        import httpx  # noqa: PLC0415
        r = httpx.post(self.base_url.rstrip("/") + "/chat/completions", timeout=timeout, json={
            "model": self.model, "temperature": self.temperature, "max_tokens": 200,
            "messages": [
                {"role": "system", "content": "You are a receivables operations analyst. Decide the single "
                 "best next action for one overdue account. Do nothing yet (hold) is a valid, often-correct "
                 "choice. Reply with ONLY one action token from the menu on the last line."},
                {"role": "user", "content": prompt}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def _describe(s: Signals) -> str:
    parts = [f"amount outstanding: ${s.amount_cents/100:.0f}", f"{s.days_overdue} days overdue"]
    if s.prior_reminders is not None:
        parts.append(f"{s.prior_reminders} reminders already sent")
        parts.append("customer sent a promise to pay" if s.sent_promise else "no promise to pay")
    if s.has_dispute is not None:
        parts.append("an open dispute exists" if s.has_dispute else "no open dispute")
    return "; ".join(parts)


def _prompt(s: Signals, *, experiences: str = "") -> str:
    ev = f"Account facts: {_describe(s)}."
    exp = f"\n\nVerified outcomes of similar prior accounts (what actually worked):\n{experiences}" if experiences else ""
    return f"{ev}{exp}\n\nChoose one action. Menu:\n{_ACTION_MENU}\n\nAnswer with one token only."


def _parse(raw: str) -> K:
    low = (raw or "").lower()
    # last explicit token wins (models sometimes reason then answer)
    found = [m for m in re.findall(r"[a-z_]+", low) if m in _ACTION_TOKENS]
    return _ACTION_TOKENS[found[-1]] if found else K.DIRECT_REMINDER   # unparseable → the naive default


def build_experience_text(train: Sequence[LatentAccount], *, level: str = "G", k: int = 60) -> Dict[str, str]:
    """The Runtime's retrieved VERIFIED experience, per observable bucket: the action that produced the
    best average net value on the training split (a bounded, strategy-only summary — not a rule)."""
    agg: Dict[str, Dict[K, List[int]]] = {}
    for l in train:
        b = _bucket(signals_for(l, level))
        for a in _ACTIONS:
            agg.setdefault(b, {}).setdefault(a, []).append(net_value(l, a))
    out: Dict[str, str] = {}
    for b, per in agg.items():
        best = max(per, key=lambda a: sum(per[a]) / len(per[a]))
        out[b] = f"- for accounts like this, '{best.value}' produced the best verified outcome on average"
    return out


def run_model_experiment(*, n: int = 32, seed: int = 11, level: str = "D",
                         base_url: Optional[str] = None, model: Optional[str] = None) -> Dict[str, ArmReport]:
    """Evaluate the SAME frozen model with and without retrieved verified Experience on the same test
    accounts. Returns {'model_no_experience': ArmReport, 'model_with_experience': ArmReport}."""
    corpus = make_corpus(seed=seed)
    cut = len(corpus) // 2
    train, test = corpus[:cut], corpus[cut:cut + n]
    fm = FrozenModel(base_url=base_url or DEFAULT_BASE_URL, model=model or DEFAULT_MODEL)
    exp = build_experience_text(train, level="G")

    def act(l: LatentAccount, *, with_exp: bool) -> K:
        s = signals_for(l, level)
        experiences = exp.get(_bucket(signals_for(l, "G")), "") if with_exp else ""
        return _parse(fm.choose(_prompt(s, experiences=experiences)))

    return {
        "model_no_experience": _score("model_no_experience", "frozen model, no experience",
                                      lambda l: act(l, with_exp=False), test),
        "model_with_experience": _score("model_with_experience", "frozen model + verified Experience",
                                        lambda l: act(l, with_exp=True), test),
    }

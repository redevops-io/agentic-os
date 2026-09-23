"""Live frozen-model arms for Supplier Invoice Control — S0 (no experience) and S1 (native experience).

Same frozen model/identity as every other gate. Used by the prospective H test: run S0 to fix the
prediction, then reveal S1. Only reconciliation evidence is placed in the prompt.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from ..receivables_model_arm import DEFAULT_BASE_URL, DEFAULT_MODEL, endpoint_reachable
from .harness import DecisionCase, Proposal
from .supplier_invoice_world import (
    APPROVE, DISPUTE, HOLD, HUMAN_REVIEW, PARTIAL_APPROVE, REQUEST_EVIDENCE, SupplierInvoiceWorld)

_MENU = (
    "approve = pay the invoice in full\n"
    "partial_approve = pay only the matched/contracted portion\n"
    "request_evidence = pay a fee to obtain more evidence, then dispose correctly\n"
    "dispute = formally contest the invoice\n"
    "hold = defer without resolving\n"
    "human_review = escalate to an AP manager (material or evidence-incomplete invoices only)")
_SYSTEM = (
    "You are an accounts-payable invoice-control analyst. For one invoice, choose the single best action. "
    "Paying a correct invoice is the right, low-friction outcome — investigating or disputing a legitimate "
    "charge wastes money and supplier goodwill, and investigation itself costs money. But approving a real "
    "overbill, duplicate, or short-delivery loses money. A documented amendment on file means a variance is "
    "legitimate. Reply with ONLY one action token from the menu on the last line.")


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
    parts = [f"invoice amount band: {o.get('amount_band')}",
             f"price variance vs contract: {'yes' if o.get('price_variance_flag') else 'no'}",
             f"quantity variance vs goods received: {'yes' if o.get('qty_variance_flag') else 'no'}",
             f"possible duplicate of a prior invoice: {'yes' if o.get('duplicate_suspect_flag') else 'no'}",
             f"documented amendment on file: {'yes' if o.get('amendment_on_file_flag') else 'no'}",
             f"discrepancy materiality: {o.get('materiality_band')}",
             f"supplier reliability: {o.get('supplier_reliability')}",
             f"PO/receipt evidence complete: {'yes' if o.get('evidence_complete_flag') else 'no'}"]
    return "; ".join(parts)


def _prompt(o: Mapping[str, object], *, experience: str = "") -> str:
    exp = f"\nWhat has actually reconciled best on similar invoices:\n{experience}\n" if experience else ""
    return f"Invoice under review:\n  {_describe(o)}\n{exp}\nActions:\n{_MENU}\n"


def _parse(raw: str) -> str:
    low = raw.lower()
    for a in (PARTIAL_APPROVE, REQUEST_EVIDENCE, HUMAN_REVIEW, DISPUTE, APPROVE, HOLD):  # multiword first
        if re.search(rf"\b{re.escape(a)}\b", low):
            return a
    return APPROVE                        # unparseable → the frictionless default


def build_native_experience(world: SupplierInvoiceWorld, train: Sequence[DecisionCase], *, k: int = 40) -> str:
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


def s0_arm(model: FrozenChatModel, *, timeout: float = 30.0) -> Callable[[DecisionCase], Proposal]:
    def arm(c: DecisionCase) -> Proposal:
        return Proposal(_parse(model.choose(_prompt(c.observable), timeout=timeout)), rationale="S0")
    return arm


def s1_arm(model: FrozenChatModel, experience: str, *, timeout: float = 30.0
           ) -> Callable[[DecisionCase], Proposal]:
    def arm(c: DecisionCase) -> Proposal:
        return Proposal(_parse(model.choose(_prompt(c.observable, experience=experience), timeout=timeout)),
                        rationale="S1")
    return arm

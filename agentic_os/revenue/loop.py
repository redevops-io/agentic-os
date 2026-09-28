"""Revenue loop — parse a request into quote lines, and execute an approved QuotePlan (plan §14 tail).

The flagship's read→plan half is `flagship.plan_quote_from_request`. This is the other half:

  line_items_from_text : the "Discovery → resolve product/quantity" step — pull requested items + quantities
                         from a free-text message against the catalog (deterministic).
  execute_quote_plan   : the "Execution → create quote → update opportunity → schedule follow-up" step —
                         run the write-back through an injected `QuoteExecutor`, but ONLY for a plan that is
                         ready AND (when consequential) approved. The approval gate is enforced here, not
                         trusted to the caller.

The `QuoteExecutor` is a Protocol so the real ERPNext/Twenty write clients bind as a thin adapter in the
enterprise revenue app, while tests use an in-memory executor. Pure orchestration; no I/O of its own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Mapping, Optional, Protocol, Tuple

from agentic_os.revenue.flagship import QuotePlan
from agentic_os.revenue.quote import CatalogItem, QuoteLine

_NUM = re.compile(r"\b(\d{1,7})\b")
_WORD = re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> List[str]:
    return _WORD.findall((s or "").lower())


def line_items_from_text(text: str, catalog: Mapping[str, CatalogItem]) -> List[QuoteLine]:
    """Extract requested (item, qty) lines from a message against the catalog. An item is matched by its ref
    or a name token appearing in the text; its quantity is the nearest number to that mention (default 1)."""
    t = (text or "").lower()
    nums: List[Tuple[int, int]] = [(m.start(), int(m.group(1))) for m in _NUM.finditer(t)]
    lines: List[QuoteLine] = []
    for ref, item in catalog.items():
        needles = {ref.lower(), *[w for w in _tokens(item.name) if len(w) > 2]}
        pos = min((t.find(n) for n in needles if n and t.find(n) >= 0), default=-1)
        if pos < 0:
            continue
        # a quantity usually precedes its item ("300 units of Widget"), so prefer the nearest number to the
        # LEFT of the mention; fall back to the nearest number overall, else 1.
        qty = 1
        before = [n for n in nums if n[0] < pos]
        if before:
            qty = before[-1][1]
        elif nums:
            qty = min(nums, key=lambda np: abs(np[0] - pos))[1]
        lines.append(QuoteLine(ref, float(qty)))
    return lines


class QuoteExecutor(Protocol):
    """The write side — implemented by the real ERPNext/Twenty clients (or an in-memory test double)."""
    def create_quotation(self, entity_ref: str, plan: QuotePlan) -> str: ...
    def update_opportunity(self, entity_ref: str, note: str) -> None: ...
    def schedule_followup(self, entity_ref: str, note: str) -> None: ...


@dataclass(frozen=True)
class ExecutionReceipt:
    executed: bool
    reason: str
    entity_ref: str = ""
    quote_id: str = ""
    opportunity_updated: bool = False
    followup_scheduled: bool = False
    steps: Tuple[str, ...] = field(default_factory=tuple)


def execute_quote_plan(plan: QuotePlan, *, executor: QuoteExecutor, approved: bool = False) -> ExecutionReceipt:
    """Execute a QuotePlan's write-back — but refuse unless it is ready, and (when it requires approval) has
    been approved. Creates the quotation, updates the opportunity, and schedules a follow-up (§14 tail)."""
    entity_ref = plan.entity.entity_ref if plan.entity is not None else ""
    if not plan.ready:
        return ExecutionReceipt(False, f"not executed — plan not ready ({plan.reason})", entity_ref)
    if plan.approval_required and not approved:
        return ExecutionReceipt(False, "not executed — parked on approval", entity_ref)

    steps: List[str] = []
    quote_id = executor.create_quotation(entity_ref, plan)
    steps.append(f"created quotation {quote_id}")
    executor.update_opportunity(entity_ref, note="quote issued from resolved commercial request")
    steps.append("updated opportunity")
    executor.schedule_followup(entity_ref, note="follow up on issued quote")
    steps.append("scheduled follow-up")
    return ExecutionReceipt(True, "quote issued, opportunity updated, follow-up scheduled", entity_ref,
                            quote_id=quote_id, opportunity_updated=True, followup_scheduled=True,
                            steps=tuple(steps))


@dataclass
class InMemoryExecutor:
    """A test/demo executor that records the write-backs instead of hitting ERPNext/Twenty."""
    quotations: List[Tuple[str, dict]] = field(default_factory=list)
    opportunity_notes: List[Tuple[str, str]] = field(default_factory=list)
    followups: List[Tuple[str, str]] = field(default_factory=list)
    _seq: int = 0

    def create_quotation(self, entity_ref: str, plan: QuotePlan) -> str:
        self._seq += 1
        qid = f"QTN-{self._seq:04d}"
        self.quotations.append((qid, plan.as_dict().get("draft_quote_plan", {})))
        return qid

    def update_opportunity(self, entity_ref: str, note: str) -> None:
        self.opportunity_notes.append((entity_ref, note))

    def schedule_followup(self, entity_ref: str, note: str) -> None:
        self.followups.append((entity_ref, note))

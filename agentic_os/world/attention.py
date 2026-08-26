"""Founder Attention Queue — the Business-OS home surface ("what needs my attention").

The product plan makes Home not a list of runtime names but the founder's decision list: approvals to give,
missions blocked and waiting, dollars at risk, evidence still too thin to act on. This module is a *read* over
the world-runtime, not a new engine. It runs the governed worlds and harvests every point the loop routed to a
human — the NeedsYou approvals, the policy blocks, the money held pending a decision — into one prioritized
queue. Every item carries why (the evidence-grounded reason the Runtime raised it), which business block it
belongs to, the dollars at stake, a realism label, and where it sits on the Observe → Recommend → Approve →
Autonomous ladder.

The home never shows a bare "run X" — it shows "approve the $1,900 sponsorship (spend-gated, $22.5k expected
value)", "unblock the KYC mission (sanctions hit)", "3 leads need more evidence before outreach". One queue
across every business system, under one identity/evidence/policy model.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .models import WorldRun
from .orchestrator import ScenarioOrchestrator
from .trace_blocks import CS, FIN, REV, RUN, SEC

# Each world belongs to a business system, so the home groups by Revenue / Finance / Security etc. — not by
# runtime internals (the plan's "Home is what needs my attention, not runtime names").
_WORLD_BLOCK = {"sponsorship-booking": REV, "creator-sponsorship": REV, "gtm-pilot-discovery": REV,
                "after-hours-lead": REV, "paid-acquisition": REV, "finance-leakage": FIN, "kyc-ownership": SEC, "contact-center": CS}


class AttentionKind:
    APPROVAL = "APPROVAL"            # a founder decision gating an action (spend / publish / send)
    BLOCKED = "BLOCKED"             # a mission safely stopped and cannot proceed without help
    AT_RISK = "AT_RISK"            # money exposed / held pending a decision
    NEEDS_EVIDENCE = "NEEDS_EVIDENCE"  # the Runtime refused to act because evidence is too thin
    REVIEW = "REVIEW"             # a lower-stakes look, no money gate


class Autonomy:
    OBSERVE = "OBSERVE"
    RECOMMEND = "RECOMMEND"
    APPROVE = "APPROVE"
    AUTONOMOUS = "AUTONOMOUS"


_KIND_RANK = {AttentionKind.APPROVAL: 0, AttentionKind.BLOCKED: 1, AttentionKind.AT_RISK: 2,
              AttentionKind.NEEDS_EVIDENCE: 3, AttentionKind.REVIEW: 4}

# Answers that let each governed mission run to completion (so dollar impact is realized); the NeedsYou
# milestones the loop raised on the way are still recorded in the trace and become the founder's decisions.
_AUTOPLAY_ANSWERS = {"What is the roof pitch?": "6/12", "Approve invoice correction?": "yes",
                     "Approve outreach to acme-ai-platform?": "yes", "Approve sponsorship portfolio?": "yes",
                     "POLICY_APPROVAL": "approve within ceiling"}


@dataclass(frozen=True)
class AttentionItem:
    item_id: str
    kind: str
    title: str
    why: str
    world_id: str
    business_block: str
    dollar_impact: float
    realism: str
    autonomy: str
    suggested_action: str

    def priority(self) -> "tuple[int, float]":
        """Founders triage by category, then by money. Approvals first (the founder's core job), then blocks,
        then money at risk; within a category, biggest dollar impact first."""
        return (_KIND_RANK.get(self.kind, 9), -self.dollar_impact)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def _classify(needs_you: str) -> str:
    r = (needs_you or "").upper()
    if "APPROV" in r or "POLICY" in r or "PUBLISH" in r:
        return AttentionKind.APPROVAL
    if "EVIDENCE" in r:
        return AttentionKind.NEEDS_EVIDENCE
    return AttentionKind.REVIEW


def _suggest(kind: str) -> str:
    return {AttentionKind.APPROVAL: "review the evidence and approve or decline",
            AttentionKind.BLOCKED: "unblock (restore the capability) or waive",
            AttentionKind.AT_RISK: "confirm the recovery / release the held action",
            AttentionKind.NEEDS_EVIDENCE: "gather more evidence or drop the lead",
            AttentionKind.REVIEW: "take a look"}.get(kind, "review")


def items_from_run(run: WorldRun, world_id: str) -> List[AttentionItem]:
    """Extract the founder-facing decisions from one parked/held world run."""
    items: List[AttentionItem] = []
    dollar = float(getattr(run.metrics, "revenue_value", 0.0) or 0.0)
    block = _WORLD_BLOCK.get(world_id, RUN)
    milestones = getattr(run.trace, "milestones", []) or []
    for i, m in enumerate(milestones):
        ny = getattr(m, "needs_you", "")
        if ny:
            kind = _classify(ny)
            items.append(AttentionItem(
                item_id=f"{world_id}:{i}", kind=kind, title=getattr(m, "label", "decision required"),
                why=f"{ny} — raised by the governed {world_id} mission",
                world_id=world_id, business_block=block, dollar_impact=dollar,
                realism=getattr(m, "realism", "") or "", suggested_action=_suggest(kind),
                autonomy=Autonomy.APPROVE if kind == AttentionKind.APPROVAL else Autonomy.RECOMMEND))
    if not run.ok and run.safe_stop_reason:
        items.append(AttentionItem(
            item_id=f"{world_id}:stop", kind=AttentionKind.BLOCKED, title="mission safely stopped",
            why=run.safe_stop_reason, world_id=world_id, business_block=block, dollar_impact=dollar,
            realism="", suggested_action=_suggest(AttentionKind.BLOCKED), autonomy=Autonomy.RECOMMEND))
    # money held pending a decision, with no explicit NeedsYou milestone (e.g. a drafted-but-unsent action)
    o = run.outcome or {}
    held = (o.get("booked") is False and o.get("approved") is False) or o.get("sent") is False
    if not items and held and dollar > 0:
        items.append(AttentionItem(
            item_id=f"{world_id}:atrisk", kind=AttentionKind.AT_RISK,
            title=f"${dollar:,.0f} of value held pending a decision", why="a governed action is drafted but held",
            world_id=world_id, business_block=block, dollar_impact=dollar, realism=o.get("mode", ""),
            suggested_action=_suggest(AttentionKind.AT_RISK), autonomy=Autonomy.RECOMMEND))
    return items


def build_attention_queue(worlds: Dict[str, Any], *, orchestrator: Optional[ScenarioOrchestrator] = None,
                          authority: Any = None, seeds: Optional[Dict[str, str]] = None,
                          answers: Optional[Dict[str, str]] = None,
                          offline: bool = True) -> List[AttentionItem]:
    """Run every world to completion and return one prioritized attention queue of the founder's decisions."""
    orch = orchestrator or ScenarioOrchestrator()
    seeds = seeds or {}
    ans = {**_AUTOPLAY_ANSWERS, **(answers or {})}   # complete each mission; harvest the decisions it raised
    items: List[AttentionItem] = []
    for wid, world in worlds.items():
        try:
            run = orch.run(world, seed=seeds.get(wid, "seed-0"), authority=authority, answers=ans,
                           offline=offline)
        except Exception:  # noqa: BLE001 — a world that errors becomes a BLOCKED item, never a crashed home
            items.append(AttentionItem(item_id=f"{wid}:error", kind=AttentionKind.BLOCKED,
                                       title="mission failed to run", why="the mission raised an error",
                                       world_id=wid, business_block=RUN, dollar_impact=0.0, realism="",
                                       suggested_action="inspect the mission", autonomy=Autonomy.RECOMMEND))
            continue
        items.extend(items_from_run(run, wid))
    items.sort(key=lambda it: it.priority())
    return items


def summarize(items: List[AttentionItem]) -> Dict[str, Any]:
    """The top-of-home counters: how many of each kind, and the total dollars awaiting a decision."""
    by_kind: Dict[str, int] = {}
    by_block: Dict[str, int] = {}
    for it in items:
        by_kind[it.kind] = by_kind.get(it.kind, 0) + 1
        by_block[it.business_block] = by_block.get(it.business_block, 0) + 1
    return {"total": len(items), "by_kind": by_kind, "by_block": by_block,
            "dollars_awaiting_decision": round(sum(it.dollar_impact for it in items
                                                   if it.kind in (AttentionKind.APPROVAL,
                                                                  AttentionKind.AT_RISK)), 2)}

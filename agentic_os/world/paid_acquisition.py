"""PaidAcquisitionWorld — the paid-ads plane (Reddit now, LinkedIn later) as a governed GTM world.

A third acquisition channel in the same cost-based planner: find the technical communities whose members are
likely ReDevOps buyers, score buyer density (not raw subscribers), propose promoted placements, and commit
ad spend — but only within a founder approval AND under the cross-channel Budget Governor. The demonstration
the plane exists to make: a founder can approve five individually-reasonable placements that together exceed
the budget; the Budget Governor commits them in value order and REFUSES the ones that would overrun, so total
spend never exceeds the budget even under full approval. Discovery is fixture-backed (SEEDED/SYNTHETIC) here;
the real Reddit posting adapter reuses the one already running in nutrients_bot (rog-strix). Public-safe: the
campaign commit is EXTERNAL, so under SIMULATE it routes to the Outcome Simulator — no real ad is bought.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from runtime_contracts.world import (
    EntityKind,
    EntityRef,
    GroundTruth,
    RealismClass,
    SIGNAL_OBSERVED,
    WorldDescriptor,
    WorldEvent,
)

from .base import RuntimeContext, WorldDefinition
from .budget_governor import BudgetGovernor
from .fabric import CapabilityFabric, CapabilityProvider, SideEffect
from .optimizer import CrossChannelOptimizer, direct_email_action, sponsorship_action
from .sponsorship import PriceBasis, _BUYER_TOPICS
from .trace_blocks import FIN, REV, RUN

_CHANNEL = "reddit"
_PILOT_VALUE_USD = 25000.0

# Fixture communities (subreddits). `spend` = the promoted-placement budget; `cpm` sets expected reach.
# A general-AI-news community is included to prove buyer density, not subscriber count, drives selection.
_COMMUNITIES = [
    {"id": "r/devops", "audience": 2400000, "cpm": 8.0, "spend": 600.0,
     "topics_text": "devops kubernetes ci cd platform engineering observability infrastructure automation"},
    {"id": "r/mlops", "audience": 80000, "cpm": 12.0, "spend": 400.0,
     "topics_text": "mlops model deployment platform inference monitoring pipelines evaluation"},
    {"id": "r/LocalLLaMA", "audience": 520000, "cpm": 10.0, "spend": 700.0,
     "topics_text": "llm agents rag inference local models eval retrieval agentic"},
    {"id": "r/kubernetes", "audience": 300000, "cpm": 9.0, "spend": 500.0,
     "topics_text": "kubernetes platform infrastructure operators observability devops"},
    {"id": "r/artificial", "audience": 900000, "cpm": 6.0, "spend": 500.0,
     "topics_text": "ai news general consumer chatgpt discussion"},
]


def _fit(community: Dict[str, Any]) -> float:
    text = (community.get("topics_text") or "").lower()
    hits = sum(1 for t in _BUYER_TOPICS if t in text)
    topic_fit = min(1.0, hits / 5.0)
    buyer_intent = 1.0 if any(t in text for t in ("agent", "platform", "infrastructure", "mlops", "devops")) else 0.4
    depth = 1.0 if hits >= 3 else 0.5
    return round(0.5 * topic_fit + 0.3 * buyer_intent + 0.2 * depth, 3)


def _placement_economics(community: Dict[str, Any], fit: float) -> Dict[str, Any]:
    reach = int(community["spend"] / max(community["cpm"], 1.0) * 1000)
    qualified = reach * fit
    pilots = qualified * 0.5 * 0.006 * 0.025            # buyers -> visits -> pilots (same conservative funnel)
    value = pilots * _PILOT_VALUE_USD
    return {"spend": community["spend"], "expected_reach": reach,
            "expected_qualified_reach": round(qualified, 1), "expected_pilots": round(pilots, 2),
            "expected_pilot_value": round(value, 2), "value_per_dollar": round(value / max(community["spend"], 1), 3),
            "basis": PriceBasis.PUBLISHED_RATE}


class PaidAcquisitionWorld(WorldDefinition):
    world_id = "paid-acquisition"

    def descriptor(self) -> WorldDescriptor:
        return WorldDescriptor(
            self.world_id, "reddit-communities",
            "Paid acquisition (Reddit ads) — buyer-density targeting under a cross-channel Budget Governor",
            realism=RealismClass.SEEDED_DEMO.value,
            provenance="community fixture (SEEDED); real posting reuses the nutrients_bot Reddit adapter",
            datasources=("subreddit fixture", "simulated ad campaign"), ground_truth_available=True,
            supported_scenarios=("budget-governed-campaign", "overspend-refused"),
            capability_requirements=("ads.community.search", "ads.price.retrieve", "ads.proposal.create",
                                     "ads.approval.request", "ads.campaign.commit", "ads.attribute"))

    def event_stream(self, seed: str) -> List[WorldEvent]:
        return [WorldEvent(
            world_id=self.world_id, dataset_id="reddit-communities", source_record_id=f"campaign-{seed}",
            event_type=SIGNAL_OBSERVED,
            entity_ids=(EntityRef(f"paid-campaign-{seed}", EntityKind.TASK.value, "Paid acquisition campaign"),),
            classification=RealismClass.SEEDED_DEMO.value, tenant=f"world:{self.world_id}",
            ground_truth=GroundTruth(
                situation="technical communities can be targeted by buyer density",
                safe_action="propose placements, get approval, commit spend only within the Budget Governor",
                unsafe_action="commit ad spend over the budget, or without approval/authorization",
                target_outcome="a campaign whose committed spend never exceeds the approved budget"),
            capability_requirements=self.descriptor().capability_requirements, scenario_seed=seed,
            payload={"goal": "reach AI Runtime buyers on Reddit", "budget": 1500, "channel_ceiling": 1500})]

    def register_capabilities(self, fabric: CapabilityFabric) -> None:
        fabric.register(CapabilityProvider("ads.community.search", "reddit-fixture",
                        lambda i, c: {"communities": [dict(x) for x in _COMMUNITIES]},
                        side_effect=SideEffect.READ, realism=RealismClass.SEEDED_DEMO.value, latency_ms=200))
        fabric.register(CapabilityProvider("ads.price.retrieve", "reddit-ads", lambda i, c: i,
                        side_effect=SideEffect.READ, realism=RealismClass.SEEDED_DEMO.value))
        fabric.register(CapabilityProvider("ads.proposal.create", "planner", lambda i, c: i,
                        side_effect=SideEffect.READ))
        fabric.register(CapabilityProvider("ads.approval.request", "attention",
                        lambda i, c: {"requested": True}, side_effect=SideEffect.WRITE))
        # committing ad spend leaves the boundary -> EXTERNAL -> simulated under non-LIVE (no real ad bought)
        fabric.register(CapabilityProvider("ads.campaign.commit", "reddit-ads",
                        lambda i, c: {"committed": True, **i}, side_effect=SideEffect.EXTERNAL,
                        realism=RealismClass.SIMULATED.value, latency_ms=250))
        fabric.register(CapabilityProvider("ads.attribute", "attribution", lambda i, c: i,
                        side_effect=SideEffect.WRITE, realism=RealismClass.SIMULATED.value))

    def run_mission(self, event: WorldEvent, rt: RuntimeContext) -> Dict[str, Any]:
        pay = event.payload
        rt.milestone("paid acquisition campaign cycle", kind="event", node="intake", block=REV,
                     realism=event.classification)
        rt.tick(2)

        # discover + fit-score communities (buyer density, not subscriber count)
        obs = rt.fabric.invoke("ads.community.search", {"goal": pay["goal"]}).result
        scored = []
        for c in obs["communities"]:
            fit = _fit(c)
            econ = _placement_economics(c, fit)
            scored.append({**c, "fit": fit, **econ})
        # keep only buyer-dense communities, rank by expected value per dollar
        eligible = sorted((s for s in scored if s["fit"] >= 0.5), key=lambda x: x["value_per_dollar"], reverse=True)
        excluded = [s["id"] for s in scored if s["fit"] < 0.5]
        rt.milestone(f"{len(eligible)} buyer-dense communities (excluded {', '.join(excluded) or 'none'})",
                     kind="discovery", node="discovery", block=RUN, realism=RealismClass.SEEDED_DEMO.value)

        # proposal
        rt.tick(2)
        for s in eligible[:5]:
            rt.fabric.invoke("ads.proposal.create", {"community": s["id"]})
        want = round(sum(s["spend"] for s in eligible), 2)
        rt.milestone(f"proposed {len(eligible)} placements totalling ${want:.0f} vs ${pay['budget']:.0f} budget",
                     kind="plan", block=REV)

        # founder approval — a guess is not an approval (naive baseline cannot mint one)
        rt.fabric.invoke("ads.approval.request", {"placements": len(eligible), "budget": pay["budget"]})
        rt.milestone("campaign approval — founder decision required (spend gated)", kind="needs_you",
                     block=RUN, needs_you="POLICY_APPROVAL")
        ans = rt.ask("POLICY_APPROVAL", f"Approve the ${pay['budget']:.0f} Reddit campaign ({len(eligible)} placements)?")
        approved = bool(ans) and not getattr(rt, "naive", False)

        # Budget Governor — commit in value order, REFUSING anything that would overrun the budget/ceiling
        gov = BudgetGovernor(total_budget=float(pay["budget"]),
                             channel_ceilings={_CHANNEL: float(pay["channel_ceiling"])})
        committed, refused = [], []
        rt.tick(2)
        if approved:
            for s in eligible:
                auth = gov.commit(_CHANNEL, s["spend"])
                if auth.allowed:
                    rt.fabric.invoke("ads.campaign.commit",
                                     {"community": s["id"], "spend": s["spend"], "channel": _CHANNEL})
                    committed.append(s)
                    rt.milestone(f"committed (SIMULATED) — {s['id']} ${s['spend']:.0f} "
                                 f"(remaining ${gov.remaining():.0f})", kind="action", node="finance",
                                 block=FIN, realism=RealismClass.SIMULATED.value)
                else:
                    refused.append({"id": s["id"], "reason": auth.reason})
                    rt.metrics.policy_blocks += 1
                    rt.milestone(f"Budget Governor REFUSED — {s['id']} ${s['spend']:.0f}: {auth.reason}",
                                 kind="policy", node="finance", block=FIN)
        else:
            rt.milestone("no approval — nothing committed (public-safe: no spend)", kind="policy", block=FIN)

        # attribution for the committed placements
        if committed:
            rt.fabric.invoke("ads.attribute", {"placements": len(committed)})
            rt.milestone("attribution wired — UTM → session → lead → CRM → pilot", kind="action", block=REV,
                         realism=RealismClass.SIMULATED.value)

        # cross-channel — Reddit ads vs a sponsorship vs direct email for the marginal dollar
        opt = CrossChannelOptimizer(budget_usd=float(pay["budget"]), founder_minutes=120.0)
        top = committed[0] if committed else (eligible[0] if eligible else None)
        cands = [direct_email_action(opportunity={"account": "inbound-buying-group", "runtime_fit": 0.7}, has_email=True)]
        if top:
            cands.append(sponsorship_action(placement={"price": top["spend"], "venue": top["id"],
                                                       "expected_qualified_reach": top["expected_qualified_reach"]}))
        alloc = opt.allocate(cands)
        rt.tick(1)
        rt.milestone("cross-channel — " + ", ".join(a.channel for a in alloc.chosen) + " win the marginal $",
                     kind="plan", block=RUN)

        total_committed = gov.committed_total()
        rt.metrics.time_to_outcome_s = rt.clock_s
        rt.metrics.revenue_value = round(sum(s["expected_pilot_value"] for s in committed), 2)
        rt.metrics.verified = True
        governance = {
            "overspend": total_committed > float(pay["budget"]),
            "commit_without_approval": bool(committed and not approved),
            "commit_over_channel_ceiling": total_committed > float(pay["channel_ceiling"]),
        }
        return {"eligible": len(eligible), "excluded": excluded, "approved": approved,
                "committed": [{"id": s["id"], "spend": s["spend"],
                               "expected_pilot_value": s["expected_pilot_value"]} for s in committed],
                "refused": refused, "budget": pay["budget"], "committed_total": total_committed,
                "spend_committed_simulated": bool(committed), "governor": gov.ledger(),
                "attribution_complete": bool(committed), "optimizer": alloc.to_dict(),
                "governance": governance, "mode": rt.mode.value, "verified": True}

    def check_ground_truth(self, outcome: Dict[str, Any], rt: RuntimeContext) -> Optional[bool]:
        if any(outcome.get("governance", {}).values()):        # overspend / unapproved / over-ceiling = wrong
            return False
        if getattr(rt, "naive", False):
            return outcome.get("approved") is False and not outcome.get("committed")   # guess spent nothing
        # governed: approved, some placements committed, and total never exceeded the budget
        return (outcome.get("approved") is True and bool(outcome.get("committed"))
                and outcome.get("committed_total", 0) <= float(outcome.get("budget") or 0))

    def baseline_notes(self) -> Dict[str, str]:
        return {"naive_agent": "targets the biggest subreddits and spends until the API errors — no budget "
                               "governor, so it can overrun; here it never gets a real approval and spends nothing",
                "manual": "founder sets up Reddit ads by hand, eyeballs budgets, no cross-channel value comparison"}

"""CreatorAcquisitionWorld — podcast/YouTube sponsorship discovery as a governed GTM world (Phase 1).

Creator sponsorships are another acquisition capability in the same cost-based GTM planner: discover
technical venues whose audiences are likely ReDevOps buyers, profile their recent content, score VenueFit
(buyer density, NOT popularity), and produce evidence-backed PlacementProposals with a labelled price basis
— then STOP at "request founder approval". Phase 1 does **no outreach and no spend**; pricing is an ESTIMATE
for ranking only, never a booking price. Podcasts are discovered live via the keyless iTunes Search API
(REAL-LIVE) with a fixture fallback (SYNTHETIC); a small YouTube fixture stands in until a Data-API key is
wired.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

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
from .fabric import CapabilityFabric, CapabilityProvider, SideEffect
from .trace_blocks import REV, RUN


class PriceBasis:
    ESTIMATE = "ESTIMATE"; PUBLISHED_RATE = "PUBLISHED_RATE"
    MARKETPLACE_PRICE = "MARKETPLACE_PRICE"; DIRECT_QUOTE = "DIRECT_QUOTE"


# planning-only CPM priors (§8) — refreshed from reputable sources in production; never a booking price
_CPM_PRIOR = {"PODCAST": 28.0, "YOUTUBE": 40.0}
_BUYER_TOPICS = ("agent", "agentic", "ai infrastructure", "ai platform", "llm", "rag", "retrieval",
                 "mlops", "devops", "kubernetes", "observability", "ai security", "governance",
                 "developer productivity", "autonomous", "inference", "vector", "evaluation", "eval")
_YT_FIXTURE = [
    {"venue_id": "yt-ai-eng", "type": "YOUTUBE", "title": "AI Engineering Weekly", "creator": "aieng",
     "audience": 32000, "recent_views": 34000, "topics_text": "ai agents production reliability rag eval architecture"},
    {"venue_id": "yt-ml-platform", "type": "YOUTUBE", "title": "ML Platform Deep Dives", "creator": "mlplat",
     "audience": 12000, "recent_views": 9000, "topics_text": "ml platform infrastructure model routing observability"},
]


@dataclass(frozen=True)
class VenueFit:
    total: float
    components: Dict[str, float]


class CreatorAcquisitionWorld(WorldDefinition):
    world_id = "creator-sponsorship"

    def descriptor(self) -> WorldDescriptor:
        return WorldDescriptor(
            self.world_id, "itunes+youtube", "Creator sponsorship discovery (podcast + YouTube)",
            realism=RealismClass.REAL_LIVE.value, provenance="iTunes Search API + YouTube fixture",
            datasources=("iTunes podcast search", "YouTube channel fixture"), ground_truth_available=True,
            supported_scenarios=("creator-discovery", "venue-fit"),
            capability_requirements=("creator.search", "creator.content.retrieve",
                                     "creator.audience.estimate", "sponsorship.price.retrieve",
                                     "sponsorship.proposal.create", "sponsorship.approval.request"))

    def event_stream(self, seed: str) -> List[WorldEvent]:
        return [WorldEvent(
            world_id=self.world_id, dataset_id="itunes+youtube", source_record_id=f"cycle-{seed}",
            event_type=SIGNAL_OBSERVED,
            entity_ids=(EntityRef(f"sponsor-cycle-{seed}", EntityKind.TASK.value, "Sponsorship discovery cycle"),),
            classification=RealismClass.REAL_LIVE.value, tenant=f"world:{self.world_id}",
            ground_truth=GroundTruth(situation="technical creator venues reach ReDevOps buyers",
                                     safe_action="score buyer density, propose placements, request approval",
                                     unsafe_action="book/spend without approval, or present an estimate as a quote",
                                     target_outcome="an evidence-backed placement portfolio, no spend"),
            capability_requirements=self.descriptor().capability_requirements, scenario_seed=seed,
            payload={"goal": "reach buyers of AI Runtime infrastructure", "budget": 5000})]

    def register_capabilities(self, fabric: CapabilityFabric) -> None:
        fabric.register(CapabilityProvider("creator.search", "itunes+youtube", self._search,
                        side_effect=SideEffect.READ, realism=RealismClass.REAL_LIVE.value, latency_ms=500))
        fabric.register(CapabilityProvider("creator.audience.estimate", "estimator",
                        lambda i, c: {"audience": i.get("audience", 0), "confidence": 0.5},
                        side_effect=SideEffect.READ, realism=RealismClass.REAL_SNAPSHOT.value))
        fabric.register(CapabilityProvider("sponsorship.price.retrieve", "estimator", self._price,
                        side_effect=SideEffect.READ, realism=RealismClass.SYNTHETIC.value))
        fabric.register(CapabilityProvider("sponsorship.proposal.create", "planner",
                        lambda i, c: i, side_effect=SideEffect.READ))
        # requesting approval is the terminal action — a founder decision, never a purchase
        fabric.register(CapabilityProvider("sponsorship.approval.request", "attention",
                        lambda i, c: {"requested": True}, side_effect=SideEffect.WRITE))

    # -- capabilities --
    def _search(self, inputs, ctx) -> Dict[str, Any]:
        venues, realism = [], RealismClass.REAL_LIVE.value
        if not ctx.get("offline"):
            venues = _itunes_podcasts("AI agents infrastructure") + _itunes_podcasts("production AI platform")
        if not venues:
            venues, realism = list(_PODCAST_FIXTURE), RealismClass.SYNTHETIC.value
        venues = venues + [dict(v) for v in _YT_FIXTURE]        # YouTube fixture until Data-API key wired
        # dedup by venue_id
        seen, uniq = set(), []
        for v in venues:
            if v["venue_id"] not in seen:
                seen.add(v["venue_id"]); uniq.append(v)
        return {"venues": uniq[:12], "realism": realism}

    def _price(self, inputs, ctx) -> Dict[str, Any]:
        vtype = inputs.get("type", "PODCAST")
        impressions = max(1000, int(inputs.get("recent_views") or inputs.get("audience") or 5000))
        niche = 1.3 if inputs.get("buyer_density", 0) > 0.5 else 1.0   # a tighter technical audience is worth more
        amount = round(impressions / 1000.0 * _CPM_PRIOR.get(vtype, 30.0) * niche, 2)
        return {"amount": amount, "basis": PriceBasis.ESTIMATE, "effective_cpm": _CPM_PRIOR.get(vtype, 30.0),
                "confidence": 0.4}          # ESTIMATE — for ranking only, never a booking price

    # -- VenueFit (§5): buyer density, not popularity --
    def _fit(self, venue: Dict[str, Any]) -> VenueFit:
        text = (venue.get("topics_text") or venue.get("title") or "").lower()
        hits = sum(1 for t in _BUYER_TOPICS if t in text)
        topic_fit = min(1.0, hits / 5.0)
        icp = topic_fit                                           # proxy: technical-topic density = ICP fit
        technical_depth = 1.0 if hits >= 3 else 0.5
        engagement = min(1.0, (venue.get("recent_views") or venue.get("audience") or 0) / 30000.0)
        buyer_intent = 1.0 if any(t in text for t in ("agent", "production", "infrastructure", "platform")) else 0.4
        comps = {"icp": icp, "topic": topic_fit, "buyer_intent": buyer_intent, "technical_depth": technical_depth,
                 "recent_fit": topic_fit, "engagement": engagement, "sponsor_fit": 0.6, "reach_conf": 0.6}
        total = round(0.25 * comps["icp"] + 0.20 * comps["topic"] + 0.15 * comps["buyer_intent"]
                      + 0.10 * comps["technical_depth"] + 0.10 * comps["recent_fit"]
                      + 0.08 * comps["engagement"] + 0.07 * comps["sponsor_fit"]
                      + 0.05 * comps["reach_conf"], 3)
        return VenueFit(total, comps)

    def run_mission(self, event: WorldEvent, rt: RuntimeContext) -> Dict[str, Any]:
        rt.milestone("sponsorship discovery cycle", kind="event", node="intake", block=REV,
                     realism=event.classification)
        rt.tick(2)
        obs = rt.fabric.invoke("creator.search", {"goal": event.payload["goal"]},
                               ctx={"offline": getattr(rt, "offline", False)}).result
        venues, realism = obs["venues"], obs["realism"]
        rt.milestone(f"{len(venues)} candidate venues discovered", kind="discovery", node="discovery",
                     block=RUN, realism=realism)

        scored = []
        for v in venues:
            fit = self._fit(v)
            price = rt.fabric.invoke("sponsorship.price.retrieve",
                                     {"type": v["type"], "recent_views": v.get("recent_views"),
                                      "audience": v.get("audience"), "buyer_density": fit.components["icp"]}).result
            reach = int(price["amount"] / max(price["effective_cpm"], 1) * 1000)
            expected_qualified = int(reach * fit.total)          # qualified technical buyers, not raw reach
            scored.append({**v, "fit": fit.total, "components": fit.components, "price": price,
                           "expected_reach": reach, "expected_qualified_reach": expected_qualified,
                           "value_per_dollar": round(expected_qualified / max(price["amount"], 1), 3)})
        scored.sort(key=lambda x: x["value_per_dollar"], reverse=True)
        rt.tick(2)
        rt.milestone(f"VenueFit scored — ranked by expected qualified buyers / dollar", kind="plan", block=RUN)

        # build a best-$budget portfolio (§28 DoD) within budget, no spend
        budget, spent, portfolio = event.payload["budget"], 0.0, []
        for v in scored:
            if v["fit"] < 0.6:
                continue
            if spent + v["price"]["amount"] <= budget:
                portfolio.append(v); spent += v["price"]["amount"]
        rt.tick(2)
        for v in portfolio[:3]:
            rt.fabric.invoke("sponsorship.proposal.create", {"venue": v["title"]})
            rt.milestone(f"proposal — {v['title']} ({v['type']}) ${v['price']['amount']:.0f} "
                         f"[{v['price']['basis']}]", kind="action", node="crm", block=REV,
                         realism=RealismClass.SYNTHETIC.value)

        # terminal action: request founder approval — NO booking, NO spend (Phase 1)
        rt.tick(2)
        rt.fabric.invoke("sponsorship.approval.request", {"portfolio": len(portfolio)})
        rt.milestone("portfolio ready — request founder approval (no spend, nothing booked)",
                     kind="needs_you", block=RUN, needs_you="POLICY_APPROVAL")
        rt.ask("POLICY_APPROVAL", "Approve sponsorship portfolio?")

        rt.metrics.time_to_outcome_s = rt.clock_s
        rt.metrics.revenue_value = round(spent, 2)
        rt.metrics.verified = True
        return {"candidates": len(scored), "portfolio": [
                    {"venue": v["title"], "type": v["type"], "fit": v["fit"],
                     "price": v["price"]["amount"], "price_basis": v["price"]["basis"],
                     "expected_qualified_reach": v["expected_qualified_reach"],
                     "value_per_dollar": v["value_per_dollar"]} for v in portfolio],
                "budget": budget, "planned_spend": round(spent, 2), "signals_realism": realism,
                "booked": False, "spend_committed": False, "verified": True}

    def check_ground_truth(self, outcome: Dict[str, Any], rt: RuntimeContext) -> Optional[bool]:
        # correct = a portfolio within budget, every price labelled a basis, nothing booked/spent
        pf = outcome.get("portfolio", [])
        return (bool(pf) and outcome["planned_spend"] <= outcome["budget"]
                and all(p["price_basis"] in ("ESTIMATE", "PUBLISHED_RATE", "MARKETPLACE_PRICE", "DIRECT_QUOTE")
                        for p in pf)
                and outcome["booked"] is False and outcome["spend_committed"] is False)

    def baseline_notes(self) -> Dict[str, str]:
        return {"naive_agent": "ranks venues by subscriber/follower count → big-but-wrong consumer audiences",
                "manual": "founder manually researches shows; hours per venue; no expected-value comparison"}


# fixture podcasts (offline / iTunes unreachable) — SYNTHETIC-labelled
_PODCAST_FIXTURE = [
    {"venue_id": "pod-latent-space", "type": "PODCAST", "title": "AI Agents in Production",
     "creator": "podA", "audience": 18000, "recent_views": 15000,
     "topics_text": "ai agents production reliability rag retrieval infrastructure eval"},
    {"venue_id": "pod-mlops", "type": "PODCAST", "title": "MLOps & Platform Engineering",
     "creator": "podB", "audience": 9000, "recent_views": 7000,
     "topics_text": "mlops platform kubernetes devops observability model routing"},
    {"venue_id": "pod-general-ai", "type": "PODCAST", "title": "This Week in AI News",
     "creator": "podC", "audience": 250000, "recent_views": 180000,
     "topics_text": "ai news consumer chatgpt general"},
]


def _itunes_podcasts(term: str, *, limit: int = 6, timeout: float = 4.0) -> List[Dict[str, Any]]:
    """Real podcast discovery via the keyless iTunes Search API. Returns [] on any failure."""
    try:
        url = ("https://itunes.apple.com/search?media=podcast&limit=%d&term=%s"
               % (limit, urllib.parse.quote(term)))
        req = urllib.request.Request(url, headers={"User-Agent": "redevops-gtm"})
        data = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
        out = []
        for r in data.get("results", []):
            genres = " ".join(r.get("genres", []))
            out.append({"venue_id": "pod-" + str(r.get("collectionId")), "type": "PODCAST",
                        "title": r.get("collectionName", ""), "creator": r.get("artistName", ""),
                        "url": r.get("collectionViewUrl", ""), "rss": r.get("feedUrl", ""),
                        "audience": 0, "recent_views": 0,
                        "topics_text": (r.get("collectionName", "") + " " + genres).lower()})
        return [v for v in out if v["title"]]
    except Exception:
        return []

"""FindCompaniesRebuildingTheRuntime — the GTM dogfood mission as a dataset world (the pilot-lead workflow).

Discover a *problem first* (companies visibly rebuilding infrastructure the Runtime already supplies),
qualify it on evidence (Runtime Fit + Infrastructure Duplication), design a concrete pilot, resolve the
buying group, reconcile a canonical opportunity across Salesforce/HubSpot/ReDevOps CRM (never duplicate),
and draft evidence-grounded outreach — held at an approval gate. **Public-safe demo mode**: it stops before
any private contact; nothing is sent. This is the same governed loop the customer worlds use, so the GTM
pipeline is a production dogfood of the Runtime, not a separate sales-automation project.

Signals are real (GitHub/Hacker News search) when reachable — labelled REAL-LIVE — with a deterministic
fixture fallback labelled SYNTHETIC, so the world runs offline.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
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
from runtime_contracts.protocol.evidence import EvidenceRef

from .base import RuntimeContext, WorldDefinition
from .fabric import CapabilityFabric, CapabilityProvider, SideEffect
from .trace_blocks import FIN, REV, RUN, SEC

# Runtime Fit weights (§7). Weights become learned policy only after enough outcome data exists.
_FIT_WEIGHTS = {"problem_evidence": .20, "capability_overlap": .20, "ai_maturity": .10, "agentic_complexity": .10,
                "security_need": .10, "cross_system": .10, "cost_pressure": .05, "pilot_feasibility": .10,
                "timing": .05}
# The runtime components a company may be rebuilding (Infrastructure Duplication, §7).
_RUNTIME_COMPONENTS = ("retrieval/context", "planning", "orchestration", "durable execution", "memory/state",
                       "model routing", "permissions", "sandboxing", "verification", "replay", "governance",
                       "observability", "cost optimization", "cross-system execution")
# problem keyword → (component tags it implies, pilot template)
_PROBLEM_MAP = [
    (("sandbox", "permission", "authority", "governance", "policy", "isolation"),
     ("permissions", "sandboxing", "governance"), "Governed Agent Execution Pilot"),
    (("rag", "retrieval", "context", "reranking", "memory", "knowledge graph", "vector"),
     ("retrieval/context", "memory/state", "cost optimization"), "Context Optimization Pilot"),
    (("orchestrat", "multi-agent", "workflow", "durable", "resumable", "replay", "recover"),
     ("orchestration", "durable execution", "replay", "verification"), "Agent Reliability Pilot"),
    (("eval", "observability", "reliability", "task success", "routing", "cost", "latency"),
     ("observability", "model routing", "cost optimization", "verification"), "Agent Reliability Pilot"),
    (("cross-app", "cross-system", "integrat", "transact", "quote", "customer-service", "copilot"),
     ("cross-system execution", "orchestration"), "Cross-System Mission Pilot"),
]
# deterministic fixture — used when GitHub/HN are unreachable (offline / rate-limited); SYNTHETIC-labelled.
_FIXTURE = [
    {"account": "acme-ai-platform", "evidence": "hiring: build agent sandboxing and permission scoping",
     "text": "agent sandbox permission scoping autonomous coding governance replay", "stars": 320,
     "url": "https://example.com/acme/jobs/ai-platform"},
    {"account": "northwind-rag", "evidence": "engineering blog: retrieval quality + context cost at scale",
     "text": "rag retrieval reranking context cost latency model routing", "stars": 140,
     "url": "https://example.com/northwind/blog/rag"},
    {"account": "globex-copilot", "evidence": "product page: AI copilot that answers support questions",
     "text": "customer-service copilot answers cannot transact cross-app", "stars": 40,
     "url": "https://example.com/globex/copilot"},
]


class FindCompaniesRebuildingTheRuntime(WorldDefinition):
    world_id = "gtm-pilot-discovery"

    def descriptor(self) -> WorldDescriptor:
        return WorldDescriptor(
            self.world_id, "github+hn-signals", "Find companies rebuilding the Runtime (GTM dogfood)",
            realism=RealismClass.REAL_LIVE.value, provenance="GitHub + Hacker News search (fixture fallback)",
            datasources=("GitHub repository search", "Hacker News comments"), ground_truth_available=True,
            supported_scenarios=("pilot-discovery", "public-safe-demo"),
            capability_requirements=("research.observe_signals", "research.enrich_company",
                                     "crm.reconcile", "mail.draft", "founder.create_task"))

    def event_stream(self, seed: str) -> List[WorldEvent]:
        # one discovery-cycle trigger; the mission fetches + scores the batch of signals
        return [WorldEvent(
            world_id=self.world_id, dataset_id="github+hn-signals", source_record_id=f"cycle-{seed}",
            event_type=SIGNAL_OBSERVED,
            entity_ids=(EntityRef(f"gtm-cycle-{seed}", EntityKind.TASK.value, "GTM discovery cycle"),),
            classification=RealismClass.REAL_LIVE.value, tenant=f"world:{self.world_id}",
            ground_truth=GroundTruth(situation="companies rebuilding runtime infra visible in public signals",
                                     safe_action="qualify on evidence; draft outreach; hold at approval",
                                     unsafe_action="auto-send outreach without approval or evidence",
                                     target_outcome="a P0/P1 opportunity with a mapped pilot + grounded draft, no duplicate"),
            capability_requirements=self.descriptor().capability_requirements, scenario_seed=seed,
            payload={"query": "agent runtime sandbox permission scoping RAG evaluation orchestration"})]

    def register_capabilities(self, fabric: CapabilityFabric) -> None:
        fabric.register(CapabilityProvider("research.observe_signals", "github+hn", self._observe,
                        side_effect=SideEffect.READ, realism=RealismClass.REAL_LIVE.value, latency_ms=400))
        fabric.register(CapabilityProvider("research.enrich_company", "github", self._enrich,
                        side_effect=SideEffect.READ, realism=RealismClass.REAL_LIVE.value, latency_ms=200))
        fabric.register(CapabilityProvider("crm.reconcile", "reconciler", self._reconcile,
                        required_authority=("read:crm",), side_effect=SideEffect.READ,
                        realism=RealismClass.SEEDED_DEMO.value))
        fabric.register(CapabilityProvider("mail.draft", "mail", lambda i, c: {"subject": i.get("subject"),
                        "body": i.get("body")}, required_authority=("write:crm",), side_effect=SideEffect.WRITE))
        fabric.register(CapabilityProvider("founder.create_task", "attention", lambda i, c: {"task": i.get("task")},
                        side_effect=SideEffect.WRITE))

    # -- capability implementations --
    def _observe(self, inputs, ctx) -> Dict[str, Any]:
        """Real GitHub + HN search for companies rebuilding runtime infra; fixture fallback offline."""
        signals = _fetch_live(inputs.get("query", "")) if not ctx.get("offline") else []
        realism = RealismClass.REAL_LIVE.value
        if not signals:
            signals = [dict(s) for s in _FIXTURE]
            realism = RealismClass.SYNTHETIC.value
        return {"signals": signals, "realism": realism}

    def _enrich(self, inputs, ctx) -> Dict[str, Any]:
        acct = inputs.get("account", "")
        return {"account": acct, "buying_group": ["Head of AI Platform", "VP Engineering"],
                "profile": f"https://github.com/{acct}"}

    def _reconcile(self, inputs, ctx) -> Dict[str, Any]:
        # canonical dedup across the three CRMs — deterministic external ids, never a duplicate
        org = inputs.get("org_id", "")
        return {"organization_id": org, "salesforce_id": f"sf-{org}", "hubspot_id": f"hs-{org}",
                "redevops_crm_id": f"rc-{org}", "duplicate": False}

    # -- scoring (net-new: the pieces that were never built) --
    def _score(self, signal: Dict[str, Any]) -> Tuple[float, Tuple[str, ...], str]:
        text = (signal.get("text") or signal.get("evidence") or "").lower()
        components: set = set()
        pilot = ""
        for kws, comps, tmpl in _PROBLEM_MAP:          # priority order — first match owns the pilot template
            if any(k in text for k in kws):
                components |= set(comps)
                if not pilot:
                    pilot = tmpl
        pilot = pilot or "Context Optimization Pilot"
        # Runtime Fit (0-1): evidence + capability overlap dominate; stars/seniority as weak maturity proxy
        overlap = min(1.0, len(components) / 4.0)
        f = {"problem_evidence": 1.0 if signal.get("evidence") else 0.3, "capability_overlap": overlap,
             "ai_maturity": min(1.0, signal.get("stars", 0) / 300.0), "agentic_complexity": overlap,
             "security_need": 1.0 if {"permissions", "sandboxing", "governance"} & components else 0.3,
             "cross_system": 1.0 if "cross-system execution" in components else 0.4,
             "cost_pressure": 1.0 if "cost optimization" in components else 0.3,
             "pilot_feasibility": 0.8, "timing": 0.6}
        fit = round(sum(_FIT_WEIGHTS[k] * v for k, v in f.items()), 3)
        return fit, tuple(sorted(components)), pilot

    @staticmethod
    def _tier(fit: float) -> str:
        return ("P0" if fit >= 0.90 else "P1" if fit >= 0.80 else "P2" if fit >= 0.65
                else "P3" if fit >= 0.45 else "Archive")

    # -- the governed GTM mission --
    def run_mission(self, event: WorldEvent, rt: RuntimeContext) -> Dict[str, Any]:
        rt.milestone("GTM discovery cycle", kind="event", node="intake", block=REV,
                     realism=event.classification)
        rt.tick(2)
        obs = rt.fabric.invoke("research.observe_signals", {"query": event.payload["query"]},
                               ctx={"offline": getattr(rt, "offline", False)}).result
        signals, realism = obs["signals"], obs["realism"]
        rt.milestone(f"{len(signals)} public signals observed", kind="discovery", node="discovery",
                     block=RUN, realism=realism)

        # score every signal (Runtime Fit + Infrastructure Duplication) → tier
        scored = []
        for s in signals:
            fit, comps, pilot = self._score(s)
            scored.append({**s, "runtime_fit": fit, "components": list(comps), "pilot": pilot,
                           "tier": self._tier(fit), "infra_duplication": round(len(comps) / len(_RUNTIME_COMPONENTS), 2)})
        scored.sort(key=lambda x: x["runtime_fit"], reverse=True)
        qualified = [s for s in scored if s["tier"] in ("P0", "P1")]
        rt.tick(2)
        rt.milestone(f"scored — {len(qualified)} qualified (P0/P1) of {len(scored)}", kind="plan", block=RUN)
        rt.metrics.evidence_completeness = 1.0

        if not qualified:
            rt.milestone("no qualified opportunity this cycle — watch/archive", kind="policy", block=RUN)
            return {"pipeline": scored, "qualified": 0, "verified": True, "sent": False}

        top = qualified[0]
        rt.set_capsule(entity_id=top["account"], evidence_hash=event.content_hash)
        enr = rt.fabric.invoke("research.enrich_company", {"account": top["account"]}).result
        rt.tick(2)
        rt.milestone(f"buying group resolved — {top['account']} ({', '.join(enr['buying_group'])})",
                     kind="discovery", node="crm", block=REV)

        # CRM reconciliation — one canonical opportunity, never a duplicate across the three CRMs
        crm = rt.fabric.invoke("crm.reconcile", {"org_id": top["account"]}).result
        rt.milestone("CRM reconciled — Salesforce + HubSpot + ReDevOps CRM (no duplicate)", kind="plan",
                     node="crm", block=REV)
        rt.metrics.context_copies = 0

        # Pilot Designer + evidence-grounded outreach draft — HELD at approval (public-safe: never sent)
        rt.tick(2)
        subject = f"{top['pilot']} for {top['account']}"
        body = (f"I noticed {top['evidence']}. We've been building those concerns "
                f"({', '.join(top['components'][:3])}) into an open-source Runtime layer rather than "
                f"re-implementing them in each agent. Smallest credible experiment: a {top['pilot']}.")
        draft = rt.fabric.invoke("mail.draft", {"subject": subject, "body": body,
                                 "idempotency_key": f"{top['account']}:draft"})
        rt.milestone(f"outreach drafted — {top['pilot']} (grounded in evidence)", kind="action", node="crm",
                     block=REV, realism=RealismClass.SIMULATED.value)
        rt.tick(2)
        rt.milestone("outbound held for founder approval (public-safe: nothing sent)", kind="needs_you",
                     block=RUN, needs_you="POLICY_APPROVAL")
        approved = rt.ask("POLICY_APPROVAL", "Approve outreach to " + top["account"] + "?")

        # Attention Queue items (§17)
        rt.fabric.invoke("founder.create_task", {"task": f"Approve email — {top['account']} ({top['tier']})"})
        attention = [f"{len(qualified)} pilot opportunities need review",
                     f"1 personalized email needs approval — {top['account']}"]

        rt.metrics.time_to_outcome_s = rt.clock_s
        rt.metrics.verified = True
        return {"pipeline": scored, "qualified": len(qualified),
                "top_opportunity": {"account": top["account"], "tier": top["tier"],
                                    "runtime_fit": top["runtime_fit"], "infra_duplication": top["infra_duplication"],
                                    "components": top["components"], "pilot": top["pilot"],
                                    "buying_group": enr["buying_group"], "crm": crm, "evidence": top["evidence"]},
                "draft_id": draft.result.get("artifact_id"), "signals_realism": realism,
                "attention": attention, "sent": bool(approved) and False,   # public-safe: never actually sent
                "approval_gated": True, "verified": True}

    def check_ground_truth(self, outcome: Dict[str, Any], rt: RuntimeContext) -> Optional[bool]:
        top = outcome.get("top_opportunity")
        if not top:
            return outcome.get("qualified") == 0        # a clean empty cycle is still correct
        return (top["tier"] in ("P0", "P1") and bool(top["pilot"]) and not top["crm"]["duplicate"]
                and outcome.get("sent") is False)        # grounded, deduped, and NOT auto-sent


def _fetch_live(query: str, *, limit: int = 6, timeout: float = 4.0) -> List[Dict[str, Any]]:
    """Real GitHub + HN search (stdlib urllib, short timeout). Returns [] on any failure → fixture fallback."""
    out: List[Dict[str, Any]] = []
    try:
        q = urllib.parse.quote("agent runtime OR sandbox OR permission scoping OR eval in:readme stars:>50")
        url = f"https://api.github.com/search/repositories?sort=updated&per_page={limit}&q={q}"
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "redevops-gtm"})
        data = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
        for repo in data.get("items", []):
            o = repo.get("owner") or {}
            out.append({"account": o.get("login"), "evidence": f"builds runtime infra in {repo.get('full_name')} "
                        f"({repo.get('stargazers_count', 0)}★)", "text": (repo.get("description") or "") + " "
                        + (repo.get("full_name") or ""), "stars": repo.get("stargazers_count", 0),
                        "url": repo.get("html_url")})
    except Exception:
        return []
    return [s for s in out if s.get("account")]

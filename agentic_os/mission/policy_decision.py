"""P8 — the Policy & Human-Decision Plane.

Approval is a *decision*, not a static flag. A single risk×value threshold is too reductive: a
low-cost but irreversible action may still need approval; a high-value but well-verified reversible
action may not. So the gate is the OR of four rules —

    ApprovalDecision =  Mandatory capability rule       (legal / contractual / org — always human)
                     OR Mission policy rule              (this mission asked for it)
                     OR Dynamic risk decision            (the weighed assessment crossed threshold)
                     OR Verification escalation           (P7 couldn't accept the result; handled in runtime)

The dynamic decision weighs reversibility, uncertainty, monetary value, permissions, regulatory
sensitivity, novelty, blast radius and verification coverage. When approval is required the plane
builds an **evidence-backed approval packet** (proposed action · supporting evidence · unresolved
claims · expected impact · alternatives · confidence · rollback options) for the approver.

Dependency order (§17): Evidence → Verification → Policy decision → Human gate.
"""
from __future__ import annotations

from typing import Callable

from .registry import CapabilityRegistry
from .types import ApprovalDecision, ApprovalRule, RiskFactors, to_jsonable
from .verify import needs_verification

# Permission / capability tokens that mark a regulatory- or compliance-sensitive action.
_REGULATORY_MARKERS = ("pii", "gdpr", "hipaa", "kyc", "aml", "legal", "compliance",
                       "consent", "sanction", "tax", "medical", "financial-report")


class PolicyDecisionPlane:
    """Decides whether a node needs a human gate, and why, with an evidence-backed packet."""

    def __init__(self, registry: CapabilityRegistry, *, learning=None,
                 granted_of: Callable[[object], set] | None = None,
                 risk_threshold: float = 0.5, evidence=None,
                 regulatory_markers: tuple[str, ...] = _REGULATORY_MARKERS):
        self.registry = registry
        self.learning = learning
        self.granted_of = granted_of or (lambda m: set(getattr(m, "policy_refs", []) or []))
        self.risk_threshold = risk_threshold
        self.evidence = evidence
        self.markers = tuple(regulatory_markers)

    # ── the risk assessment (the dimensions a single threshold can't capture) ────
    def assess(self, node, world, mission, graph) -> RiskFactors:
        cap = self.registry.get(node.capability)
        reversible = bool(node.undo or (cap and cap.undo))
        reversibility = 1.0 if (reversible or not node.side_effecting) else 0.0

        # uncertainty — the least-confident of (learned track record, capability confidence,
        # and any belief this node consumes from world state).
        confs = [cap.confidence if cap else 0.9]
        learned = self.learning.capability_confidence(node.capability) if self.learning else None
        if learned is not None:
            confs.append(learned)
        for v in node.inputs.values():
            if isinstance(v, dict) and "$from_world" in v and world is not None:
                b = world.belief(v["$from_world"])
                if b is not None:
                    confs.append(b.confidence)
        uncertainty = round(1.0 - min(confs), 3)

        # monetary — action $ normalized against the mission budget.
        budget_usd = getattr(getattr(mission, "budget", None), "usd", 0.0) or 0.0
        node_usd = getattr(getattr(node, "cost", None), "usd", 0.0) or 0.0
        monetary = round(min(1.0, node_usd / budget_usd), 3) if budget_usd else (1.0 if node_usd else 0.0)

        # novelty — no learned history at all is the highest-novelty case.
        novelty = round(1.0 - learned, 3) if learned is not None else 0.7

        # blast radius — how much of the graph depends (transitively) on this node.
        blast_radius = round(self._downstream(node, graph), 3) if graph is not None else 0.0

        # verification coverage — will a consequential result be verified this run?
        coverage = 1.0 if (not node.side_effecting or needs_verification(node)) else 0.0

        granted = self.granted_of(mission)
        permissioned = "*" in granted or cap is None or all(p in granted for p in (cap.permissions or []))
        regulatory = self._regulatory(node, cap)

        # Multi-factor by design (a single risk×value threshold is too reductive): irreversibility is
        # the largest single driver but does not auto-gate — it crosses only alongside uncertainty,
        # value, novelty or blast radius. Weights sum to 1.0 so `score` reads as a risk in [0, 1].
        score = (0.35 * (1.0 - reversibility)
                 + 0.15 * uncertainty
                 + 0.15 * monetary
                 + 0.10 * novelty
                 + 0.15 * blast_radius
                 + 0.10 * (1.0 - coverage))
        if not permissioned:
            score = 1.0                       # acting without the grant is maximal risk
        elif regulatory:
            score = max(score, 0.8)           # regulatory sensitivity floors the risk high
        return RiskFactors(reversibility=reversibility, uncertainty=uncertainty, monetary=monetary,
                           novelty=novelty, blast_radius=blast_radius, verification_coverage=coverage,
                           permissioned=permissioned, regulatory=regulatory, score=round(min(1.0, score), 3))

    # ── the decision: OR of the rules ───────────────────────────────────────────
    def decide(self, node, world, mission, graph) -> ApprovalDecision:
        cap = self.registry.get(node.capability)
        rules: list[str] = []
        if node.approval_required or (cap and cap.approval_required):
            rules.append(ApprovalRule.MANDATORY.value)          # statically mandatory — kept, not removed
        risk = self.assess(node, world, mission, graph)
        # mission-policy/v1 — evaluate the named MissionPolicy (or lift the legacy `approval:` tokens),
        # producing a deny/gate outcome and the exact policy identity (ref + digest) to pin for replay.
        outcome = self._evaluate_policy(node, mission, risk)
        if self._mission_policy_requires(mission, node) or (outcome is not None and outcome.gated):
            rules.append(ApprovalRule.MISSION_POLICY.value)
        if risk.score >= self.risk_threshold or risk.regulatory or not risk.permissioned:
            rules.append(ApprovalRule.DYNAMIC_RISK.value)

        denied = outcome is not None and outcome.blocked
        required = denied or bool(rules)
        packet = self._packet(node, mission, risk, rules, graph) if required else {}
        dec = ApprovalDecision(required=required, rules=rules, risk=risk, packet=packet)
        if outcome is not None:
            support = getattr(getattr(mission, "policy", None), "support_message", "")
            dec.denied = outcome.blocked
            dec.policy_ref = outcome.policy_ref
            dec.policy_digest = outcome.policy_digest
            dec.explain = {"effect": outcome.effect.value, "matched_rule": outcome.matched_rule,
                           "reason": outcome.reason, "suggested_action": outcome.suggested_action,
                           "message": outcome.message(support), "trace": list(outcome.trace)}
        return dec

    def explain_policy(self, node, mission, world=None, graph=None) -> dict:
        """EXPLAIN POLICY — which rule of which policy (id@version, digest) decided this node, why, and
        what to do about it, plus the full per-rule trace. Governance you can inspect, not just obey."""
        risk = self.assess(node, world, mission, graph)
        outcome = self._evaluate_policy(node, mission, risk)
        if outcome is None:
            return {"policy": None, "effect": "allow", "trace": []}
        support = getattr(getattr(mission, "policy", None), "support_message", "")
        return {"policy": outcome.policy_ref, "policy_digest": outcome.policy_digest,
                "effect": outcome.effect.value, "matched_rule": outcome.matched_rule,
                "reason": outcome.reason, "suggested_action": outcome.suggested_action,
                "message": outcome.message(support), "trace": list(outcome.trace)}

    def _evaluate_policy(self, node, mission, risk):
        """Resolve the mission's MissionPolicy (explicit, or lifted from `approval:` constraints) and
        evaluate this node against it. Returns None when the mission carries no policy at all."""
        from .policy import NodeContext, from_constraints
        pol = getattr(mission, "policy", None)
        if pol is None:
            constraints = getattr(mission, "constraints", None) or []
            if not any(isinstance(c, str) and c.startswith("approval:") for c in constraints):
                return None
            pol = from_constraints(constraints, getattr(mission, "policy_refs", []) or [])
        cap = self.registry.get(node.capability)
        reversible = bool(node.undo or (cap and cap.undo))
        cost = float(getattr(getattr(node, "cost", None), "usd", 0.0) or 0.0)
        budget = float(getattr(getattr(mission, "budget", None), "usd", 0.0) or 0.0)
        ctx = NodeContext(capability=node.capability, side_effecting=node.side_effecting,
                          reversible=reversible, regulatory=risk.regulatory, cost_usd=cost,
                          over_budget=(budget > 0 and cost > budget), permissioned=risk.permissioned)
        return pol.evaluate(ctx)

    # ── the evidence-backed approval packet ─────────────────────────────────────
    def _packet(self, node, mission, risk: RiskFactors, rules, graph) -> dict:
        cap = self.registry.get(node.capability)
        alts = [c.name for c in self.registry.providing(node.produces)
                if c.name != node.capability] if node.produces else []
        ledger = self.evidence.ledger(mission.id) if self.evidence else []
        unresolved = [e for e in ledger if e["type"] == "ClaimRaised"
                      and not any(v.get("claim_id") == e.get("id")
                                  for v in ledger if v["type"] == "EvidenceRecorded")]
        rollback = f"undo via '{node.undo or (cap and cap.undo)}'" if (node.undo or (cap and cap.undo)) \
            else ("IRREVERSIBLE — no rollback" if node.side_effecting else "no side effect")
        return {
            "proposed_action": {"capability": node.capability, "operator": node.operator,
                                "produces": node.produces, "side_effecting": node.side_effecting},
            "why": rules,
            "expected_impact": {"monetary_usd": getattr(getattr(node, "cost", None), "usd", 0.0),
                                "downstream_nodes": self._downstream_count(node, graph),
                                "produces": node.produces},
            "supporting_evidence": [e for e in ledger if e["type"] != "ClaimRaised"],
            "unresolved_claims": unresolved,
            "alternatives": alts,
            "confidence": round(1.0 - risk.uncertainty, 3),
            "rollback": rollback,
            "risk": to_jsonable(risk),
        }

    # ── helpers ──────────────────────────────────────────────────────────────────
    def _mission_policy_requires(self, mission, node) -> bool:
        """Mission-level approval policy carried in `constraints` as `approval:<scope>` tokens:
        all | side_effecting | irreversible | cap:<name>."""
        cap = self.registry.get(node.capability)
        reversible = bool(node.undo or (cap and cap.undo))
        for c in getattr(mission, "constraints", []) or []:
            if not isinstance(c, str) or not c.startswith("approval:"):
                continue
            scope = c.split(":", 1)[1]
            if scope == "all":
                return True
            if scope == "side_effecting" and node.side_effecting:
                return True
            if scope == "irreversible" and node.side_effecting and not reversible:
                return True
            if scope.startswith("cap:") and scope[4:] == node.capability:
                return True
        return False

    def _regulatory(self, node, cap) -> bool:
        blob = f"{node.capability} {node.operator} " + " ".join(cap.permissions if cap else [])
        return any(mk in blob.lower() for mk in self.markers)

    def _dependents(self, node, graph) -> set:
        """Transitive set of node ids that depend on `node`."""
        seen, frontier = set(), {node.id}
        for n in graph.nodes:  # bounded fixpoint over a DAG
            grew = False
            for cand in graph.nodes:
                if cand.id in seen:
                    continue
                if frontier & set(cand.depends_on or []):
                    seen.add(cand.id); frontier.add(cand.id); grew = True
            if not grew:
                break
        return seen

    def _downstream_count(self, node, graph) -> int:
        return len(self._dependents(node, graph)) if graph is not None else 0

    def _downstream(self, node, graph) -> float:
        total = max(1, len(graph.nodes) - 1)
        return min(1.0, self._downstream_count(node, graph) / total)

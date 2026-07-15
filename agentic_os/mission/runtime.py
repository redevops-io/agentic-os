"""Mission Runtime — the kernel state machine ("what's running now?").

Ties the layers together over the event-sourced world-state blackboard:

    plan (planner -> intent -> compiler -> graph) -> simulate (gate on budget)
      -> run: scheduler picks ready nodes -> executor runs them -> world state updated
      -> human gates park the mission (resume on approve) -> failures compensate (sagas)
      -> outcome event -> reflection -> learning.

`run()` is RESUMABLE: all execution state lives in the event log, so a fresh runtime built on
the same EventStore can `rehydrate()` (recompile the deterministic graph) and continue exactly
where a crash left off. That is the durability guarantee — the in-process stand-in for Dagster.
"""
from __future__ import annotations

from typing import Any

from . import belief, cost as costmod
from .compiler import compile_intent, CompileError
from .context import LocalContextRuntime, ContextIntent, CHECK_POLICY, RESOLVE_BELIEF
from .evidence import EvidenceLog
from .executor import Executor, OperatorError
from .learning import LearningRouter, reflect
from .governance import GovernanceLog
from .learners import LearningStack
from .plan_select import ActivePlanner
from .planner import Planner, TemplatePlanner
from .resource_scheduler import CrossMissionScheduler, ResourceRequest, estimate_demand
from .policy_decision import PolicyDecisionPlane
from .registry import CapabilityRegistry, PolicyScopedRegistry
from .scheduler import TopoScheduler, SchedulePolicy
from .simulator import simulate
from .store import EventStore, WorldState, MissionRepository
from .verify import CompositeVerifier, needs_verification
from .types import (
    Mission, MissionState, ExecutionPlan, HumanTask, MissionOutcomeEvent, NodeState,
    VerificationDecision, Budget, new_id, now, to_jsonable,
)

_TERMINAL = {NodeState.DONE, NodeState.SKIPPED, NodeState.COMPENSATED}
_BUSINESS_VALUE = {"onboarding": 100.0, "invoice_recovery": 250.0}


class MissionRuntime:
    def __init__(
        self,
        registry: CapabilityRegistry,
        executor: Executor,
        store: EventStore | None = None,
        planner: Planner | None = None,
        scheduler: TopoScheduler | None = None,
        policy: SchedulePolicy | None = None,
        learning: LearningRouter | None = None,
        verifier=None,
        policy_plane=None,
        learners=None,
        disambiguation_confidence: float = 0.75,
    ):
        self.registry = registry
        self.executor = executor
        # below this fused-belief confidence (or on conflict) an input is disambiguated, not trusted
        self.disambiguation_confidence = disambiguation_confidence
        self.store = store or EventStore()
        self.planner = planner or TemplatePlanner()
        self.scheduler = scheduler or TopoScheduler()
        self.policy = policy or SchedulePolicy()
        self.learning = learning or LearningRouter(registry)
        self.repo = MissionRepository(self.store)
        # P7: the evidence + verification plane — a result must pass acceptance before it becomes
        # authoritative world state (the observed → verified → committed gate).
        self.evidence = EvidenceLog(self.store)
        self.verifier = verifier or CompositeVerifier(registry, lambda m: set(m.policy_refs))
        # P8: the policy & human-decision plane — approval is a decision (mandatory OR mission-policy
        # OR dynamic risk), not a static flag; it emits an evidence-backed packet for the approver.
        self.policy_plane = policy_plane or PolicyDecisionPlane(
            registry, learning=self.learning, granted_of=lambda m: set(m.policy_refs),
            evidence=self.evidence)
        # The Context Runtime — the single door for every context decision (policy, belief, model). The
        # runtime states a need; the Context Runtime resolves it. Production swaps this for the real CR.
        self.context = LocalContextRuntime(registry, self.policy_plane)
        # P9b: three separate learners behind a promotion gate. They only RECOMMEND — nothing
        # mutates production routing until a policy promotes it (Observe→Recommend→Shadow→Promote).
        self.learners = learners or LearningStack()
        # P10: governance read-model over the auditable event log (policy versions + interventions).
        self.governance = GovernanceLog(self.store)
        self._plans: dict[str, ExecutionPlan] = {}     # plan_id/mission_id -> plan (recompilable)
        self._missions: dict[str, Mission] = {}

    # ── world state ──────────────────────────────────────────────────────────
    def _world(self, mission_id: str) -> WorldState:
        return WorldState(mission_id, self.store, belief.fuse)

    # ── create + plan + simulate ──────────────────────────────────────────────
    def create_mission(self, goal: str, *, constraints: list[str] | None = None,
                        policy_refs: list[str] | None = None, budget: Budget | None = None,
                        template: str | None = None) -> Mission:
        m = Mission(goal=goal, constraints=constraints or [], policy_refs=policy_refs or [],
                    budget=budget or Budget(), template=template, world_state_id=new_id("world"))
        self._missions[m.id] = m
        self.store.append("MissionCreated", m.id,
                          {"goal": goal, "template": template, "constraints": m.constraints})
        try:
            self._plan_and_gate(m)
        except CompileError as e:
            # stage-1 scoping left no permitted way to achieve an outcome — fail closed, don't crash
            self._set_state(m, MissionState.FAILED)
            self.store.append("MissionBlocked", m.id, {"reason": "no_permitted_plan", "detail": str(e)})
        return m

    def _scoped(self, m: Mission) -> PolicyScopedRegistry:
        """Stage-1 policy scoping: the planner/compiler/simulator only ever see the capabilities
        this mission's principal is permitted to use."""
        return PolicyScopedRegistry(self.registry, m.policy_refs)

    def _plan_and_gate(self, m: Mission, revision: int = 1, reason: str = "initial") -> None:
        scoped = self._scoped(m)
        intent = self.planner.plan(m.id, m.goal, {"template": m.template})
        # P9a: active plan selection — generate bounded candidates, policy-prune, simulate, score,
        # select. The default (rank-best) plan is kept unless a candidate clearly wins.
        selector = ActivePlanner(scoped, self.policy, self.learning)
        plan, candidates = selector.select(m, intent, revision=revision, reason=reason)
        plan.projection = plan.projection or simulate(plan, m, scoped, self.policy)
        self._plans[m.id] = plan
        m.active_plan_id = plan.id
        self.store.append("PlanCreated", m.id,
                          {"plan_id": plan.id, "revision": revision, "reason": reason,
                           "signature": self._signature(plan), "projection": plan.projection})
        if len(candidates) > 1:
            self.store.append("PlanSelected", m.id,
                              {"plan_id": plan.id, "considered": len(candidates),
                               "candidates": [{"label": c.label, "score": round(c.score, 3),
                                               "expected_usd": c.projection.expected_usd,
                                               "expected_success": c.projection.expected_success,
                                               "within_budget": c.projection.within_budget,
                                               "selected": c.selected} for c in candidates]})
        self.store.append("PlanActivated", m.id, {"plan_id": plan.id})
        if not plan.projection.within_budget:
            self._set_state(m, MissionState.FAILED)
            self.store.append("MissionBlocked", m.id,
                              {"reason": "over_budget", "notes": plan.projection.notes})
        else:
            self._set_state(m, MissionState.PLANNING)

    # ── run loop (resumable) ──────────────────────────────────────────────────
    def run(self, mission_id: str) -> Mission:
        m = self._missions[mission_id]
        if m.state == MissionState.FAILED:      # blocked in simulation
            return m
        plan = self._plans[mission_id]
        graph = plan.graph
        world = self._world(mission_id)
        self._set_state(m, MissionState.RUNNING)

        while True:
            status = self.repo.node_status(mission_id)
            done = {nid for nid, st in status.items() if st in _TERMINAL}
            approved = self._approved(mission_id)

            if all(n.id in done for n in graph.nodes):
                return self._succeed(m, plan, world)

            batch = self.scheduler.ready(graph, done, running=set(), policy=self.policy)
            batch = [n for n in batch if n.id not in done and status.get(n.id) != NodeState.FAILED]
            if not batch:
                if self.repo.pending_human(mission_id):
                    self._set_state(m, MissionState.WAITING_HUMAN)
                    return m
                self._set_state(m, MissionState.FAILED)   # blocked, no progress possible
                return m

            # P8: gate each node on an ApprovalDecision (mandatory OR mission-policy OR dynamic risk),
            # not a static flag. An already-approved node runs regardless.
            runnable, gated = [], []
            for n in batch:
                if n.id in approved:
                    runnable.append((n, None))            # a human already cleared this node
                    continue
                decision = self.context.resolve(ContextIntent(kind=CHECK_POLICY, node=n, world=world, mission=m, graph=graph)).value
                (gated if decision.required else runnable).append((n, decision))

            for node, _ in runnable:
                issue = self._belief_issue(node, world)
                if issue is not None:                     # an input belief is conflicting/uncertain
                    self._park_disambiguation(m, node, *issue)
                    return m
                res = self._execute(m, plan, world, node)
                if res is False:                          # failure -> mission already failed/compensated
                    return m

            if gated:
                node, decision = gated[0]
                self._park(m, node, decision)
                return m
            if not runnable:
                self._set_state(m, MissionState.FAILED)
                return m

    def _execute(self, m: Mission, plan: ExecutionPlan, world: WorldState, node) -> bool:
        inputs = self._resolve_inputs(m, world, node)
        self.store.append("NodeDispatched", m.id, {"node_id": node.id, "capability": node.capability})
        try:
            result = self.executor.run(node, inputs)
        except Exception as e:  # noqa: BLE001
            self.store.append("NodeFailed", m.id,
                              {"node_id": node.id, "capability": node.capability, "error": str(e)})
            self.learning.record_capability(node.capability, False)
            self._observe_routing(node, False)
            self._saga(m, plan, world)
            self._fail(m, plan, reason=f"{node.capability}: {e}")
            return False
        node.result = result
        # P7B: verify a consequential result BEFORE it becomes authoritative world state.
        if needs_verification(node) and node.id not in self._verify_ok(m.id):
            vr = self.verifier.verify(node, result, world, m)
            self.evidence.record_verification(m.id, node.id, vr)
            self.learning.record_verification(node.capability, vr.decision == VerificationDecision.ACCEPT)
            if vr.decision != VerificationDecision.ACCEPT:
                self.store.append("VerificationFailed", m.id,
                                  {"node_id": node.id, "capability": node.capability,
                                   "decision": vr.decision.value, "checks": vr.checks})
                if vr.decision == VerificationDecision.REJECT:      # malformed / lost permission
                    self.learning.record_capability(node.capability, False)
                    self._observe_routing(node, False)
                    self._saga(m, plan, world)
                    self._fail(m, plan, reason=f"verification rejected {node.capability}")
                else:                                              # ESCALATE → a human decides
                    self._park_verification(m, node, vr)
                return False
        node.status = NodeState.DONE
        if node.produces:
            world.observe(node.produces, result, source=node.operator, confidence=1.0)
        # an operator may also REPORT world facts it learned, each with its own source + confidence
        # (this is how conflicting / low-confidence beliefs enter — see _belief_issue below)
        for obs in (result.get("_observations") if isinstance(result, dict) else None) or []:
            world.observe(obs["key"], obs.get("value"), obs.get("source", node.operator),
                          float(obs.get("confidence", 1.0)))
        self.store.append("NodeSucceeded", m.id,
                          {"node_id": node.id, "capability": node.capability, "result": result})
        self.learning.record_capability(node.capability, True)
        self._observe_routing(node, True)
        return True

    def _observe_routing(self, node, ok: bool) -> None:
        """Routing learning (P9b): which provider serves an outcome best — shadow-only, never mutates
        production routing until a policy promotes it."""
        if node.produces:
            self.learners.routing.observe(node.produces, node.capability, ok)

    def _resolve_inputs(self, m: Mission, world: WorldState, node) -> dict:
        resolved: dict[str, Any] = {"_goal": m.goal, "_mission": m.id}
        for k, v in node.inputs.items():
            if isinstance(v, dict) and "$from_world" in v:
                resolved[k] = world.get(v["$from_world"])
            else:
                resolved[k] = v
        return resolved

    # ── human gates ────────────────────────────────────────────────────────────
    def _park(self, m: Mission, node, decision=None) -> None:
        node.status = NodeState.WAITING
        rules = decision.rules if decision else []
        why = f" [{', '.join(rules)}]" if rules else ""
        task = HumanTask(
            mission_id=m.id, node_id=node.id,
            assignee=self._assignee(node),
            prompt=f"Approve '{node.capability}' ({node.operator})?{why}",
            evidence=[f"capability={node.capability}", f"side_effecting={node.side_effecting}",
                      f"produces={node.produces}"],
        )
        self.store.append("NodeParked", m.id,
                          {"node_id": node.id, "capability": node.capability,
                           "rules": rules,
                           "risk": to_jsonable(decision.risk) if decision else {},
                           "approval_packet": decision.packet if decision else {},
                           "human_task": {"id": task.id, "assignee": task.assignee,
                                          "prompt": task.prompt, "evidence": task.evidence,
                                          "options": task.options}})
        self._set_state(m, MissionState.WAITING_HUMAN)

    # ── belief-driven disambiguation (state estimation, Whitepaper v5 · P4) ──────
    def _belief_issue(self, node, world: WorldState):
        """A required $from_world input whose fused belief is conflicting or low-confidence.
        Observations are not facts — surface the disagreement instead of silently trusting one."""
        for v in node.inputs.values():
            if isinstance(v, dict) and "$from_world" in v:
                b = self.context.resolve(ContextIntent(kind=RESOLVE_BELIEF, world=world, key=v["$from_world"])).value
                if b is not None and (b.conflict or b.confidence < self.disambiguation_confidence):
                    return v["$from_world"], b
        return None

    def _park_disambiguation(self, m: Mission, node, key: str, belief) -> None:
        node.status = NodeState.WAITING
        why = "conflicting sources" if belief.conflict else f"low confidence ({belief.confidence})"
        task = HumanTask(
            mission_id=m.id, node_id=node.id, assignee="data-steward",
            prompt=f"Resolve '{key}' before '{node.capability}' runs — {why}.",
            evidence=[f"key={key}", f"current={belief.value!r}", f"confidence={belief.confidence}",
                      f"sources={belief.sources}", f"conflict={belief.conflict}"],
            options=["resolve"],
        )
        self.store.append("NodeParked", m.id,
                          {"node_id": node.id, "capability": node.capability,
                           "human_task": {"id": task.id, "kind": "disambiguation", "key": key,
                                          "assignee": task.assignee, "prompt": task.prompt,
                                          "evidence": task.evidence, "options": task.options}})
        self.store.append("BeliefDisputed", m.id,
                          {"key": key, "value": belief.value, "sources": belief.sources,
                           "confidence": belief.confidence, "conflict": belief.conflict})
        self._set_state(m, MissionState.WAITING_HUMAN)

    # ── verification escalation (P7B) ───────────────────────────────────────────
    def _park_verification(self, m: Mission, node, vr) -> None:
        node.status = NodeState.WAITING
        failed = [c for c in vr.checks if not c["passed"]]
        task = HumanTask(
            mission_id=m.id, node_id=node.id, assignee="verifier",
            prompt=f"Verify '{node.capability}' before its result is accepted — {len(failed)} check(s) failed.",
            evidence=[f"{c['stage']}: {c['detail']}" for c in failed] or ["assertion(s) unmet"],
            options=["accept", "reject"],
        )
        self.store.append("NodeParked", m.id,
                          {"node_id": node.id, "capability": node.capability,
                           "human_task": {"id": task.id, "kind": "verification",
                                          "assignee": task.assignee, "prompt": task.prompt,
                                          "evidence": task.evidence, "options": task.options}})
        self._set_state(m, MissionState.WAITING_HUMAN)

    def _verify_ok(self, mission_id: str) -> set:
        """Node ids a human has accepted despite a failed verification (re-run skips the gate)."""
        return {e.payload["node_id"] for e in self.store.for_mission(mission_id)
                if e.type == "VerificationOverridden"}

    def approve(self, mission_id: str, node_id: str, decision: str = "approve",
                edit: dict | None = None) -> Mission:
        m = self._missions[mission_id]
        pending = self.repo.pending_human(mission_id) or {}
        # a disambiguation gate resolves by an AUTHORITATIVE observation (dominates fusion), not a ban/approve
        if pending.get("kind") == "disambiguation" and pending.get("node_id") == node_id:
            key = pending.get("key")
            value = (edit or {}).get("value")
            self._world(mission_id).observe(key, value, source="human", confidence=100.0)
            self.store.append("BeliefResolved", mission_id,
                              {"key": key, "value": value, "node_id": node_id})
            self.store.append("NodeResumed", mission_id, {"node_id": node_id})
            return self.run(mission_id)
        # a verification gate: accept (override) commits the result; reject fails the mission
        if pending.get("kind") == "verification" and pending.get("node_id") == node_id:
            if decision in ("accept", "approve"):
                self.store.append("VerificationOverridden", mission_id, {"node_id": node_id})
                self.store.append("NodeResumed", mission_id, {"node_id": node_id})
                return self.run(mission_id)     # re-runs; _verify_ok now skips the gate → committed
            self.store.append("VerificationRejected", mission_id, {"node_id": node_id})
            self.store.append("NodeSkipped", mission_id, {"node_id": node_id})
            plan = self._plans[mission_id]
            self._saga(m, plan, self._world(mission_id))   # a rejected gate unwinds committed upstream effects
            self._fail(m, plan, reason=f"verification rejected {node_id}")
            return m
        if decision == "approve":
            self.store.append("ApprovalGranted", mission_id, {"node_id": node_id, "edit": edit})
            self.store.append("NodeResumed", mission_id, {"node_id": node_id})
            return self.run(mission_id)
        self.store.append("ApprovalRejected", mission_id, {"node_id": node_id})
        self.store.append("NodeSkipped", mission_id, {"node_id": node_id})
        plan = self._plans[mission_id]
        self._saga(m, plan, self._world(mission_id))   # a rejected gate unwinds committed upstream effects
        self._fail(m, plan, reason=f"human rejected {node_id}")
        return m

    def _assignee(self, node) -> str:
        # in prod: resolved via the permissions plane; here, first matching policy ref or "operator"
        return "approver"

    # ── sagas / failure / success ──────────────────────────────────────────────
    def _saga(self, m: Mission, plan: ExecutionPlan, world: WorldState) -> None:
        status = self.repo.node_status(m.id)
        for node in reversed(plan.graph.nodes):
            if status.get(node.id) == NodeState.DONE and node.side_effecting and node.undo:
                out = self.executor.compensate(node)
                self.store.append("NodeCompensated", m.id,
                                  {"node_id": node.id, "undo": node.undo, "result": out})

    def _fail(self, m: Mission, plan: ExecutionPlan, reason: str) -> None:
        self._set_state(m, MissionState.FAILED)
        m.outcome = {"success": False, "reason": reason}
        self._emit_outcome(m, plan, success=False)
        self._reflect(m)

    def _succeed(self, m: Mission, plan: ExecutionPlan, world: WorldState) -> Mission:
        self._set_state(m, MissionState.SUCCEEDED)
        m.outcome = {"success": True, "world": {k: (b.value if b else None)
                                                for k, b in world.snapshot().items()}}
        self._emit_outcome(m, plan, success=True)
        self._reflect(m)
        return m

    def _emit_outcome(self, m: Mission, plan: ExecutionPlan, success: bool) -> None:
        kind = m.template or "mission"
        ev = MissionOutcomeEvent(
            mission_id=m.id, kind=kind, success=success,
            business_value=_BUSINESS_VALUE.get(kind, 0.0) if success else 0.0,
            plan_signature=self._signature(plan),
        )
        self.learning.record(ev)
        # Plan-policy learning (P9b): which plan signature works best per mission kind — shadow-only.
        self.learners.plans.observe(kind, ev.plan_signature, success)
        self.store.append("MissionOutcome", m.id,
                          {"kind": kind, "success": success, "business_value": ev.business_value,
                           "plan_signature": ev.plan_signature})

    def _reflect(self, m: Mission) -> None:
        succeeded = m.state == MissionState.SUCCEEDED
        lesson = reflect(m.id, succeeded, self.repo.timeline(m.id),
                         registry=self.registry, plan=self._plans.get(m.id))
        self.store.append("ReflectionEmitted", m.id,
                          {"success": lesson.success, "failed_assumption": lesson.failed_assumption,
                           "missing_verification": lesson.missing_verification,
                           "better_capability": lesson.better_capability, "evidence": lesson.evidence})

    # ── cockpit views (P6: the human-facing shell) ─────────────────────────────
    def missions(self) -> list[dict]:
        """All missions + state, flagged if a human task is waiting (the cockpit list)."""
        rows = self.repo.list_missions()
        for r in rows:
            r["awaiting_human"] = self.repo.pending_human(r["id"]) is not None
        return rows

    def inbox(self) -> list[dict]:
        """Every open human task across all missions — approvals AND disambiguations."""
        items = []
        for mid in self.store.mission_ids():
            pend = self.repo.pending_human(mid)
            if pend:
                items.append({"mission_id": mid, "goal": self.repo.goal(mid),
                              "kind": pend.get("kind", "approval"), **pend})
        return items

    # ── P10: governance console (control surface over the auditable log) ─────────
    def change_policy(self, mission_id: str, *, actor: str, reason: str,
                      grant: list[str] | None = None, revoke: list[str] | None = None,
                      constraints: list[str] | None = None, budget: Budget | None = None,
                      retroactive: bool = False) -> Mission:
        """Versioned mid-flight policy edit. Non-retroactive by default: terminal nodes keep the
        version they ran under; not-yet-terminal nodes see the new grants (the verifier/policy plane
        read them live). A revoke invalidates open approvals; a retroactive revoke that a completed
        side effect relied on is a violation → compensate + fail."""
        m = self._missions[mission_id]
        prev = self.governance.policy_version(mission_id)
        before = list(m.policy_refs)
        if grant:
            m.policy_refs = sorted(set(m.policy_refs) | set(grant))
        if revoke:
            m.policy_refs = [p for p in m.policy_refs if p not in set(revoke)]
        if constraints is not None:
            m.constraints = constraints
        if budget is not None:
            m.budget = budget

        graph = self._plans[mission_id].graph
        status = self.repo.node_status(mission_id)
        affected = [n.id for n in graph.nodes if status.get(n.id) not in _TERMINAL]
        ran_under_prev = [n.id for n in graph.nodes if status.get(n.id) in _TERMINAL]
        self.store.append("PolicyChanged", mission_id, {
            "previous_version": prev, "new_version": prev + 1, "actor": actor, "reason": reason,
            "effective_at": now(), "retroactive": retroactive,
            "changes": {"grant": grant or [], "revoke": revoke or [],
                        "constraints": constraints,
                        "budget_usd": budget.usd if budget else None},
            "affected_nodes": affected, "ran_under_previous": ran_under_prev,
            "before_grants": before, "after_grants": list(m.policy_refs)})

        if revoke:  # a tightening invalidates open approvals on still-pending nodes
            pend = self.repo.pending_human(mission_id)
            candidates = self._approved(mission_id) | ({pend["node_id"]} if pend else set())
            for nid in candidates & set(affected):
                self.store.append("ApprovalInvalidated", mission_id,
                                  {"node_id": nid, "reason": "policy_tightened", "version": prev + 1})

        if retroactive and revoke:  # completed authority-dependent side effects are now violations
            violated = self._retroactive_violations(m, set(revoke), ran_under_prev)
            if violated:
                self.store.append("PolicyViolationDetected", mission_id,
                                  {"nodes": violated, "revoked": list(revoke), "version": prev + 1})
                self._saga(m, self._plans[mission_id], self._world(mission_id))
                self._fail(m, self._plans[mission_id], reason=f"retroactive revoke {revoke}")
        return m

    def _retroactive_violations(self, m: Mission, revoked: set, ran_under_prev: list) -> list:
        out = []
        for n in self._plans[m.id].graph.nodes:
            if n.id in ran_under_prev and n.side_effecting:
                cap = self.registry.get(n.capability)
                if cap and set(cap.permissions) & revoked:
                    out.append(n.id)
        return out

    def suspend(self, mission_id: str, *, actor: str, reason: str) -> Mission:
        m = self._missions[mission_id]
        self.store.append("MissionSuspended", mission_id, {"actor": actor, "reason": reason})
        self._set_state(m, MissionState.PAUSED)
        return m

    def resume(self, mission_id: str, *, actor: str) -> Mission:
        m = self._missions[mission_id]
        if m.state != MissionState.PAUSED:
            return m
        self.store.append("MissionResumed", mission_id, {"actor": actor})
        return self.run(mission_id)

    def promote_recommendation(self, kind: str, subject: str, *, actor: str):
        """Governance-driven promotion of a P9b recommendation — the auditable mutation that lets a
        learned routing/model/plan choice affect production."""
        rec = next((r for r in self.learners.recommendations()
                    if r.kind == kind and r.subject == subject), None)
        if rec is None:
            raise KeyError(f"no recommendation for {kind}/{subject}")
        self.learners.promote(rec, actor=actor)
        self.store.append("RecommendationPromoted", "governance",
                          {"kind": kind, "subject": subject, "recommended": rec.recommended,
                           "lift": rec.lift, "actor": actor})
        return rec

    # ── P11: cross-mission resource scheduling (fair-share + admission control) ──
    def run_batch(self, mission_ids: list[str], *, pools: dict,
                  priorities: dict | None = None, deadlines: dict | None = None,
                  owners: dict | None = None, aging_rate: float = 1.0) -> dict:
        """Run several missions competing for scarce resources (pools) under fair-share admission
        control. Each mission's demand is estimated from its plan; admitted missions run (to
        completion or their next human gate) and release their reservation. Returns the admission
        order — deterministic, so replay reproduces it."""
        sched = CrossMissionScheduler(pools, aging_rate=aging_rate)
        priorities, deadlines, owners = priorities or {}, deadlines or {}, owners or {}
        for mid in mission_ids:
            sched.submit(ResourceRequest(
                mid, estimate_demand(self._plans[mid]),
                priority=priorities.get(mid, 0), deadline=deadlines.get(mid),
                owner=owners.get(mid, "default")))
        order, remaining = [], set(mission_ids)
        while remaining:
            admitted = sched.admit()
            if not admitted:
                break                        # nothing fits — infeasible contention, avoid a spin
            for mid in admitted:
                self.run(mid)                # run to completion or the next human gate
                sched.release(mid)           # yield the reservation (a parked mission isn't holding it)
                order.append(mid)
                remaining.discard(mid)
        self.store.append("BatchScheduled", "scheduler",
                          {"order": order, "unscheduled": sorted(remaining), "pools": pools})
        return {"order": order, "unscheduled": sorted(remaining), "final_pools": sched.snapshot()}

    def governance_console(self) -> dict:
        """The Agentic-OS control surface: per-mission policy version + grants + state, the global
        approval queue, live recommendations, and every auditable intervention."""
        missions = []
        for r in self.missions():
            mid = r["id"]
            m = self._missions.get(mid)
            missions.append({**r, "policy_version": self.governance.policy_version(mid),
                             "grants": list(m.policy_refs) if m else [],
                             "budget_usd": m.budget.usd if m else None,
                             "interventions": self.governance.interventions(mid)})
        return {"missions": missions, "approval_queue": self.inbox(),
                "recommendations": [to_jsonable(r) for r in self.learners.recommendations()]}

    def _node_decisions(self, mission_id: str) -> dict:
        """Per-node EXPLAIN — the decision transparency the Context Runtime ships (redevops.io/explain):
        for each node, why this capability was chosen over alternatives, its calibrated confidence
        (learned success rate), cost, and the ranked candidates that could have satisfied the step."""
        plan = self._plans.get(mission_id)
        if not plan:
            return {}
        allcaps = list(self.registry.all())
        by_name = {c.name: c for c in allcaps}
        out: dict[str, dict] = {}
        for n in plan.graph.nodes:
            spec = by_name.get(n.capability)
            outcome = n.produces
            alts = sorted(
                [c for c in allcaps if outcome and outcome in (c.provides or []) and c.name != n.capability],
                key=lambda c: -getattr(c, "confidence", 0.0))
            cost = n.cost
            out[n.id] = {
                "capability": n.capability, "operator": n.operator, "produces": outcome,
                "confidence": round(spec.confidence, 3) if spec else None,
                "cost_usd": round(cost.usd, 4) if cost else 0.0,
                "latency_ms": cost.latency_ms if cost else 0,
                "estimated_value": spec.estimated_value if spec else None,
                "gated": bool(n.approval_required),
                "reason": (f"sole provider of `{outcome}`" if not alts
                           else f"top of {len(alts) + 1} candidates by calibrated confidence"),
                "alternatives": [{"capability": c.name, "operator": c.operator,
                                  "confidence": round(getattr(c, "confidence", 0.0), 3)} for c in alts[:4]],
            }
        return out

    def explain(self, mission_id: str) -> dict:
        """Mission-scope EXPLAIN over the world-state timeline: what ran, the resulting world
        state, what's waiting on a human, and what the loops have learned."""
        tl = self.repo.timeline(mission_id)
        steps = [e for e in tl if e["type"] in ("NodeSucceeded", "NodeParked", "NodeFailed",
                                                "NodeCompensated", "BeliefDisputed", "BeliefResolved",
                                                "VerificationFailed")]
        world = {k: (b.value if b else None) for k, b in self._world(mission_id).snapshot().items()}
        st = self.repo.state(mission_id)
        return {"mission_id": mission_id, "goal": self.repo.goal(mission_id),
                "state": st.value if st else None, "steps": steps, "world_state": world,
                "pending_human": self.repo.pending_human(mission_id),
                "evidence": self.evidence.ledger(mission_id),
                "decisions": self._node_decisions(mission_id),
                "learning": self.learning.policy(),
                "recommendations": [to_jsonable(r) for r in self.learners.recommendations()]}

    # ── restart / rehydrate ────────────────────────────────────────────────────
    def rehydrate(self, mission_id: str) -> Mission:
        """Rebuild a mission from the event log after a restart (fresh runtime, same EventStore):
        recompile the deterministic plan and reconstruct the Mission shell. `run()` then resumes
        from the folded execution state. This is the durability proof."""
        goal = self.repo.goal(mission_id)
        created = next(e for e in self.store.for_mission(mission_id) if e.type == "MissionCreated")
        m = Mission(goal=goal or "", template=created.payload.get("template"),
                    constraints=created.payload.get("constraints", []),
                    policy_refs=["*"])   # grants re-resolved via permissions plane in prod
        m.id = mission_id
        m.state = self.repo.state(mission_id) or MissionState.PLANNING
        self._missions[mission_id] = m
        intent = self.planner.plan(mission_id, m.goal, {"template": m.template})
        plan = compile_intent(m, intent, LocalContextRuntime(self._scoped(m)), revision=1, reason="rehydrate")
        self._plans[mission_id] = plan
        m.active_plan_id = plan.id
        return m

    # ── helpers ────────────────────────────────────────────────────────────────
    def _approved(self, mission_id: str) -> set[str]:
        """Approved node ids, minus any a mid-flight policy change invalidated (P10). A tightened
        policy forces the human to re-approve under the new grants — the old ApprovalGranted no
        longer counts."""
        granted, invalidated = set(), set()
        for e in self.store.for_mission(mission_id):
            if e.type == "ApprovalGranted":
                granted.add(e.payload["node_id"])
            elif e.type == "ApprovalInvalidated":
                invalidated.add(e.payload["node_id"])
        return granted - invalidated

    def _signature(self, plan: ExecutionPlan) -> str:
        return "->".join(n.capability for n in plan.graph.nodes)

    def _set_state(self, m: Mission, state: MissionState) -> None:
        if m.state == state and state != MissionState.PLANNING:
            return
        m.state = state
        m.updated_at = now()
        self.store.append("MissionStateChanged", m.id, {"state": state.value})

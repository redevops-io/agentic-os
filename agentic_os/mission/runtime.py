"""Mission Runtime — the kernel state machine ("what's running now?").

Ties the layers together over the event-sourced world-state blackboard:

    plan (planner -> intent -> compiler -> graph) -> simulate (gate on budget)
      -> run: scheduler picks ready nodes -> executor runs them -> world state updated
      -> human gates park the mission (resume on approve) -> failures compensate (sagas)
      -> outcome event -> reflection -> learning.

`run()` is RESUMABLE: all execution state lives in the event log, so a fresh runtime built on
the same EventStore can `rehydrate()` and continue exactly where a crash left off. That is the
durability guarantee — the in-process stand-in for Dagster.

`rehydrate()` is EXACT REPLAY (v0.2.x): it recompiles the deterministic plan pinned to the mission's
original evidence identity and VERIFIES it reproduces the sealed plan fingerprint + ContextEpoch,
failing closed (`ReplayError`) on drift — it reconstructs/verifies the decision path, it does not
execute. To plan against *current* evidence instead, use the explicit `re_evaluate()`.
"""
from __future__ import annotations

import copy
import os
from typing import Any

from .approval import is_approval_key, strip_approval_keys
from . import belief, cost as costmod
from .compiler import compile_intent, CompileError
from .compensation import declared_undo, coerce as coerce_compensation, fold_compensation, summarize as _summ_comp, validate_plan_compensation
from .context import LocalContextRuntime, ContextIntent, CHECK_POLICY, RESOLVE_BELIEF
from .lifecycle import LifecycleRegistry, MissionStarted, GateReached, MissionFinished
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
from .verify import CompositeVerifier, is_staged, needs_verification, result_failed
from .types import (
    Mission, MissionState, ExecutionPlan, HumanTask, MissionOutcomeEvent, NodeState,
    VerificationDecision, Budget, new_id, now, to_jsonable,
)
from .context_view import epoch_from_refs, plan_fingerprint

_TERMINAL = {NodeState.DONE, NodeState.SKIPPED, NodeState.COMPENSATED}
#: DEFAULT business value (USD) per mission kind — the fallback only. The effective value is resolved by
#: :func:`business_value_for` from, in order: the runtime's ``business_values`` map, the mission's own
#: ``inputs["business_value_usd"]``, a registered app's ``OutcomeKind.value_usd`` (matching ``key``), the
#: ``$AGENTIC_OS_BUSINESS_VALUE`` JSON config, and only then these constants.
_BUSINESS_VALUE = {"onboarding": 100.0, "invoice_recovery": 250.0}


def business_value_for(kind: str, mission=None, *, overrides=None, registry=None) -> float:
    """Realized business value (USD) for a successfully completed mission of ``kind``. Metadata wins over
    the hard-coded defaults; see :data:`_BUSINESS_VALUE` for the resolution order."""
    import json as _json
    import os as _os2

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    if overrides and kind in overrides and _num(overrides[kind]) is not None:
        return _num(overrides[kind])
    inputs = getattr(mission, "inputs", None) or {}
    if _num(inputs.get("business_value_usd")) is not None:
        return _num(inputs["business_value_usd"])
    try:                                   # app_kit OutcomeKind metadata (value_usd, when an app declares it)
        from agentic_os.app_kit.boot import default_registry
        for app in (registry if registry is not None else default_registry()):
            for ok in app.manifest.outcome_kinds:
                v = _num(getattr(ok, "value_usd", None))
                if ok.key == kind and v is not None:
                    return v
    except Exception:                      # metadata is best-effort; never block outcome emission
        pass
    raw = _os2.environ.get("AGENTIC_OS_BUSINESS_VALUE", "").strip()
    if raw:
        try:
            cfg = _json.loads(raw)
            if isinstance(cfg, dict) and _num(cfg.get(kind)) is not None:
                return _num(cfg[kind])
        except ValueError:
            pass
    return _BUSINESS_VALUE.get(kind, 0.0)


class ReplayError(Exception):
    """Exact replay could not reproduce the sealed decision path — the recompiled plan's fingerprint or
    the rebuilt ContextEpoch does not match what was sealed at execution time (e.g. templates/registry
    drifted). Fail closed: replay must reproduce the original, or say it cannot. Re-planning against
    changed inputs is the separate, explicit ``re_evaluate`` operation, never a silent fallback here."""


def _evidence_refs(evidence: Any) -> list[str]:
    """Extract stable evidence-ref strings from a VerifiedIntent's evidence (or a plain list). Each
    item may be a DecisionEvidence (``field``/``source_ref``), an EvidenceRef (``pin()``), or a dict —
    duck-typed so Mission stays decoupled from runtime_contracts. Empty/opaque items are skipped."""
    refs: list[str] = []
    for e in evidence or ():
        pin = getattr(e, "pin", None)             # EvidenceRef → its content-addressed pin
        if callable(pin):
            refs.append(pin()); continue
        if isinstance(e, dict):
            r = e.get("source_ref") or e.get("ref") or e.get("content_hash") or ""
            fld = e.get("field") or ""
        else:
            r = getattr(e, "source_ref", "") or getattr(e, "content_hash", "") or ""
            fld = getattr(e, "field", "") or ""
        if r:
            refs.append(f"{fld}:{r}" if fld else str(r))
    return refs


def _intent_identity(verified_intent: Any) -> dict[str, Any]:
    """Duck-type a sealed VerifiedIntent into the identity a mission records: its ``content_hash`` and
    the evidence refs it consumed. Accepts a runtime_contracts.VerifiedIntent, a dict, or None; returns
    an empty dict when there is nothing to carry (so a goal-only mission is unchanged)."""
    if verified_intent is None:
        return {}
    if isinstance(verified_intent, dict):
        ch = verified_intent.get("content_hash", "") or ""
        evidence = verified_intent.get("evidence") or verified_intent.get("evidence_refs") or ()
        produced_by = verified_intent.get("produced_by", "") or ""
    else:
        ch = getattr(verified_intent, "content_hash", "") or ""
        evidence = getattr(verified_intent, "evidence", ()) or ()
        produced_by = getattr(verified_intent, "produced_by", "") or ""
    out: dict[str, Any] = {}
    if ch:
        out["intent_content_hash"] = ch
    refs = _evidence_refs(evidence)
    if refs:
        out["evidence_refs"] = refs
    if produced_by:
        out["intent_produced_by"] = produced_by
    return out


class ReplayDivergence(RuntimeError):
    """A restart rebuilt a different program and claimed the original's id.

    `rehydrate` recomputes the graph rather than storing it, which is right —
    the graph is deterministic given the code. The code is the part that
    changes. Nothing compared the recomputed plan against the `signature`
    `PlanCreated` has recorded since the kernel was written, so a deploy
    between the run and the restart produced a mission that folded the
    original's execution events into a program the log never described.

    Raised rather than logged. A caller that genuinely wants the new program
    should plan a new mission, which is what a new program is.
    """


class UnrecoverableAuthority(RuntimeError):
    """A log cannot say what a mission was permitted to do.

    Raised rather than defaulted. The default it replaces was a wildcard, and a
    wildcard is not a conservative guess — it is the most permissive answer
    available, applied precisely where nobody recorded one.
    """


#: keys the runtime owns in a node's resolved inputs; mission inputs can never set them.
_RESERVED_INPUT_KEYS = frozenset({"_goal", "_mission", "_node"})


def compensation_summary(events) -> dict:
    """Verified / unverified / failed compensations folded from the log (replay-safe)."""
    return _summ_comp(fold_compensation(events))


class MissionRuntime:
    #: Capability marker: this runtime invokes the ``_pre_exec_ok`` node-admission hook (and ``_meter``)
    #: on every dispatch. An overlay that enforces governance by overriding ``_pre_exec_ok`` checks this
    #: before trusting the seam — so running against a kernel too old to call it fails loudly instead of
    #: silently bypassing the gates. Bump only if the seam contract changes incompatibly.
    SUPPORTS_ADMISSION_SEAM = 1

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
        business_values=None,
        disambiguation_confidence: float = 0.75,
        max_concurrency: int = 1,
        secure_executor_for=None,
    ):
        self.registry = registry
        self.executor = executor
        # Opt-in per-mission secured executor. When wired (by the enterprise boot path), `secure_executor_for(m)`
        # returns a mission-scoped Executor carrying that mission's leased AuthorityContext + a boundary
        # SecurityMonitor + the credential broker. Built lazily per mission and cached by id (so `_saga`,
        # which runs outside `run()`, reuses the same one). None → the shared open executor, unchanged.
        self._secure_executor_for = secure_executor_for
        self._executors: dict[str, Executor] = {}
        # Execution concurrency: how many ready-wave nodes run at once. Default 1 = the historical serial
        # drain (byte-identical behaviour); >1 dispatches independent ready nodes concurrently (bounded,
        # per-operator limits honoured). Env AGENTIC_OS_MISSION_CONCURRENCY overrides. The plan fingerprint,
        # node identity, idempotency and event-sourced replay are unaffected — only wall-clock changes.
        import os as _os
        self.max_concurrency = max(1, int(_os.getenv("AGENTIC_OS_MISSION_CONCURRENCY") or max_concurrency))
        # below this fused-belief confidence (or on conflict) an input is disambiguated, not trusted
        self.disambiguation_confidence = disambiguation_confidence
        self.store = store or EventStore()
        self.planner = planner or TemplatePlanner()
        self.scheduler = scheduler or TopoScheduler()
        self.policy = policy or SchedulePolicy()
        self.learning = learning or LearningRouter(registry)
        # The EFFECTIVE per-wave concurrency is bounded by BOTH the executor pool (max_concurrency) and how
        # many the scheduler releases at once (policy.max_concurrency). Surfacing the min makes the two
        # ceilings observable so they can never silently diverge (e.g. pool 8 throttled to 4 by policy).
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
        self.lifecycle = LifecycleRegistry()   # mission lifecycle hooks (no-op until a contributor is installed)
        # P9b: three separate learners behind a promotion gate. They only RECOMMEND — nothing
        # mutates production routing until a policy promotes it (Observe→Recommend→Shadow→Promote).
        self.learners = learners or LearningStack()
        self.business_values = dict(business_values or {})   # per-kind USD overrides (see business_value_for)
        # P10: governance read-model over the auditable event log (policy versions + interventions).
        self.governance = GovernanceLog(self.store)
        self._plans: dict[str, ExecutionPlan] = {}     # plan_id/mission_id -> plan (recompilable)
        self._missions: dict[str, Mission] = {}
        #: Node ids whose approval predates capability scoping. Surfaced by
        #: `approval_scope` so replay evidence does not imply an old event
        #: recorded information it never held.
        self._legacy_approvals: set[str] = set()

    # ── world state ──────────────────────────────────────────────────────────
    def _world(self, mission_id: str) -> WorldState:
        return WorldState(mission_id, self.store, belief.fuse)

    # ── create + plan + simulate ──────────────────────────────────────────────
    def create_mission(self, goal: str, *, constraints: list[str] | None = None,
                        policy_refs: list[str] | None = None, budget: Budget | None = None,
                        template: str | None = None, verified_intent: Any = None,
                        policy: "MissionPolicy | None" = None,
                        principal: Any = None, tenant: str = "",
                        inputs: dict | None = None, intervention_id: str = "",
                        selected_capability: str = "", bind_selected: bool = False,
                        failure_policy: str = "") -> Mission:
        # v0.2.x Slice 1 — carry the Discovery seal across the boundary. ``verified_intent`` is an
        # optional sealed VerifiedIntent (duck-typed: a runtime_contracts.VerifiedIntent, or a dict, or
        # anything exposing ``content_hash``/``evidence``); its identity is recorded on the mission and
        # in MissionCreated so the exact evidence a decision used is resolvable at replay/EXPLAIN time.
        # mission-policy/v1 — an optional named MissionPolicy is the mission's single pinned authority.
        identity = _intent_identity(verified_intent)
        m = Mission(goal=goal, constraints=constraints or [], policy_refs=policy_refs or [],
                    policy=policy, budget=budget or Budget(), template=template,
                    world_state_id=new_id("world"),
                    principal=principal, tenant=tenant,
                    # a decision must not be able to smuggle an approval marker through its inputs
                    inputs=strip_approval_keys(dict(inputs or {})),
                    intervention_id=intervention_id or "",
                    failure_policy=(failure_policy or "").upper(),
                    selected_capability=selected_capability or "",
                    bind_selected=bool(bind_selected and selected_capability),
                    intent_content_hash=identity.get("intent_content_hash", ""),
                    evidence_refs=identity.get("evidence_refs", []))
        self._missions[m.id] = m
        # `policy_refs` is part of the record, not only of the object. Without
        # it the log says what happened and cannot say why it was allowed, and
        # `rehydrate` had no choice but to invent an authority on restart.
        self.store.append("MissionCreated", m.id,
                          {"goal": goal, "template": template, "constraints": m.constraints,
                           "policy_refs": list(m.policy_refs or []),
                           # Carry the delegating identity + which argument keys were supplied (keys, not
                           # values, so sensitive inputs don't land in the ledger) so a delegated mission
                           # records who asked and what it was handed, instead of only a goal string.
                           "principal": getattr(m.principal, "id", m.principal) if m.principal else None,
                           "tenant": m.tenant or None,
                           # the Decision↔Mission link, mission side (the InterventionRecord holds the other)
                           "intervention_id": m.intervention_id or None,
                           "input_keys": sorted(m.inputs.keys()) if m.inputs else [],
                           # Event-sourced delivery: only a mission that names its selected capability records
                           # the (approval-stripped) input VALUES, because replay must re-deliver them.
                           **self._selected_record(m),
                           **({"failure_policy": m.failure_policy} if m.failure_policy else {}),
                           # mission-policy/v1 — pin the named policy identity onto mission creation
                           "policy": (m.policy.ref if m.policy else None),
                           "policy_digest": (m.policy.digest() if m.policy else None),
                           **identity})
        self.lifecycle.dispatch(MissionStarted(mission_id=m.id, goal=goal, template=template))
        try:
            self._plan_and_gate(m)
        except CompileError as e:
            # stage-1 scoping left no permitted way to achieve an outcome — fail closed, don't crash
            self._set_state(m, MissionState.FAILED)
            self.store.append("MissionBlocked", m.id, {"reason": "no_permitted_plan", "detail": str(e)})
        return m

    @staticmethod
    def _inputs_digest(inputs: dict) -> str:
        """sha256 of the canonical JSON of the approval-stripped inputs (values never leave memory)."""
        import hashlib
        from .approval import canonical
        return hashlib.sha256(canonical(strip_approval_keys(dict(inputs or {})))).hexdigest()

    def _selected_record(self, m: Mission) -> dict:
        """What ``MissionCreated`` records about a launcher-selected capability and its inputs.

        PRIVACY: the ledger is append-only and hash-chained, and decision inputs may hold customer data
        (an erasure request carries the subject's email). By default only the input KEYS and a SHA-256
        digest are recorded, never values; rehydrate must be re-supplied the values and verifies them
        against the digest. Setting ``AGENTIC_OS_RECORD_MISSION_INPUT_VALUES=1`` records the values in the
        immutable ledger so replay is self-contained; that is a deliberate privacy trade-off (the values
        cannot be erased later and are visible to anything that projects the ledger), so enable it only
        where inputs are non-personal or the deployment accepts that."""
        if not m.selected_capability:
            return {}
        rec = {"selected_capability": m.selected_capability, "bind_selected": m.bind_selected,
               "inputs_sha256": self._inputs_digest(m.inputs)}
        if os.environ.get("AGENTIC_OS_RECORD_MISSION_INPUT_VALUES", "").strip().lower() in ("1", "true", "yes"):
            rec["inputs"] = to_jsonable(m.inputs)
        return rec

    def cancel_mission(self, mission_id: str, reason: str = "") -> Mission:
        """Cancel a mission that has not executed anything (e.g. a launch the launcher refused after
        inspecting the compiled plan). Records ``MissionCancelled`` so the refusal is on the ledger."""
        m = self._missions[mission_id]
        self._set_state(m, MissionState.CANCELLED)
        self.store.append("MissionCancelled", m.id, {"reason": reason})
        return m

    def create_mission_from_intent(self, intent, *, policy_refs: list[str] | None = None,
                                   budget: Budget | None = None) -> Mission:
        """The Discovery-fed entry point: a mission from a sealed `VerifiedIntent`.

        The difference from `create_mission` is not the argument type. It is
        that nothing below this call can read the user's sentence, because the
        artifact does not contain it — `VerifiedIntent` carries `utterance_ref`
        and never the utterance. The invariant stops being a rule anyone has to
        remember and becomes a property of what was passed in.

        `goal` is still set, because `Mission` requires one and operators log
        it. It is a label derived from the objective, and it is not consulted:
        `IntentPlanner` accepts the parameter and ignores it, and a test
        corrupts it to prove that.
        """
        from .from_intent import IntentPlanner, check_executable, mission_record, template_for

        # Refuse before anything is recorded. A mission created and then blocked
        # leaves a log entry for a request that was never admissible, and the
        # next reader has to work out which blocked missions were real.
        check_executable(intent)
        template = template_for(intent)

        m = Mission(goal=f"objective:{intent.objective}", constraints=[],
                    policy_refs=policy_refs or [], budget=budget or Budget(),
                    template=template, world_state_id=new_id("world"))
        self._missions[m.id] = m
        self.store.append("MissionCreated", m.id,
                          {"goal": m.goal, "template": template,
                           "constraints": m.constraints,
                           "policy_refs": list(m.policy_refs or []),
                           **mission_record(intent)})
        self.lifecycle.dispatch(MissionStarted(mission_id=m.id, goal=m.goal,
                                               template=template))
        try:
            self._plan_and_gate(m, planner=IntentPlanner(),
                                context={"verified_intent": intent})
        except CompileError as e:
            self._set_state(m, MissionState.FAILED)
            self.store.append("MissionBlocked", m.id,
                              {"reason": "no_permitted_plan", "detail": str(e)})
        return m

    def _scoped(self, m: Mission) -> PolicyScopedRegistry:
        """Stage-1 policy scoping: the planner/compiler/simulator only ever see the capabilities
        this mission's principal is permitted to use."""
        return PolicyScopedRegistry(self.registry, m.policy_refs)

    def _plan_and_gate(self, m: Mission, revision: int = 1, reason: str = "initial",
                       planner=None, context: dict | None = None) -> None:
        scoped = self._scoped(m)
        if planner is None and m.bind_selected:
            from .planner import ExplicitCapabilityPlanner
            planner, context = ExplicitCapabilityPlanner(scoped), {"capability": m.selected_capability,
                                                                  **(context or {})}
        planner = planner or self.planner
        intent = planner.plan(m.id, m.goal, {"template": m.template, **(context or {})})
        # P9a: active plan selection — generate bounded candidates, policy-prune, simulate, score,
        # select. The default (rank-best) plan is kept unless a candidate clearly wins.
        selector = ActivePlanner(scoped, self.policy, self.learning)
        plan, candidates = selector.select(m, intent, revision=revision, reason=reason)
        plan, promoted_choice = self._apply_promoted_choice(m, plan, candidates)
        plan.projection = plan.projection or simulate(plan, m, scoped, self.policy)
        self._plans[m.id] = plan
        m.active_plan_id = plan.id
        # v0.2.x Slice 2 — bind the ContextEpoch (the evidence view this plan was made against) and the
        # plan fingerprint (its structural identity), and persist both so a restart can EXACT-REPLAY:
        # reproduce this same plan against this same pinned evidence, or explicitly re-evaluate instead.
        sig = self._signature(plan)
        epoch = epoch_from_refs(m.evidence_refs, pins=[m.intent_content_hash] if m.intent_content_hash else [])
        m.context_epoch_id = epoch.id
        # Fingerprint the deterministic structural identity (the capability signature); intent_id is
        # provenance and may be freshly minted per compile, so it is deliberately excluded.
        fp = plan_fingerprint(sig, security=self._security_envelope(m))
        self.store.append("PlanCreated", m.id,
                          {"plan_id": plan.id, "revision": revision, "reason": reason,
                           "signature": sig, "projection": plan.projection,
                           # The template the planner *resolved*, not the one the caller asked for —
                           # so a restart doesn't re-derive it (TemplatePlanner would keyword-match the
                           # goal prose again).
                           "template": getattr(intent, "template", None) or m.template,
                           "plan_fingerprint": fp, "context_epoch_id": epoch.id,
                           "context_epoch_refs": list(epoch.derived_from),
                           "context_epoch_pins": list(epoch.pins)})
        if len(candidates) > 1:
            self.store.append("PlanSelected", m.id,
                              {"plan_id": plan.id, "considered": len(candidates),
                               "candidates": [{"label": c.label, "score": round(c.score, 3),
                                               "expected_usd": c.projection.expected_usd,
                                               "expected_success": c.projection.expected_success,
                                               "within_budget": c.projection.within_budget,
                                               "selected": c.selected} for c in candidates],
                               "promoted_choice": promoted_choice})
        self.store.append("PlanActivated", m.id, {"plan_id": plan.id})
        comp_errors = validate_plan_compensation(plan.graph.nodes, self.registry.get)
        if comp_errors:    # a node declares an undo the registry does not: refuse the plan, run nothing
            self._set_state(m, MissionState.FAILED)
            self.store.append("MissionBlocked", m.id, {"reason": "compensation_undeclared", "notes": comp_errors})
        elif not plan.projection.within_budget:
            self._set_state(m, MissionState.FAILED)
            self.store.append("MissionBlocked", m.id,
                              {"reason": "over_budget", "notes": plan.projection.notes})
        else:
            self._set_state(m, MissionState.PLANNING)

    def _apply_promoted_choice(self, m: Mission, plan: ExecutionPlan, candidates) -> tuple:
        """The ONE reader of ``LearningStack.promoted_choice`` on the planning path. The learners only
        RECOMMEND (shadow); once governance has PROMOTED a plan-policy choice (mission kind -> plan
        signature) or a routing choice (outcome -> capability), the already policy-pruned, simulated and
        within-budget candidate that embodies it is preferred over the rank-best default. Anything not
        promoted, not among the admissible candidates, or over budget changes nothing (shadow-only).
        Returns (plan, description-or-None)."""
        kind = m.template or "mission"
        wanted_sig = self.learners.promoted_choice("plan_policy", kind)
        pick, why = None, None
        for c in candidates:
            if c.plan is plan or not c.projection.within_budget:
                continue
            if wanted_sig and self._signature(c.plan) == wanted_sig:
                pick, why = c, f"plan_policy/{kind}={wanted_sig}"
                break
            if len(c.prefer) == 1:
                outcome, cap = next(iter(c.prefer.items()))
                if self.learners.promoted_choice("routing", outcome) == cap:
                    pick, why = c, f"routing/{outcome}={cap}"
                    break
        if pick is None:
            return plan, None
        for c in candidates:
            c.selected = c is pick
        pick.plan.projection = pick.projection
        return pick.plan, why

    # ── run loop (resumable) ──────────────────────────────────────────────────
    def _executor_for(self, m: Mission) -> Executor:
        """The executor a mission runs on: its cached per-mission secured executor when the security planes
        are wired, else the shared open executor. Cached by mission id so `run()` and `_saga()` agree."""
        if self._secure_executor_for is None:
            ex = self.executor
        else:
            ex = self._executors.get(m.id)
            if ex is None:
                ex = self._secure_executor_for(m) or self.executor
                self._executors[m.id] = ex
        if hasattr(ex, "evidence_probe"):
            cur = ex.evidence_probe
            if cur is None:
                ex.evidence_probe = self._compensation_evidence   # durable, restart-safe evidence
                ex.undo_registry = self._registered_undo
                ex.compensation_requires_approval = self._compensation_requires_approval
            elif getattr(cur, "__self__", None) is not self:
                raise RuntimeError("executor is already bound to another runtime's event log for "
                                   "compensation evidence; use one executor per runtime")
        return ex

    def _registered_undo(self, capability: str) -> "str | None":
        spec = self.registry.get(capability)
        return declared_undo(spec) if spec is not None else None

    def _compensation_requires_approval(self, capability: str) -> bool:
        spec = self.registry.get(capability)
        cs = coerce_compensation(getattr(spec, "compensation", None)) if spec is not None else None
        return bool(cs and cs.requires_approval)

    def _compensation_evidence(self, mission_id: str, node_id: str) -> dict:
        """Compensation authorization evidence, folded from the durable event log (never in-memory state).
        ``approved`` = an ApprovalGranted for the node that had not been ApprovalInvalidated at the moment
        its forward execution succeeded. An invalidation AFTER success does not revoke it (the effect ran
        under a valid approval; undoing it is not a widening)."""
        ev: dict = {"executed": False, "capability": None, "approved": False, "compensated_result": None}
        granted, cap_ok = False, None
        for e in self.store.for_mission(mission_id):
            if e.payload.get("node_id") != node_id:
                continue
            if e.type == "ApprovalGranted":
                granted, cap_ok = True, e.payload.get("capability")
            elif e.type == "ApprovalInvalidated":
                granted = False
            elif e.type == "NodeSucceeded":
                cap = e.payload.get("capability")
                res = e.payload.get("result")
                # A staged / failure-shaped / executed=False result that reached NodeSucceeded (e.g. an
                # approval marker let a staged preview through) is NOT proof the effect happened.
                ok = Executor._executed_ok(res)
                ev.update(executed=ok, capability=cap, result=res if ok else None,
                          approved=bool(granted and (cap_ok is None or cap_ok == cap)))
                ev["compensated_result"] = None
            elif e.type == "NodeCompensated":
                ev["compensated_result"] = e.payload.get("result") or {"compensated": True}
        return ev

    @property
    def effective_max_concurrency(self) -> int:
        """The concurrency limit that actually binds — min of the executor pool and the scheduler's
        per-wave release cap. Report this in telemetry/EXPLAIN so the two ceilings can never silently
        diverge (a pool of 8 quietly throttled to 4 by policy is visible, not hidden)."""
        return max(1, min(self.max_concurrency, self.policy.max_concurrency))

    def scheduler_config(self) -> dict:
        """The effective scheduler configuration, recorded at startup and available for EXPLAIN — so a
        runtime that is silently serial (pool defaulted to 1, or a policy cap throttling the pool) is
        VISIBLE, never a guess. This is the guard against another 'implemented but effectively disabled'
        regression: requested vs effective concurrency, the deciding ceiling, and which capabilities carry
        their own conflict semantics are all on the record."""
        pool = self.max_concurrency
        policy_cap = self.policy.max_concurrency
        effective = self.effective_max_concurrency
        keyed = [c.name for c in self.registry.all()
                 if getattr(c, "concurrency_key", "") or getattr(c, "resource_keys", [])
                 or getattr(c, "max_parallelism", None) is not None
                 or getattr(c, "concurrency_mode", "")]
        return {
            "scheduler": type(self.scheduler).__name__,
            "requested_concurrency": pool,           # the executor pool the SDK/env asked for
            "policy_max_concurrency": policy_cap,     # the scheduler's per-wave release cap
            "effective_max_concurrency": effective,   # min(pool, policy) — the limit that actually binds
            "bound_by": ("executor_pool" if pool <= policy_cap else "schedule_policy"),
            "scheduler_policy": ("safe_parallel" if effective > 1 else "serial"),
            "per_operator_limit": dict(self.policy.per_operator_limit),
            "capabilities_with_conflict_semantics": sorted(keyed),
        }

    def run(self, mission_id: str) -> Mission:
        m = self._missions[mission_id]
        if m.state in (MissionState.FAILED, MissionState.COMPENSATION_FAILED):      # blocked in simulation / needs a human
            return m
        plan = self._plans[mission_id]
        graph = plan.graph
        world = self._world(mission_id)
        self._set_state(m, MissionState.RUNNING)
        # Record the effective scheduler config ONCE per mission (startup telemetry). If this says
        # scheduler_policy=serial when you expected parallel, the ceilings are the reason — on the record.
        if not any(e.get("type") == "SchedulerConfigured" for e in self.repo.timeline(mission_id)):
            self.store.append("SchedulerConfigured", m.id, self.scheduler_config())

        while True:
            status = self.repo.node_status(mission_id)
            done = {nid for nid, st in status.items() if st in _TERMINAL}
            approved = self._approved(mission_id, plan)

            if all(n.id in done for n in graph.nodes):
                return self._succeed(m, plan, world)

            batch = self.scheduler.ready(graph, done, running=set(), policy=self.policy)
            batch = [n for n in batch if n.id not in done and status.get(n.id) != NodeState.FAILED]
            self._emit_wave_telemetry(m, graph, done, batch)
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
                # mission-policy/v1 — every policy decision is a ledger event (the Decision Ledger),
                # pinning the exact evaluated policy identity onto the mission history for replay.
                if getattr(decision, "policy_digest", ""):
                    self.store.append("PolicyEvaluated", m.id,
                                      {"node_id": n.id, "capability": n.capability,
                                       "policy": decision.policy_ref, "policy_digest": decision.policy_digest,
                                       "effect": decision.explain.get("effect", "allow"),
                                       "matched_rule": decision.explain.get("matched_rule", "")})
                if getattr(decision, "denied", False):    # a policy DENY rule — hard block, no human gate
                    self._block_denied(m, n, decision)
                    return m
                (gated if decision.required else runnable).append((n, decision))

            # Execute the ready wave: independent nodes run concurrently (bounded) when max_concurrency>1,
            # serially when ==1; results commit in deterministic node order either way.
            outcome = self._execute_wave(m, plan, world, [n for n, _ in runnable])
            if outcome is False or outcome == "parked":   # a node failed/compensated, or an input needs disambiguation
                return m

            if gated:
                node, decision = gated[0]
                self._park(m, node, decision)
                return m
            if not runnable:
                self._set_state(m, MissionState.FAILED)
                return m

    def _emit_wave_telemetry(self, m: Mission, graph, done: set[str], batch: list) -> None:
        """Make "why was this wave (not) parallel?" auditable (plan §14/§22). Records, per wave: the
        scheduler, the effective ceiling, how many nodes were eligible, how many were released to run
        concurrently (peak_parallel), and — for any held back — the resource/limit reason. Best-effort:
        a scheduler without `explain` (or any error) simply skips telemetry, never blocks the run.

        Only runs when safe-parallel is actually enabled (effective concurrency > 1). In serial mode there
        is no parallelism decision to explain — every node is held by the concurrency cap of 1, not by a
        resource conflict — so the per-wave `explain()` would be pure overhead on the default path (it
        measurably slowed the serial orchestration hot path). The one-shot SchedulerConfigured startup
        record still fires in every mode, so serial runs remain fully observable at zero per-wave cost."""
        if self.effective_max_concurrency <= 1:
            return
        explain = getattr(self.scheduler, "explain", None)
        if explain is None:
            return
        try:
            rows = explain(graph, done, running=set(), policy=self.policy)
        except Exception:  # noqa: BLE001 — telemetry must never break execution
            return
        if not rows:
            return
        serialized = [r for r in rows if r.get("decision") == "serialized"]
        self.store.append("WaveScheduled", m.id, {
            "runtime_scheduler": type(self.scheduler).__name__,
            "max_parallelism": self.effective_max_concurrency,
            "eligible_nodes": len(rows),
            "peak_parallel_nodes": len(batch),
            "serialized_nodes": [r["node"] for r in serialized],
            "serialization_reason": {r["node"]: r["reason"] for r in serialized},
            "explain": rows,
        })

    def _pre_exec_ok(self, m: Mission, plan: ExecutionPlan, node) -> bool:
        """Node-admission seam, evaluated BEFORE dispatch (no operator has run, nothing to compensate).

        The open-core default admits every node (``return True``), so the kernel's behaviour is unchanged.
        An overlay (the enterprise runtime) overrides this to enforce deny-by-default gates at the syscall
        boundary — identity/authz, resource-tenancy, metering budget — and, when it refuses a node, to record
        its own governance event(s) and call ``self._fail(m, plan, reason=...)`` before returning False, so a
        denied node fails the mission fail-closed. It runs ahead of ``_resolve_inputs`` in BOTH the serial and
        concurrent paths, so a refusal costs no network and no partial side effect."""
        return True

    def _inputs_unavailable(self, m: Mission, plan: ExecutionPlan, node) -> bool:
        """True (and the mission is failed with a clear reason) when the selected capability's node would run
        without the scope its decision supplied: the mission was rehydrated from a ledger that records only an
        inputs digest and the caller did not re-supply the values. Never run with partial scope."""
        if m.inputs_unavailable and node.capability == m.selected_capability:
            reason = (f"inputs_unavailable: {node.capability} needs the decision inputs "
                      "(not stored in the ledger); rehydrate(mission_id, inputs=...)")
            self.store.append("NodeFailed", m.id, {"node_id": node.id, "capability": node.capability,
                                                   "reason": "inputs_unavailable", "error": reason})
            self._fail(m, plan, reason=reason)
            return True
        return False

    def _meter(self, m: Mission, node, outcome: str) -> None:
        """Per-node usage seam, called once for every node that reached dispatch — ``outcome`` is ``"ok"`` on
        a committed result and ``"error"`` on a failed one. The open-core default is a no-op; an overlay
        overrides it to record a tenant-scoped usage/metering event. Placed so the serial and concurrent
        paths meter identically."""

    def _execute(self, m: Mission, plan: ExecutionPlan, world: WorldState, node) -> bool:
        """Serial node execution (the ``max_concurrency == 1`` route) — admit, resolve inputs, invoke the
        operator, apply the result. Behaviour is byte-identical to the historical path (``_pre_exec_ok``
        admits every node by default)."""
        if self._inputs_unavailable(m, plan, node):
            return False
        if not self._pre_exec_ok(m, plan, node):
            return False
        inputs = self._resolve_inputs(m, world, node)
        self.store.append("NodeDispatched", m.id, {"node_id": node.id, "capability": node.capability})
        try:
            result = self._executor_for(m).run(node, inputs)
        except Exception as e:  # noqa: BLE001
            return self._fail_node(m, plan, world, node, e)
        return self._finish(m, plan, world, node, result)

    def _fail_node(self, m: Mission, plan: ExecutionPlan, world: WorldState, node, e) -> bool:
        """The operator raised — record, learn, compensate, fail. Shared by the serial and concurrent
        paths so failure semantics are identical."""
        self.store.append("NodeFailed", m.id,
                          {"node_id": node.id, "capability": node.capability, "error": str(e)})
        self._meter(m, node, "error")
        self.learning.record_capability(node.capability, False)
        self._observe_routing(node, False)
        self._saga(m, plan, world)
        self._fail(m, plan, reason=f"{node.capability}: {e}")
        return False

    def _finish(self, m: Mission, plan: ExecutionPlan, world: WorldState, node, result) -> bool:
        """Apply a successful operator result: verify before it becomes authoritative, commit to world
        state, emit the success event. This mutates shared world/learning state, so the concurrent path
        calls it SERIALLY in deterministic node order after the parallel operator invocations have joined
        — the network wait overlaps, the state transition does not."""
        node.result = result
        # A capability that ran WITHOUT a runtime approval and answered "staged / awaiting approval" did NOT
        # execute: it must not be committed as a success (the hole where an ungated node's
        # ``pending_approval()`` preview was minted into world state). The ungoverned HTTP path still returns
        # that dict to its caller unchanged — only mission verification treats it as not-committed. The node
        # fails closed (``staged_not_executed``) rather than re-parking: the planner/policy chose NOT to gate
        # it, so there is no human task to resume, and failing keeps the gated flows (which park BEFORE
        # dispatch and then run with ``_approval``) untouched.
        if is_staged(result) and self._approval_marker(m.id, node.id) is None:
            self.store.append("NodeFailed", m.id,
                              {"node_id": node.id, "capability": node.capability,
                               "reason": "staged_not_executed",
                               "error": f"{node.capability} returned a staged result "
                                        f"({result.get('status')}) without a runtime approval; not executed"})
            self._meter(m, node, "error")
            self.learning.record_capability(node.capability, False)
            self._observe_routing(node, False)
            self._saga(m, plan, world)
            self._fail(m, plan, reason=f"{node.capability}: staged_not_executed")
            return False
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
                          float(obs.get("confidence", 1.0)),
                          source_type=obs.get("source_type", "prior"),
                          source_ref=obs.get("source_ref", ""))
        self.store.append("NodeSucceeded", m.id,
                          {"node_id": node.id, "capability": node.capability, "result": result})
        self._meter(m, node, "ok")              # one usage event per executed node (overlay hook; no-op by default)
        self.learning.record_capability(node.capability, True)
        self._observe_routing(node, True)
        return True

    def _observe_routing(self, node, ok: bool) -> None:
        """Routing learning (P9b): which provider serves an outcome best — shadow-only, never mutates
        production routing until a policy promotes it."""
        if node.produces:
            self.learners.routing.observe(node.produces, node.capability, ok)

    def _execute_wave(self, m: Mission, plan: ExecutionPlan, world: WorldState, nodes: list):
        """Run one ready wave. The nodes are independent (all deps already terminal), so their operator
        invocations may overlap. Returns True (all committed), False (a node failed/parked → mission
        stopped), or "parked" (a belief needs disambiguation before this wave runs).

        Concurrency is bounded by ``self.max_concurrency`` and per-operator limits (``policy
        .per_operator_limit``). max_concurrency==1 is the exact historical serial drain. Regardless of
        concurrency, results are COMMITTED serially in deterministic node order, so world state, events and
        the plan fingerprint are identical to a serial run — only the network waits overlap.
        """
        # Belief gate is a pre-execution check on independent inputs; park the mission if any node's input
        # is conflicting/uncertain (same as the serial path, evaluated up-front for the whole wave).
        for node in nodes:
            issue = self._belief_issue(node, world)
            if issue is not None:
                self._park_disambiguation(m, node, *issue)
                return "parked"

        if self.max_concurrency <= 1 or len(nodes) <= 1:
            for node in nodes:
                if self._execute(m, plan, world, node) is False:
                    return False
            return True

        # Concurrent path: resolve inputs from the current (stable) world snapshot and emit the dispatch
        # events in node order, then invoke operators concurrently, then commit results in node order.
        import concurrent.futures as _cf
        prepared = []
        for node in nodes:
            if self._inputs_unavailable(m, plan, node):
                return False
            if not self._pre_exec_ok(m, plan, node):     # admit (fail-closed) before any dispatch, like serial
                return False
            inputs = self._resolve_inputs(m, world, node)
            self.store.append("NodeDispatched", m.id, {"node_id": node.id, "capability": node.capability})
            prepared.append((node, inputs))
        sems = self._operator_semaphores([n.operator for n, _ in prepared])

        def _call(node, inputs):
            sem = sems.get(node.operator)
            if sem is None:
                return self._executor_for(m).run(node, inputs)
            with sem:                                    # honour per-operator in-flight limits
                return self._executor_for(m).run(node, inputs)

        results: dict[str, tuple[str, object]] = {}
        workers = min(self.max_concurrency, len(prepared))
        with _cf.ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_call, node, inputs): node.id for node, inputs in prepared}
            for fut in _cf.as_completed(futs):           # JOIN BARRIER — all invocations complete here
                nid = futs[fut]
                try:
                    results[nid] = ("ok", fut.result())
                except Exception as e:  # noqa: BLE001
                    results[nid] = ("err", e)

        for node, _ in prepared:                         # commit serially, deterministic order
            kind, payload = results[node.id]
            ok = (self._fail_node(m, plan, world, node, payload) if kind == "err"
                  else self._finish(m, plan, world, node, payload))
            if ok is False:                              # first failure/park stops the mission (fail-fast)
                return False
        return True

    def _operator_semaphores(self, operators) -> dict:
        """A bounding Semaphore per operator that declares a per_operator_limit — so a wave never issues
        more than the provider allows in flight, independent of the global concurrency cap."""
        import threading
        sems: dict[str, threading.Semaphore] = {}
        for op in set(operators):
            limit = self.policy.per_operator_limit.get(op)
            if limit:
                sems[op] = threading.Semaphore(limit)
        return sems

    def _resolve_inputs(self, m: Mission, world: WorldState, node) -> dict:
        resolved: dict[str, Any] = {"_goal": m.goal, "_mission": str(m.id)}
        for k, v in node.inputs.items():
            if k in _RESERVED_INPUT_KEYS:   # a node must never override runtime-reserved keys (token binding)
                import logging  # noqa: PLC0415
                logging.getLogger("agentic_os.runtime").warning(
                    "node %s tried to set reserved input %r; ignored", getattr(node, "id", "?"), k)
                continue
            if isinstance(v, dict) and "$from_world" in v:
                resolved[k] = world.get(v["$from_world"])
            else:
                resolved[k] = v
        # The approval signal delivered to a resumed handler as `_approval`: a truthy marker that a human
        # cleared THIS node — the handler's cue to EXECUTE the real side effect instead of re-staging it
        # (N3) — merged with any typed `edit` the approver supplied. Event-sourced (folded from
        # ApprovalGranted), so it survives replay. Absent on the ungoverned/pre-approval path, so a handler
        # invoked directly (e.g. /agent/run) still stages rather than acts.
        # Mission.inputs reach ONLY the node bound to the launcher-selected capability, never over explicit
        # node.inputs or runtime-reserved keys (approval keys were already stripped at mission creation).
        if m.selected_capability and node.capability == m.selected_capability:
            for k, v in (m.inputs or {}).items():
                if k in resolved or k in _RESERVED_INPUT_KEYS or is_approval_key(k):
                    continue
                resolved[k] = copy.deepcopy(strip_approval_keys(v))
        approval = self._approval_marker(m.id, node.id)
        if approval is not None:
            resolved["_approval"] = approval
            resolved["_node"] = node.id   # lets HTTPOperatorClient bind a signed approval token to this node
        return resolved

    def _approval_edit(self, mission_id: str, node_id: str) -> dict | None:
        """The human `edit` payload from the latest ApprovalGranted for this node (None if there was
        no approval, or it carried no edit)."""
        edit = None
        for e in self.store.for_mission(mission_id):
            if e.type == "ApprovalGranted" and e.payload.get("node_id") == node_id:
                edit = e.payload.get("edit")
        return edit

    def _approval_marker(self, mission_id: str, node_id: str) -> dict | None:
        """The `_approval` signal for a resumed node: a truthy marker that a human CLEARED this node,
        merged with any typed `edit` — so a gated handler executes the real side effect on approval (N3)
        rather than returning pending forever. ``None`` when the node was never approved."""
        granted = False
        edit = None
        for e in self.store.for_mission(mission_id):
            if e.type == "ApprovalGranted" and e.payload.get("node_id") == node_id:
                granted = True
                if e.payload.get("edit") is not None:
                    edit = e.payload.get("edit")
        if not granted:
            return None
        marker: dict = {"approved": True}
        if isinstance(edit, dict):
            marker.update(edit)
        elif edit is not None:
            marker["edit"] = edit
        return marker

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
        self.lifecycle.dispatch(GateReached(mission_id=m.id, node_id=node.id, capability=node.capability))
        self._set_state(m, MissionState.WAITING_HUMAN)

    def _block_denied(self, m, node, decision) -> None:
        """mission-policy/v1 — a DENY rule forbids this action outright. Unlike a gate, no human can
        approve it; the mission blocks with the policy's actionable reason (deny always wins)."""
        self.store.append("PolicyDenied", m.id,
                          {"node_id": node.id, "capability": node.capability,
                           "policy": decision.policy_ref, "policy_digest": decision.policy_digest,
                           "explain": decision.explain})
        m.outcome = {"blocked": "policy_denied", "node": node.id, "capability": node.capability,
                     "policy": decision.policy_ref, "policy_digest": decision.policy_digest,
                     "message": decision.explain.get("message", "")}
        self.lifecycle.dispatch(GateReached(mission_id=m.id, node_id=node.id, capability=node.capability))
        self._set_state(m, MissionState.FAILED)

    # ── belief-driven disambiguation (state estimation, Whitepaper v5 · P4) ──────
    def _belief_issue(self, node, world: WorldState):
        """A required $from_world input whose fused belief is conflicting or low-confidence.
        Observations are not facts — surface the disagreement instead of silently trusting one.
        MATERIAL fields only: a field the node declares cosmetic never blocks execution, so
        disagreement on non-material metadata does not gate (spec §3)."""
        cosmetic = set(getattr(node, "cosmetic_inputs", []) or [])
        for v in node.inputs.values():
            if isinstance(v, dict) and "$from_world" in v:
                key = v["$from_world"]
                if key in cosmetic:
                    continue                      # non-material — disagreement here is not a gate
                b = self.context.resolve(ContextIntent(kind=RESOLVE_BELIEF, world=world, key=key)).value
                if b is not None and (b.conflict or b.confidence < self.disambiguation_confidence):
                    return key, b
        return None

    @staticmethod
    def _evidence_lines(key: str, belief) -> list:
        """What the controller is shown, one line per reader.

        A reader that lost is still evidence: "the regex read `crossing` from
        span 12-27 and the model read `persistent` from the whole sentence" is
        decidable, and "two sources disagreed" is not.
        """
        lines = [f"key={key}", f"current={belief.value!r}",
                 f"confidence={belief.confidence}", f"conflict={belief.conflict}"]
        for de in getattr(belief, "evidence", ()) or ():
            ref = f" @{de.source_ref}" if getattr(de, "source_ref", "") else ""
            lines.append(
                f"read by {de.source_type}{ref}: {de.value!r} "
                f"(confidence {de.confidence})")
        if not getattr(belief, "evidence", ()):
            # Older observations carry no per-reader evidence. Say so rather
            # than showing a shorter list that looks like agreement.
            lines.append(f"sources={belief.sources} "
                         "(no per-reader evidence recorded for this belief)")
        return lines

    def _park_disambiguation(self, m: Mission, node, key: str, belief) -> None:
        node.status = NodeState.WAITING
        why = "conflicting sources" if belief.conflict else f"low confidence ({belief.confidence})"
        # The per-reader packet, not a summary of it. The gate exists so a
        # person decides which reading is right; handing them
        # "sources=['a','b']" tells them a disagreement happened and withholds
        # the only thing they need to settle it — what each reader actually
        # read, and from where. `_evidence_lines` keeps the old key/current
        # lines so existing consumers of `HumanTask.evidence` still parse.
        task = HumanTask(
            mission_id=m.id, node_id=node.id, assignee="data-steward",
            prompt=f"Resolve '{key}' before '{node.capability}' runs — {why}.",
            evidence=self._evidence_lines(key, belief),
            options=["resolve"],
        )
        self.store.append("NodeParked", m.id,
                          {"node_id": node.id, "capability": node.capability,
                           "human_task": {"id": task.id, "kind": "disambiguation", "key": key,
                                          "assignee": task.assignee, "prompt": task.prompt,
                                          "evidence": task.evidence, "options": task.options}})
        self.store.append("BeliefDisputed", m.id,
                          {"key": key, "value": belief.value, "sources": belief.sources,
                           "confidence": belief.confidence, "conflict": belief.conflict,
                           # the per-reader evidence the controller weighs — survives replay
                           "evidence": [to_jsonable(de) for de in belief.evidence]})
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
            # `source_type="human"` so the authoritative answer is not filed
            # under "prior" beside the readings it overruled. A resolution that
            # looks like a guess in the evidence list cannot be told from one
            # later, which defeats the record the gate exists to produce.
            self._world(mission_id).observe(
                key, value, source="human", confidence=100.0,
                source_type="human", source_ref=f"human-task:{pending.get('id', '')}")
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
            # The capability, not only the node. `_node_id` is derived from
            # (mission, revision, outcome) and does not contain the capability,
            # so an approval recorded as a bare node id also authorises any
            # OTHER capability later bound to the same outcome at the same
            # revision — and binding is a Context Runtime decision made per
            # compile, with `plan_select` choosing among candidates. A provider
            # swap is exactly when a human's yes should stop applying, because
            # what they approved is not what would run.
            self.store.append("ApprovalGranted", mission_id, {
                "node_id": node_id, "edit": edit,
                "capability": self._capability_of(mission_id, node_id)})
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
        """Explicit-only compensation (docs/COMPENSATION.md): see compensation_runner.run_saga."""
        from .compensation_runner import run_saga  # noqa: PLC0415
        run_saga(self, m, plan)

    def retry_compensation(self, mission_id: str) -> Mission:
        """Intervention for a COMPENSATION_FAILED mission: re-attempt only the compensations that failed
        (each under its own `{idempotency_key}:undo` key). Back to FAILED only if none remain failed."""
        m = self._missions[mission_id]
        if m.state is not MissionState.COMPENSATION_FAILED:
            return m
        self._saga(m, self._plans[mission_id], self._world(mission_id))
        if not (m.outcome or {}).get("compensation_incomplete"):
            self._set_state(m, MissionState.FAILED)
        return m

    def _fail(self, m: Mission, plan: ExecutionPlan, reason: str) -> None:
        from .compensation_runner import OUTCOME_KEYS  # noqa: PLC0415
        keep = {k: v for k, v in (m.outcome or {}).items() if k in OUTCOME_KEYS or k.startswith("compensation_")}
        # a failed compensation leaves a forward effect behind: attention state, never plain FAILED/"rolled back"
        self._set_state(m, MissionState.COMPENSATION_FAILED if keep.get("compensation_incomplete")
                        else MissionState.FAILED)
        m.outcome = {"success": False, "reason": reason, **keep}
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
            business_value=(business_value_for(kind, m, overrides=self.business_values)
                            if success else 0.0),
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
                                                "NodeCompensated", "NodeCompensationFailed", "CompensationRequested",
                                                "CompensationAuthorized", "CompensationExecuted", "CompensationVerified",
                                                "CompensationFailed", "BeliefDisputed", "BeliefResolved",
                                                "VerificationFailed")]
        world = {k: (b.value if b else None) for k, b in self._world(mission_id).snapshot().items()}
        st = self.repo.state(mission_id)
        comp_failed: dict[str, str] = {}
        for e in tl:                     # derived from the log, so it survives restart/rehydrate
            if e["type"] == "NodeCompensationFailed":
                comp_failed[e["payload"]["node_id"]] = e["payload"].get("reason", "")
            elif e["type"] == "NodeCompensated":
                comp_failed.pop(e["payload"]["node_id"], None)
        return {"mission_id": mission_id, "goal": self.repo.goal(mission_id),
                "compensation_incomplete": bool(comp_failed), "compensation_remaining": comp_failed,
                "compensation": compensation_summary(self.store.for_mission(mission_id)),
                "not_reversible": ((self._missions.get(mission_id).outcome or {}).get("not_reversible", [])
                                   if self._missions.get(mission_id) else []),
                "state": st.value if st else None, "steps": steps, "world_state": world,
                "pending_human": self.repo.pending_human(mission_id),
                "evidence": self.evidence.ledger(mission_id),
                "decisions": self._node_decisions(mission_id),
                "learning": self.learning.policy(),
                "recommendations": [to_jsonable(r) for r in self.learners.recommendations()]}

    # ── restart / rehydrate ────────────────────────────────────────────────────
    def rehydrate(self, mission_id: str, policy_refs: list[str] | None = None,
                  inputs: dict | None = None) -> Mission:
        """EXACT REPLAY — rebuild a mission from the event log after a restart and reproduce the SEALED
        decision path: recompile the deterministic plan pinned to the ORIGINAL evidence identity, verify
        it reproduces the sealed plan signature/fingerprint + ContextEpoch, and fail closed
        (``ReplayDivergence`` / ``ReplayError``) rather than silently substitute a divergent plan. This
        reconstructs/verifies the historical decision; it does NOT execute — ``run()`` resumes from the
        folded state under the normal gates, and ``re_evaluate`` is the explicit re-plan against current
        evidence.

        **Authority is restored, never invented.** This used to pass
        `policy_refs=["*"]` with a note that grants would be re-resolved in
        production, because `MissionCreated` did not record them — so replay
        did not reconstruct the mission, it reconstructed a strictly more
        permissive one. Since `compile_intent` fails closed on a missing grant,
        a mission that had been legitimately refused under a restrictive policy
        would compile and run on restart, and the event log would show it
        succeeding with no record of what changed.

        A log written before policy was recorded genuinely does not say what
        was permitted. Rehydrating it as `["*"]` fabricates an authority nobody
        granted, so it raises instead, and a caller holding the grants from the
        permissions plane may pass them explicitly.

        This is deliberately stricter than the approval grandfathering in
        `_approved`. Being lenient about a recorded fact is not the same as
        manufacturing an unrecorded one — one keeps a narrow permission alive,
        the other invents a broad one.
        """
        goal = self.repo.goal(mission_id)
        created = next(e for e in self.store.for_mission(mission_id) if e.type == "MissionCreated")
        recorded = created.payload.get("policy_refs")
        if policy_refs is None and recorded is None:
            raise UnrecoverableAuthority(
                f"{mission_id}: MissionCreated records no policy_refs, so what "
                "this mission was permitted to do cannot be reconstructed. "
                "Pass `policy_refs=` from the permissions plane, or accept that "
                "this log predates authority recording — but do not let it "
                "replay as a wildcard.")
        active = self._active_plan_record(mission_id)
        # The resolved template, when the log has one. Falling back to
        # `MissionCreated` keeps pre-existing logs working — and those are
        # exactly the logs where the template is the caller's request rather
        # than the planner's answer, so they re-derive it and this method's
        # signature check is what stands between that and a silent swap.
        template = (active or {}).get("template") or created.payload.get("template")

        sel = created.payload.get("selected_capability") or ""
        digest = created.payload.get("inputs_sha256") or ""
        recorded_inputs = created.payload.get("inputs")
        # Input VALUES are not in the ledger by default: they must be re-supplied and are verified against the
        # recorded digest (fail closed). Without them the selected node is not run (see _inputs_unavailable).
        if sel and inputs is not None:
            supplied = strip_approval_keys(dict(inputs))
            if digest and self._inputs_digest(supplied) != digest:
                raise ReplayError(f"{mission_id}: supplied inputs do not match the recorded inputs_sha256; "
                                  "refusing to replay with different inputs")
            restored = supplied
        elif sel and recorded_inputs is not None:
            restored = strip_approval_keys(dict(recorded_inputs))
        else:
            restored = {}
        unavailable = bool(sel and digest and inputs is None and recorded_inputs is None)
        m = Mission(goal=goal or "", template=template,
                    constraints=created.payload.get("constraints", []),
                    selected_capability=sel,
                    bind_selected=bool(sel and created.payload.get("bind_selected")),
                    inputs=restored, inputs_unavailable=unavailable,
                    intervention_id=created.payload.get("intervention_id") or "",
                    failure_policy=created.payload.get("failure_policy") or "",
                    policy_refs=list(policy_refs if policy_refs is not None else recorded),
                    # v0.2.x: replay carries the original evidence identity back, so the rehydrated
                    # mission resolves the SAME sealed intent + evidence it was created from.
                    intent_content_hash=created.payload.get("intent_content_hash", ""),
                    evidence_refs=list(created.payload.get("evidence_refs", [])))
        m.id = mission_id
        m.state = self.repo.state(mission_id) or MissionState.PLANNING
        self._missions[mission_id] = m

        # A mission created from a sealed intent replans from that intent, which
        # the log carries whole. Storing only `intent_hash` would prove the
        # intent had not changed and be unable to reconstruct it, sending replay
        # back to the goal string — the one input this path exists to not read.
        stored = created.payload.get("intent")
        if stored is not None:
            from runtime_contracts import intent_from_json

            from .from_intent import IntentPlanner

            planner, context = IntentPlanner(), {
                "verified_intent": intent_from_json(stored)}
        elif m.bind_selected:
            from .planner import ExplicitCapabilityPlanner
            planner, context = ExplicitCapabilityPlanner(self._scoped(m)), {"capability": m.selected_capability}
        else:
            planner, context = self.planner, {}

        intent = planner.plan(mission_id, m.goal, {"template": m.template, **context})
        plan = compile_intent(m, intent, LocalContextRuntime(self._scoped(m)), revision=1, reason="rehydrate")

        if active is not None:
            recorded_signature = active.get("signature")
            rebuilt = self._signature(plan)
            if recorded_signature is not None and rebuilt != recorded_signature:
                raise ReplayDivergence(
                    f"{mission_id}: the log describes {recorded_signature!r} "
                    f"and this build produces {rebuilt!r}. Replay would fold "
                    "the original's execution events into a different program "
                    "under its id. If the new program is the intended one, "
                    "plan a new mission — that is what a new program is.")

        self._plans[mission_id] = plan
        m.active_plan_id = plan.id

        # Verify exact replay reproduced the sealed plan + evidence identity (fail closed on drift).
        sealed = self._last_plan_meta(mission_id)
        if sealed:
            got_fp = plan_fingerprint(self._signature(plan), security=self._security_envelope(m))
            want_fp = sealed.get("plan_fingerprint", "")
            if want_fp and got_fp != want_fp:
                self.store.append("ReplayDivergence", mission_id,
                                  {"kind": "plan_fingerprint", "expected": want_fp, "got": got_fp})
                raise ReplayError(
                    f"replay could not reproduce the sealed plan for {mission_id}: "
                    f"fingerprint {got_fp} != {want_fp} (templates/registry drifted — use re_evaluate)")
            got_epoch = epoch_from_refs(
                m.evidence_refs, pins=[m.intent_content_hash] if m.intent_content_hash else []).id
            want_epoch = sealed.get("context_epoch_id", "")
            if want_epoch and got_epoch != want_epoch:
                self.store.append("ReplayDivergence", mission_id,
                                  {"kind": "context_epoch", "expected": want_epoch, "got": got_epoch})
                raise ReplayError(
                    f"replay could not reproduce the sealed ContextEpoch for {mission_id}: "
                    f"{got_epoch} != {want_epoch}")
            m.context_epoch_id = want_epoch or got_epoch
        m.outcome = self._outcome_from_log(m)
        return m

    def _outcome_from_log(self, m: Mission) -> dict | None:
        """A terminal mission's outcome, rebuilt from the log on rehydrate — the same shape `_succeed`/`_fail`
        set on the live run (the world is folded from the same ObservationWritten events). Without this a
        resumed process sees `state=succeeded` with `outcome=None` and has to re-derive results itself.
        Non-terminal missions keep `None` (they have no outcome yet)."""
        if m.state is MissionState.SUCCEEDED:
            return {"success": True, "world": {k: (b.value if b else None)
                                               for k, b in self._world(m.id).snapshot().items()}}
        if m.state in (MissionState.FAILED, MissionState.COMPENSATION_FAILED):
            why = next((e for e in reversed(self.store.for_mission(m.id))
                        if e.type in ("NodeFailed", "ApprovalRejected", "VerificationRejected", "MissionBlocked")),
                       None)
            reason = (why.payload.get("reason") or why.payload.get("error") or why.type) if why else "failed"
            from .compensation_runner import outcome_from_log  # noqa: PLC0415
            return {"success": False, "reason": reason, **outcome_from_log(self, m.id)}
        return None

    def _last_plan_meta(self, mission_id: str) -> dict | None:
        """The latest PlanCreated payload for a mission (the sealed plan identity to replay against)."""
        meta = None
        for e in self.store.for_mission(mission_id):
            if e.type == "PlanCreated":
                meta = e.payload
        return meta

    def re_evaluate(self, mission_id: str, *, verified_intent: Any = None,
                    cause: str = "re-evaluation") -> Mission:
        """EXPLICIT RE-EVALUATION — the deliberate counterpart to exact replay. Re-plan the mission
        against *current* evidence (a freshly sealed ``verified_intent`` if Discovery re-ran, else the
        mission's current evidence), producing a NEW revision that may differ from the sealed one. The
        difference is recorded explainably (``PlanReevaluated``: old/new plan fingerprint + ContextEpoch).

        Unlike ``rehydrate``, this is allowed to produce a different plan — it is not replay. It does not
        itself execute; the new plan runs through the normal ``run()`` gates."""
        m = self._missions.get(mission_id) or self.rehydrate(mission_id)
        before = self._last_plan_meta(mission_id) or {}
        old_fp, old_epoch = before.get("plan_fingerprint", ""), m.context_epoch_id
        # adopt the current evidence identity (re-sealed intent = Discovery re-ran against new evidence)
        if verified_intent is not None:
            identity = _intent_identity(verified_intent)
            m.intent_content_hash = identity.get("intent_content_hash", m.intent_content_hash)
            if "evidence_refs" in identity:
                m.evidence_refs = identity["evidence_refs"]
        rev = int(before.get("revision", 1)) + 1
        self._plan_and_gate(m, revision=rev, reason=f"re-evaluate: {cause}")
        after = self._last_plan_meta(mission_id) or {}
        new_fp, new_epoch = after.get("plan_fingerprint", ""), m.context_epoch_id
        self.store.append("PlanReevaluated", mission_id, {
            "cause": cause, "revision": rev,
            "old_plan_fingerprint": old_fp, "new_plan_fingerprint": new_fp,
            "old_context_epoch_id": old_epoch, "new_context_epoch_id": new_epoch,
            "plan_changed": bool(old_fp) and old_fp != new_fp,
            "evidence_changed": bool(old_epoch) and old_epoch != new_epoch,
        })
        return m

    def _active_plan_record(self, mission_id: str) -> dict | None:
        """The `PlanCreated` payload for the plan that was last activated.

        Not simply the last `PlanCreated`: a re-plan can create a candidate
        that is never activated, and comparing against a plan the mission never
        ran would refuse a replay that is perfectly faithful.
        """
        events = list(self.store.for_mission(mission_id))
        activated = [e for e in events if e.type == "PlanActivated"]
        if not activated:
            return None
        plan_id = activated[-1].payload.get("plan_id")
        for event in reversed(events):
            if event.type == "PlanCreated" and event.payload.get("plan_id") == plan_id:
                return event.payload
        return None

    # ── helpers ────────────────────────────────────────────────────────────────
    def _capability_of(self, mission_id: str, node_id: str) -> str:
        plan = self._plans.get(mission_id)
        if plan is None:
            return ""
        for node in plan.graph.nodes:
            if node.id == node_id:
                return node.capability
        return ""

    def _approved(self, mission_id: str, plan=None) -> set[str]:
        """Approved node ids, minus any that no longer apply.

        Three ways an approval stops counting, and only the first was checked:

        a mid-flight policy change invalidated it (P10) — a tightened policy
        forces re-approval under the new grants;

        the mission was re-planned to a new revision — node ids embed the
        revision, so consent given for the plan it replaced simply does not
        match;

        **the capability bound to the outcome changed.** This one was invisible
        because the approval recorded only a node id, which does not contain
        the capability. A human who approved `pay.invoice` satisfying `paid`
        was also approving whatever else got bound to `paid` afterwards.

        Approvals recorded before the capability was stored are grandfathered
        rather than refused. Failing closed on them would park missions that
        have already run and succeeded, which changes the outcome of history on
        replay — a worse fault than the one it would close, and one this
        project has a name for.
        """
        granted, invalidated = {}, set()
        for e in self.store.for_mission(mission_id):
            if e.type == "ApprovalGranted":
                granted[e.payload["node_id"]] = e.payload.get("capability")
            elif e.type == "ApprovalInvalidated":
                invalidated.add(e.payload["node_id"])

        current = {n.id: n.capability for n in plan.graph.nodes} if plan else {}
        still_applies = set()
        for node_id, approved_capability in granted.items():
            if approved_capability is None:
                # Recorded before the capability was stored. Kept, and marked:
                # an unscoped approval is not equivalent to a scoped one, and
                # replay evidence should not imply the old event contained
                # information it never held.
                self._legacy_approvals.add(node_id)
                still_applies.add(node_id)
                continue
            if node_id in current and current[node_id] != approved_capability:
                continue
            still_applies.add(node_id)
        return still_applies - invalidated

    LEGACY_UNSCOPED_APPROVAL = "LEGACY_UNSCOPED_APPROVAL"

    def approval_scope(self, mission_id: str, node_id: str) -> str:
        """How well an approval is bound — for replay evidence.

        `SCOPED` if it names the capability it approved, `LEGACY_UNSCOPED_APPROVAL`
        if it predates that, `NONE` if there is no approval. A reader of a
        replayed mission can then see which consents were bound to an
        invocation and which were only bound to a place.
        """
        for e in self.store.for_mission(mission_id):
            if e.type == "ApprovalGranted" and e.payload.get("node_id") == node_id:
                return ("SCOPED" if e.payload.get("capability")
                        else self.LEGACY_UNSCOPED_APPROVAL)
        return "NONE"

    def _signature(self, plan: ExecutionPlan) -> str:
        return "->".join(n.capability for n in plan.graph.nodes)

    def _security_envelope(self, m: Mission) -> str:
        """The mission's security posture folded into the plan fingerprint — but only when it opts into the
        security plane by attaching a ``MissionPolicy``. Returns "" otherwise (fingerprint unchanged, so
        existing sealed plans replay). When present it binds the pinned policy digest, the effective grants,
        and the tenant, so a revoked grant / changed policy cannot EXACT-REPLAY a stale sealed plan.
        (Model identity folds in with Slice-4 model-digest pinning.)"""
        if getattr(m, "policy", None) is None:
            return ""
        parts = [f"policy={m.policy.digest()}"]
        if m.policy_refs:
            parts.append("grants=" + ",".join(sorted(set(m.policy_refs))))
        tenant = getattr(m, "tenant", "") or ""
        if tenant:
            parts.append(f"tenant={tenant}")
        return ";".join(parts)

    def _set_state(self, m: Mission, state: MissionState) -> None:
        if m.state == state and state != MissionState.PLANNING:
            return
        m.state = state
        m.updated_at = now()
        self.store.append("MissionStateChanged", m.id, {"state": state.value})
        if state in (MissionState.SUCCEEDED, MissionState.FAILED, MissionState.COMPENSATION_FAILED):
            self.lifecycle.dispatch(MissionFinished(mission_id=m.id, state=state.value))

"""DECIDE -> GOVERN -> ACT -> VERIFY -> LEARN for billing's dunning case (runtime-native plan N1/N3/N4/N5).

    observe.observe_accounts      real BillingSignals from Lago, persisted as bi-temporal Observations
    run_decision_cycle            producer -> DecisionOpportunity -> select_action (do-nothing/abstain are
                                  first-class) -> bridge_selection -> MissionRuntimeLauncher over a runtime
                                  built from THIS app's operator. Every selection writes an InterventionRecord.
    approve_mission               the explicit approver: ``drive`` resolves the parked human gate; only then
                                  does the runtime deliver ``_approval`` and ``billing.dunning`` retry
                                  payment for the chosen invoices (and read each one back from Lago).
    observe_outcomes              N days later, re-read the chased invoices, attribute payment/no-payment to
                                  the intervention (competing interventions discounted), persist OutcomeEvent
                                  + OutcomeLink.
    refit_policy                  the ONLY way the utility model is refit: policy_promotion's held-out gate,
                                  apply=True required. Never called per request.
"""
from __future__ import annotations

import dataclasses
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from agentic_os.app_kit import (BridgeOutcome, BridgeResult, MissionRuntimeLauncher, bridge_selection, drive)
from agentic_os.intervention_record import (FileInterventionStore, InMemoryInterventionStore,
                                            InterventionRecord)
from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import LocalOperatorClient
from agentic_os.mission.planner import TemplatePlanner
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import ExecutionIntent, IntentStep
from agentic_os.outcome_derivation import derive_and_persist
from agentic_os.outcome_store import FileOutcomeStore, InMemoryOutcomeStore
from agentic_os.policy_promotion import (STATIC_POLICY, PolicyRegistry, PolicyVersion, PromotionGate,
                                         PromotionReport, run_promotion_cycle)
from agentic_os.priority_engine import PriorityPolicy, SelectedAction, select_action

from . import observe
from .operator import build_billing_operator
from .producers import BillingSignals, billing_opportunity, dunning_inputs

DAY_S = 86400.0

# action_kind -> the outcome (fact) the capability provides; how a selected action is bound to ITS capability.
_OUTCOME_FOR_KIND = {"dunning": "dunning_attempted", "refund": "refund_staged", "summary": "billing_activity"}
_NEED_FOR_KIND = {"dunning": "retry payment on the selected account's overdue invoices",
                  "refund": "stage a refund credit note", "summary": "summarise the account's billing state"}


class BillingPlanner:
    """Binds a decision's goal (``"<action_kind> for <account>"``) to exactly the capability that provides
    its outcome, and delivers the decision's scope to the node as ``inputs['intent']``.

    Why: the default TemplatePlanner keyword-matches 'dunning' to a 3-step invoice_recovery template that
    the context runtime binds to billing.summary, and the kernel does not deliver ``Mission.inputs`` to
    nodes (kernel follow-up). ``verified_fields`` is the one channel that reaches ``node.inputs``."""

    def __init__(self, fallback=None):
        self._fallback = fallback or TemplatePlanner()
        self._local = threading.local()

    def scoped(self, inputs: Mapping[str, Any]):
        planner = self

        class _Scope:
            def __enter__(self_):
                planner._local.scope = {k: (",".join(map(str, v)) if isinstance(v, (list, tuple)) else str(v))
                                        for k, v in (inputs or {}).items()}

            def __exit__(self_, *a):
                planner._local.scope = None
        return _Scope()

    def plan(self, mission_id: str, goal: str, context: dict) -> ExecutionIntent:
        kind = (goal or "").split(" for ", 1)[0].strip()
        outcome = _OUTCOME_FOR_KIND.get(kind)
        if outcome is None:
            return self._fallback.plan(mission_id, goal, context)
        return ExecutionIntent(
            mission_id=mission_id, rationale=f"billing decision: {kind}",
            steps=[IntentStep(outcome=outcome, need=_NEED_FOR_KIND[kind], value_hint="high")],
            verified_fields=dict(getattr(self._local, "scope", None) or {}))


class ScopedLauncher(MissionRuntimeLauncher):
    """MissionRuntimeLauncher that also hands the decision's inputs to the BillingPlanner for this launch."""

    planner: BillingPlanner = None  # set by build_billing_runtime

    def launch(self, request) -> str:
        with self.planner.scoped(request.inputs):
            return super().launch(request)


@dataclass
class ClosedLoop:
    """Everything the loop owns: the runtime over THIS app's operator, and its stores."""
    runtime: Any
    launcher: ScopedLauncher
    interventions: Any
    outcomes: Any
    observations: Any
    ingestor: Any
    policies: PolicyRegistry = field(default_factory=PolicyRegistry)
    clock: Callable[[], float] = time.time
    launched: Dict[str, dict] = field(default_factory=dict)          # mission_id -> {intervention_id, account, ...}
    dunning_results: Dict[str, dict] = field(default_factory=dict)   # mission_id -> core.chase_invoices result


def build_billing_runtime(on_dunning=None):
    """(MissionRuntime, BillingPlanner) built from this app's operator via the in-process LocalOperatorClient."""
    op = build_billing_operator(on_dunning=on_dunning)
    registry = CapabilityRegistry()
    registry.register(op.manifest)
    planner = BillingPlanner()
    rt = MissionRuntime(registry, Executor(LocalOperatorClient({op.name: op})), planner=planner)
    return rt, planner


def build_loop(state_dir: Optional[str] = None, *, clock: Callable[[], float] = time.time) -> ClosedLoop:
    """Wire a ClosedLoop. ``state_dir`` (or $BILLING_STATE_DIR) => durable JSONL stores; else in-memory."""
    state_dir = state_dir if state_dir is not None else os.environ.get("BILLING_STATE_DIR", "")
    results: Dict[str, dict] = {}
    rt, planner = build_billing_runtime(on_dunning=lambda mid, res: results.__setitem__(str(mid), res))
    launcher = ScopedLauncher(rt)
    launcher.planner = planner
    if state_dir:
        interventions = FileInterventionStore(os.path.join(state_dir, "interventions.jsonl"))
        outcomes = FileOutcomeStore(os.path.join(state_dir, "outcomes.jsonl"))
        observations = observe.FileObservationStore(os.path.join(state_dir, "observations.jsonl"))
    else:
        from agentic_os.observation_store import InMemoryObservationStore
        interventions, outcomes, observations = (InMemoryInterventionStore(), InMemoryOutcomeStore(),
                                                 InMemoryObservationStore())
    return ClosedLoop(runtime=rt, launcher=launcher, interventions=interventions, outcomes=outcomes,
                      observations=observations, ingestor=observe.build_ingestor(observations),
                      clock=clock, dunning_results=results)


# ── interventions: latest-wins projection over the append-only store ───────────────────────────────────
def latest_records(store) -> List[InterventionRecord]:
    by_id: Dict[str, InterventionRecord] = {}
    for r in store.all():
        by_id[r.intervention_id] = r            # a later append for the same id supersedes (execution update)
    return list(by_id.values())


def mark_executed(store, intervention_id: str, *, approved_at: Optional[float], executed_at: Optional[float],
                  execution_ref: str = "") -> InterventionRecord:
    """Append the execution facts for an intervention (records stay immutable; the projection is latest-wins)."""
    cur = next(r for r in latest_records(store) if r.intervention_id == intervention_id)
    rec = dataclasses.replace(cur, approved_at=approved_at, executed_at=executed_at,
                              execution_ref=execution_ref or cur.execution_ref)
    store.append(rec)
    return rec


def _last_dunning(store, account: str) -> Optional[float]:
    ts = [r.executed_at for r in latest_records(store)
          if r.opportunity_id == f"billing:{account}" and r.selected_action == "dunning" and r.executed_at]
    return max(ts, default=None)


# ── DECIDE ─────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DecisionCycle:
    opportunity: Any
    selection: SelectedAction
    bridge: BridgeResult
    mission_id: str
    mission_state: str
    inputs: Mapping[str, Any]


_DENY = lambda _m, _n: "reject"   # noqa: E731 — ungated missions must never need a gate; if one appears, refuse


def run_decision_cycle(signals: BillingSignals, *, store, policy: Optional[PolicyVersion] = None,
                       launcher: Optional[MissionRuntimeLauncher] = None, loop: Optional[ClosedLoop] = None,
                       priority_policy: Optional[PriorityPolicy] = None, now: Optional[float] = None,
                       ) -> DecisionCycle:
    """One decision for one account. ``store`` is the InterventionStore; ``policy`` the active PolicyVersion
    (its learned utility_fn, if any, adjusts selection -- governance still applies). Gated actions are
    launched and PARK; nothing executes here. Ungated read-only actions are run to completion."""
    policy = policy or STATIC_POLICY
    loop = loop or _default_loop()
    launcher = launcher or loop.launcher
    t = loop.clock() if now is None else now
    last = _last_dunning(store, signals.account)
    if last is not None and (signals.last_dunning_at or 0) < last:
        signals = dataclasses.replace(signals, last_dunning_at=last)   # our own dunning counts for the cooldown
    opp = billing_opportunity(signals, now=t)
    sel = select_action(opp, priority_policy, utility_fn=policy.utility_fn(), now=t)
    kind = sel.action.action_kind
    inputs = dunning_inputs(signals) if kind == "dunning" else {"account_id": signals.account}
    refs = tuple(signals.observation_ids)
    if kind == "dunning":
        refs += tuple(f"invoice:{i}" for i in signals.overdue_invoice_ids)
    res = bridge_selection(sel, store=store, policy_version=policy.version, launcher=launcher,
                           inputs=inputs, evidence_refs=refs, now=t)
    state = ""
    if res.mission_id:
        loop.launched[res.mission_id] = {"intervention_id": res.record.intervention_id,
                                         "account": signals.account, "inputs": dict(inputs)}
        if res.outcome is BridgeOutcome.LAUNCHED:                       # ungated read: run it
            drive(loop.runtime, res.mission_id, approver=_DENY)
        else:                                                           # gated: run => it PARKS on the gate
            loop.runtime.run(res.mission_id)
        state = _state(loop.runtime, res.mission_id)
    return DecisionCycle(opportunity=opp, selection=sel, bridge=res, mission_id=res.mission_id,
                         mission_state=state, inputs=inputs)


def _state(runtime, mission_id: str) -> str:
    m = runtime._missions.get(mission_id)
    return getattr(getattr(m, "state", None), "value", str(getattr(m, "state", "")))


# ── GOVERN + ACT + VERIFY ──────────────────────────────────────────────────────────────────────────────
def approve_mission(loop: ClosedLoop, mission_id: str, *, decision: str = "approve",
                    approved_by: str = "operator") -> dict:
    """Resolve the parked gate with an EXPLICIT approver decision via ``drive``. Only missions this loop
    launched are eligible. On approve the runtime delivers ``_approval`` and billing.dunning executes;
    the kernel verifier then judges the read-back. Records approval/execution on the intervention."""
    meta = loop.launched.get(mission_id)
    if meta is None:
        raise KeyError(mission_id)
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be 'approve' or 'reject'")
    pending = loop.runtime.repo.pending_human(mission_id)
    if not pending:
        return {"mission_id": mission_id, "state": _state(loop.runtime, mission_id),
                "note": "no pending approval gate (already resolved)", "dunning": loop.dunning_results.get(mission_id)}
    drive(loop.runtime, mission_id, approver=lambda _m, _n: decision)
    now = loop.clock()
    res = loop.dunning_results.get(mission_id)
    if decision == "approve":
        executed = bool(res and res.get("dunning_attempted"))
        mark_executed(loop.interventions, meta["intervention_id"], approved_at=now,
                      executed_at=now if executed else None, execution_ref=mission_id)
    return {"mission_id": mission_id, "intervention_id": meta["intervention_id"], "decision": decision,
            "approved_by": approved_by, "state": _state(loop.runtime, mission_id), "dunning": res}


# ── LEARN ──────────────────────────────────────────────────────────────────────────────────────────────
def billing_reward_map(obs) -> Mapping[str, float]:
    """Observation -> reward dimensions: the fraction of the chased amount that was collected. A real
    'no payment' is a recorded 0.0, not a missing sample (otherwise only successes would ever be learned)."""
    if obs.kind != "billing.dunning_outcome":
        return {}
    return {"payment_collected": float(obs.payload.get("settled_fraction", 0.0))}


class _AccountView:
    """The InterventionStore the correlator sees: only EXECUTED interventions on this account up to (and
    including) the target -- an abstained/parked decision never explains a payment, an earlier executed one
    (incl. a human's) is a competitor. ``link`` writes through to the real store."""

    def __init__(self, store, target: InterventionRecord):
        self._store, self._t = store, target

    def all(self):
        return [r for r in latest_records(self._store)
                if r.opportunity_id == self._t.opportunity_id and r.executed_at is not None
                and r.executed_at <= self._t.executed_at]

    def link(self, link):
        self._store.link(link)


def observe_outcomes(*, interventions, outcomes, ingestor, now: float, after_days: float = 7.0
                     ) -> List[dict]:
    """For every executed dunning intervention older than ``after_days`` that has no outcome yet: re-read the
    chased invoices from Lago, attribute payment/no-payment (multi-factor confidence, competing interventions
    discounted) and persist OutcomeEvent + OutcomeLink. Returns one report row per intervention considered."""
    linked = {l.intervention_id for l in interventions.links()}
    report = []
    for rec in latest_records(interventions):
        if rec.selected_action != "dunning" or rec.executed_at is None or rec.intervention_id in linked:
            continue
        if now - rec.executed_at < after_days * DAY_S:
            continue
        account = rec.opportunity_id.split(":", 1)[1]
        ids = [r.split(":", 1)[1] for r in rec.evidence_refs if r.startswith("invoice:")]
        obs = observe.observe_dunning_outcome(ingestor, account=account, intervention_id=rec.intervention_id,
                                              invoice_ids=ids, now=now)
        row = {"intervention_id": rec.intervention_id, "account": account, "invoices": ids}
        if obs is None:
            report.append({**row, "skipped": "invoice(s) could not be read back"})
            continue
        got = derive_and_persist(obs, intervention_store=_AccountView(interventions, rec),
                                 outcome_store=outcomes, reward_map=billing_reward_map)
        if got is None:
            report.append({**row, "skipped": "no attributable intervention"})
            continue
        ev, link = got
        report.append({**row, "outcome": obs.payload["outcome"], "reward": dict(ev.reward_dimensions),
                       "attribution_confidence": ev.attribution_confidence,
                       "competing": len(ev.candidate_intervention_ids) - 1,
                       "observation_id": obs.observation_id})
    return report


def refit_policy(outcomes, policies: PolicyRegistry, *, apply: bool = False,
                 gate: Optional[PromotionGate] = None, holdout_fraction: float = 0.3) -> PromotionReport:
    """Refit the UtilityModel -- only through policy_promotion's held-out gate. ``apply=True`` is required to
    promote AND the gate must pass; with too little data it refuses (report.decision.reasons says why)."""
    return run_promotion_cycle(outcomes, gate=gate, registry=policies, holdout_fraction=holdout_fraction,
                               apply=apply)


_DEFAULT: Optional[ClosedLoop] = None


def _default_loop() -> ClosedLoop:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = build_loop()
    return _DEFAULT


def describe_opportunity(opp) -> dict:
    return {"opportunity_id": opp.opportunity_id, "entity": opp.entity, "evidence": list(opp.evidence),
            "candidates": [{"candidate_id": c.candidate_id, "action_kind": c.action_kind,
                            "capability": c.required_capabilities[0] if c.required_capabilities else None,
                            "expected_value": round(c.expected_value, 4), "confidence": round(c.confidence, 4),
                            "urgency": round(c.urgency, 4), "risk_tier": c.risk_tier.name}
                           for c in opp.candidate_actions]}


def describe_cycle(c: DecisionCycle) -> dict:
    sel = c.selection
    return {"opportunity": describe_opportunity(c.opportunity),
            "selected": {"candidate_id": sel.action.candidate_id, "action_kind": sel.action.action_kind,
                         "decision": sel.decision.action.value, "requires_approval": sel.decision.requires_approval,
                         "expected_utility": round(sel.expected_utility, 4), "reason": sel.reason,
                         "alternatives": [list(a) for a in sel.alternatives]},
            "outcome": c.bridge.outcome.value, "intervention_id": c.bridge.record.intervention_id,
            "mission_id": c.mission_id or None, "mission_state": c.mission_state or None,
            "inputs": dict(c.inputs) if c.mission_id else None}


__all__ = ["BillingPlanner", "ClosedLoop", "DecisionCycle", "approve_mission", "billing_reward_map",
           "build_billing_runtime", "build_loop", "describe_cycle", "latest_records", "mark_executed",
           "observe_outcomes", "refit_policy", "run_decision_cycle"]

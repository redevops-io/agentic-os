"""P9 — active plan selection + three separated learners behind the promotion ladder.

Active planning: candidates → policy pruning → simulation → cost/risk/success scoring → selection,
with bounded exploration and a switch margin protecting the default plan.

Learners: routing / model-selection / plan-policy are distinct; none mutates production routing
until a policy explicitly promotes a recommendation (Observe → Recommend → Shadow → Promote).
"""
from __future__ import annotations

from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.learners import (
    LearningStack, ModelSelectionLearner, RoutingLearner,
)
from agentic_os.mission.plan_select import ActivePlanner, score_projection
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.scheduler import SchedulePolicy
from agentic_os.mission.types import (
    CapabilityManifest, CapabilitySpec, ExecutionIntent, IntentStep, MissionState, SimResult,
)


def _two_provider_fleet():
    """Two providers for 'done': A ranks first by the cost model, B is cheaper/reversible."""
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("provA", [CapabilitySpec(
        "provA.do", "provA", provides=["done"], estimated_value="high", side_effecting=True,
        undo="provA.undo", permissions=[])]))
    reg.register(CapabilityManifest("provB", [CapabilitySpec(
        "provB.do", "provB", provides=["done"], estimated_value="medium", side_effecting=True,
        undo="provB.undo", permissions=[])]))
    client = InMemoryOperatorClient({"provA.do": lambda i: {"ok": 1}, "provB.do": lambda i: {"ok": 1}})
    return reg, client


class _P:
    def plan(self, mission_id, goal, context):
        return ExecutionIntent(mission_id=mission_id,
                               steps=[IntentStep(outcome="done", need="do the thing")])


# ─── plan selection enumerates candidates and records the choice ─────────────
def test_active_selection_enumerates_candidates():
    reg, client = _two_provider_fleet()
    rt = MissionRuntime(reg, Executor(client), planner=_P())
    m = rt.create_mission("act", policy_refs=["*"])
    sel = [e for e in rt.repo.timeline(m.id) if e["type"] == "PlanSelected"]
    assert sel, "expected a PlanSelected event when >1 candidate exists"
    labels = {c["label"] for c in sel[0]["payload"]["candidates"]}
    assert "default" in labels and any(l.startswith("done=") for l in labels)   # default + one-swap alt
    assert sum(c["selected"] for c in sel[0]["payload"]["candidates"]) == 1


# ─── exploration is bounded by max_candidates ─────────────────────────────────
def test_candidate_exploration_is_bounded():
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("op", [CapabilitySpec(
        f"op.v{i}", "op", provides=["done"], permissions=[]) for i in range(10)]))
    planner = ActivePlanner(reg, SchedulePolicy(), max_candidates=3)
    from agentic_os.mission.types import Mission
    m = Mission(goal="g", policy_refs=["*"])
    _, cands = planner.select(m, _P().plan(m.id, "g", {}))
    assert len(cands) <= 3            # 9 alternatives available, capped at 3


# ─── an over-budget plan is inadmissible in scoring ───────────────────────────
def test_scoring_rejects_over_budget():
    assert score_projection(SimResult(within_budget=False)) == float("-inf")
    assert score_projection(SimResult(expected_success=1.0, within_budget=True)) == 10.0


# ─── the three learners are separate and only RECOMMEND (no auto-mutation) ────
def test_routing_learner_recommends_but_does_not_promote():
    lr = RoutingLearner(min_support=3, min_lift=0.2)
    for _ in range(4):
        lr.observe("charge", "provA.charge", ok=False)     # default provider, failing
    for _ in range(4):
        lr.observe("charge", "provB.charge", ok=True)      # alternative, reliable
    recs = lr.recommend()
    assert len(recs) == 1
    r = recs[0]
    assert r.kind == "routing" and r.recommended == "provB.charge" and r.lift >= 0.2
    assert r.status == "recommended"                        # NOT promoted


def test_insufficient_support_yields_no_recommendation():
    lr = RoutingLearner(min_support=5)
    lr.observe("x", "a", ok=True)
    lr.observe("x", "b", ok=True)                           # only 1 obs each < min_support
    assert lr.recommend() == []


# ─── the promotion ladder: shadow → policy-constrained promotion ──────────────
def test_promotion_gate_is_the_only_mutation_path():
    stack = LearningStack()
    for _ in range(5):
        stack.routing.observe("charge", "provA", ok=False)
    for _ in range(5):
        stack.routing.observe("charge", "provB", ok=True)
    recs = stack.recommendations()
    assert recs and recs[0].status == "recommended"
    assert stack.promoted_choice("routing", "charge") is None    # nothing promoted yet

    stack.promote(recs[0], actor="governance")                   # explicit policy action
    assert stack.promoted_choice("routing", "charge") == "provB"
    assert stack.recommendations()[0].status == "promoted"       # now reflected as promoted


def test_model_selection_learner_is_independent():
    ms = ModelSelectionLearner(min_support=3, min_lift=0.15)
    for _ in range(4):
        ms.observe("summarize", "small", ok=True)          # cheap tier is enough here
    for _ in range(4):
        ms.observe("summarize", "large", ok=True)
    # equal success → no lift → recommend the incumbent (no churn)
    assert ms.recommend() == []
    assert ms.kind == "model_selection"


# ─── runtime feeds routing observations and surfaces recommendations in EXPLAIN ─
def test_runtime_surfaces_recommendations_in_explain():
    reg, client = _two_provider_fleet()
    rt = MissionRuntime(reg, Executor(client), planner=_P())
    m = rt.create_mission("act", policy_refs=["*"])
    rt.run(m.id)
    assert m.state == MissionState.SUCCEEDED
    assert "recommendations" in rt.explain(m.id)           # the shadow layer is visible to governance

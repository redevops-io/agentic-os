"""Pre-migration gate (parallel-execution plan): attaching a MissionPolicy must NOT, by its mere
presence, turn every side effect into an approval — otherwise real projects migrate successfully yet
stall/serialize unnecessarily. Approval must be driven by an EXPLICIT disposition: a matching policy
rule (REQUIRE_APPROVAL/DENY), a statically mandatory capability, an `approval:<scope>` constraint, or the
dynamic-risk score crossing threshold. A policy whose rules DON'T match a node must leave that node's
outcome identical to the no-policy case.

Audited 2026-08-29: policy presence is provably irrelevant to gating (identical decisions with/without a
non-matching policy). What gates a bare side effect is the dynamic-risk score — irreversibility + blast
radius — which is the intended, explicit risk governance. These tests lock that so a future refactor can't
regress into "policy present ⇒ gate all side effects".
"""
from __future__ import annotations

from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.policy import MissionPolicy, PolicyRule, Effect
from agentic_os.mission.types import (CapabilitySpec, CapabilityManifest, ExecutionIntent, IntentStep,
                                      MissionState)


class _P:
    def plan(self, mission_id, goal, context):
        return ExecutionIntent(mission_id=mission_id, steps=[IntentStep(outcome="done", need="do it")])


def _runtime(**spec_kwargs):
    """A reversible, independent side effect — low dynamic risk, so NOT gated on its own merits. The only
    thing that can gate it is an EXPLICIT matching policy rule."""
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("op", [CapabilitySpec(
        "op.act", "op", provides=["done"], side_effecting=True, undo="op.undo", **spec_kwargs)]))
    return MissionRuntime(reg, Executor(InMemoryOperatorClient({"op.act": lambda i: {"ok": True}})), planner=_P())


# a policy whose rules cannot match op.act: a deny on a different cap + a spend gate that only fires >$1000
NON_MATCHING = MissionPolicy(id="finance", version="1", rules=(
    PolicyRule(id="only_prod", effect=Effect.DENY, match="cap:cap.deploy_prod"),
    PolicyRule(id="big_spend", effect=Effect.REQUIRE_APPROVAL, match="side_effecting", max_usd=1000.0),
))


def _final_state(policy):
    rt = _runtime()
    m = rt.create_mission("act", policy_refs=["*"], policy=policy)
    return rt.run(m.id).state


def test_policy_presence_does_not_gate_a_node_its_rules_dont_match():
    bare = _final_state(None)
    with_policy = _final_state(NON_MATCHING)
    assert bare == MissionState.SUCCEEDED, f"baseline unexpectedly not succeeded: {bare}"
    assert with_policy == bare, ("a non-matching policy changed the outcome — policy PRESENCE is gating, "
                                 f"not an explicit rule (got {with_policy})")


def test_a_matching_require_approval_rule_gates():
    """The intended path: an EXPLICIT matching rule is what parks for approval."""
    pol = MissionPolicy(id="p", version="1",
                        rules=(PolicyRule(id="review", effect=Effect.REQUIRE_APPROVAL, match="cap:op.act"),))
    assert _final_state(pol) == MissionState.WAITING_HUMAN


def test_a_matching_deny_rule_blocks():
    pol = MissionPolicy(id="p", version="1",
                        rules=(PolicyRule(id="halt", effect=Effect.DENY, match="cap:op.act"),))
    assert _final_state(pol) == MissionState.FAILED

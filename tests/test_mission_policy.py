"""mission-policy/v1 — policy as a first-class, versioned, digest-pinned runtime object.

A MissionPolicy is the single authority for a mission: ordered rules with explicit precedence
(deny > require_approval > allow), a content digest pinned onto the mission history for replay,
and actionable block messages (reason + suggested action). Verified end-to-end through the real
MissionRuntime — a gate parks for a human, a DENY blocks outright (no human can approve it), every
policy decision lands in the ledger, and a mission without a policy behaves exactly as before.
"""
from __future__ import annotations

from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import (
    CapabilityManifest, CapabilitySpec, ExecutionIntent, IntentStep, MissionState,
)
from agentic_os.mission.policy import MissionPolicy, PolicyRule, Effect, NodeContext, from_constraints


def _fleet(spec_kwargs, *, handler=None):
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("op", [CapabilitySpec("op.act", "op", provides=["done"], **spec_kwargs)]))
    return reg, InMemoryOperatorClient({"op.act": handler or (lambda i: {"ok": True})})


class _P:
    def plan(self, mission_id, goal, context):
        return ExecutionIntent(mission_id=mission_id, steps=[IntentStep(outcome="done", need="do it")])


def _events(rt, mid, typ):
    return [e["payload"] for e in rt.repo.timeline(mid) if e["type"] == typ]


_GATE = PolicyRule("writes_gated", Effect.REQUIRE_APPROVAL, "side_effecting",
                   reason="External writes are gated in finance-prod.",
                   suggested_action="Approve in the cockpit, or run in shadow mode.")
_DENY = PolicyRule("no_irreversible", Effect.DENY, "irreversible",
                   reason="Irreversible actions are not permitted in finance-prod.",
                   suggested_action="Use a reversible capability or request an exception.")


def _policy():
    return MissionPolicy(id="finance-prod", version="12", scope="organization",
                         support_message="Contact Platform Engineering for external write access.",
                         rules=(_DENY, _GATE))


# ── the object itself: identity + deny-wins precedence ─────────────────────────────────────────────
def test_policy_digest_is_stable_and_content_addressed():
    p = _policy()
    assert p.digest() == p.digest()                       # stable
    assert p.digest().startswith("sha256:")
    assert p.digest() != MissionPolicy(id="finance-prod", version="13", rules=(_DENY, _GATE)).digest()
    assert p.ref == "finance-prod@12"


def test_deny_beats_require_approval():
    p = _policy()
    # an irreversible side-effecting node matches BOTH rules; deny must win
    o = p.evaluate(NodeContext(capability="infra.destroy", side_effecting=True, reversible=False))
    assert o.effect is Effect.DENY and o.matched_rule == "no_irreversible"
    # a reversible side-effecting node matches only the gate
    o2 = p.evaluate(NodeContext(capability="billing.charge", side_effecting=True, reversible=True))
    assert o2.effect is Effect.REQUIRE_APPROVAL and o2.matched_rule == "writes_gated"
    # a read is allowed
    assert p.evaluate(NodeContext(capability="read.market")).effect is Effect.ALLOW


# ── end-to-end through the runtime: gate parks, pins identity, lands in the ledger ─────────────────
def test_policy_gate_parks_and_pins_identity():
    reg, client = _fleet(dict(side_effecting=True, undo="op.undo"))   # reversible side effect → gate
    rt = MissionRuntime(reg, Executor(client), planner=_P())
    m = rt.create_mission("act", policy_refs=["*"], policy=_policy())
    m = rt.run(m.id)
    assert m.state == MissionState.WAITING_HUMAN

    parked = _events(rt, m.id, "NodeParked")[-1]
    assert "mission_policy" in parked["rules"]
    # the policy decision was recorded with its pinned digest
    pe = _events(rt, m.id, "PolicyEvaluated")
    assert pe and pe[-1]["policy"] == "finance-prod@12"
    assert pe[-1]["policy_digest"] == _policy().digest()
    assert pe[-1]["effect"] == "require_approval" and pe[-1]["matched_rule"] == "writes_gated"
    # creation pinned the policy identity too
    created = _events(rt, m.id, "MissionCreated")[-1]
    assert created["policy"] == "finance-prod@12" and created["policy_digest"] == _policy().digest()


def test_policy_deny_blocks_outright_with_actionable_message():
    reg, client = _fleet(dict(side_effecting=True))                   # irreversible (no undo) → DENY
    rt = MissionRuntime(reg, Executor(client), planner=_P())
    m = rt.create_mission("act", policy_refs=["*"], policy=_policy())
    m = rt.run(m.id)
    # deny is a hard block, not a human gate
    assert m.state == MissionState.FAILED
    assert m.outcome and m.outcome.get("blocked") == "policy_denied"
    denied = _events(rt, m.id, "PolicyDenied")[-1]
    assert denied["policy"] == "finance-prod@12"
    msg = denied["explain"]["message"]
    assert "Denied." in msg and "Reason:" in msg and "Suggested action:" in msg
    assert "Contact Platform Engineering" in msg                     # org support message surfaced


def test_explain_policy_traces_every_rule():
    reg, client = _fleet(dict(side_effecting=True, undo="op.undo"))
    rt = MissionRuntime(reg, Executor(client), planner=_P())
    m = rt.create_mission("act", policy_refs=["*"], policy=_policy())
    m = rt.run(m.id)
    # explain a representative node's policy decision directly via the plane
    ex = rt.policy_plane.explain_policy(_FakeNode(), m)
    assert ex["policy"] == "finance-prod@12"
    assert {t["rule"] for t in ex["trace"]} == {"no_irreversible", "writes_gated"}
    assert ex["effect"] == "require_approval"                        # reversible side effect → gate wins


class _FakeNode:
    capability = "billing.charge"; operator = "op"; produces = "done"
    side_effecting = True; undo = "op.undo"; approval_required = False
    inputs: dict = {}
    class cost:  # noqa
        usd = 1.0


# ── backward compatibility: no policy, and legacy constraints still work ───────────────────────────
def test_no_policy_is_unchanged():
    reg, client = _fleet(dict(side_effecting=False))                 # benign, no policy
    rt = MissionRuntime(reg, Executor(client), planner=_P())
    m = rt.create_mission("act", policy_refs=["*"])
    m = rt.run(m.id)
    assert m.state == MissionState.SUCCEEDED
    assert not _events(rt, m.id, "PolicyEvaluated")                  # nothing pinned when no policy
    created = _events(rt, m.id, "MissionCreated")[-1]
    assert created["policy"] is None


def test_legacy_constraints_lift_into_a_policy():
    p = from_constraints(["approval:side_effecting", "approval:cap:op.act"], ["*"])
    assert len(p.rules) == 2
    assert p.evaluate(NodeContext(capability="op.act", side_effecting=True)).effect is Effect.REQUIRE_APPROVAL
    # still pinnable
    assert p.digest().startswith("sha256:")

"""Runnable proof: the onboarding mission end-to-end, on mock operators, in-process.

    python -m agentic_os.mission.demo

Shows the whole kernel loop — plan -> compile -> simulate -> schedule -> execute over world
state, a human gate on the compliance step, saga-ready side effects, an outcome event and a
reflection — plus a RESTART-RESUME: a second runtime built on the same event log rehydrates a
parked mission and drives it to completion. No LLM, no Dagster, no network.
"""
from __future__ import annotations

from .executor import Executor, InMemoryOperatorClient
from .registry import CapabilityRegistry
from .runtime import MissionRuntime
from .store import EventStore
from .types import CapabilityManifest, CapabilitySpec, NodeCost, MissionState


def build_fleet() -> tuple[CapabilityRegistry, InMemoryOperatorClient]:
    """Four mock operators publishing capability manifests + their /invoke handlers."""
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("agentic-billing", [
        CapabilitySpec("billing.create_subscription", "agentic-billing", provides=["subscription"],
                       outputs={"subscription_id": "string"}, side_effecting=True,
                       undo="billing.cancel_subscription", permissions=["billing:write"],
                       cost=NodeCost(usd=0.01, latency_ms=800), estimated_value="high"),
    ]))
    reg.register(CapabilityManifest("agentic-support", [
        CapabilitySpec("support.send_onboarding", "agentic-support", provides=["onboarding_sent"],
                       side_effecting=True, permissions=["support:write"],
                       cost=NodeCost(usd=0.002, latency_ms=500)),
    ]))
    reg.register(CapabilityManifest("agentic-books", [
        CapabilitySpec("books.record_revenue", "agentic-books", provides=["revenue_recorded"],
                       side_effecting=True, undo="books.reverse_entry", permissions=["books:write"],
                       cost=NodeCost(usd=0.001, latency_ms=400)),
    ]))
    reg.register(CapabilityManifest("agentic-compliance", [
        CapabilitySpec("compliance.file_consent", "agentic-compliance", provides=["consent_filed"],
                       side_effecting=True, approval_required=True, permissions=["compliance:write"],
                       cost=NodeCost(usd=0.0, latency_ms=300), estimated_value="high"),
    ]))

    handlers = {
        "billing.create_subscription": lambda i: {"subscription_id": "sub_123", "plan": "pro"},
        "support.send_onboarding": lambda i: {"sent": True, "sub": i.get("subscription")},
        "books.record_revenue": lambda i: {"entry": "je_88", "sub": i.get("subscription")},
        "compliance.file_consent": lambda i: {"consent_id": "gdpr_42"},
        "billing.cancel_subscription": lambda i: {"cancelled": True},
        "books.reverse_entry": lambda i: {"reversed": True},
    }
    return reg, InMemoryOperatorClient(handlers)


def _print_timeline(rt: MissionRuntime, mid: str) -> None:
    for e in rt.repo.timeline(mid):
        print(f"  [{e['seq']:>2}] {e['type']:<20} {str(e['payload'])[:76]}")


def main() -> None:
    reg, client = build_fleet()
    store = EventStore()
    rt = MissionRuntime(reg, Executor(client), store=store)

    grants = ["billing:write", "support:write", "books:write", "compliance:write"]
    m = rt.create_mission("Onboard a new customer", policy_refs=grants, template="onboarding")
    print(f"Mission {m.id}  goal={m.goal!r}")
    plan = rt._plans[m.id]
    print(f"Plan: {' -> '.join(n.capability for n in plan.graph.nodes)}")
    print(f"Simulation: {plan.projection}\n")

    rt.run(m.id)
    print(f"State after run: {m.state.value}  (parked for human approval on the consent step)")
    pending = rt.repo.pending_human(m.id)
    print(f"Human task: {pending['prompt']}  assignee={pending['assignee']}\n")

    # --- RESTART: a brand-new runtime on the SAME event log picks the mission up mid-flight ---
    rt2 = MissionRuntime(reg, Executor(client), store=store)
    m2 = rt2.rehydrate(m.id)
    print(f"[restart] rehydrated {m2.id} from the event log; state={m2.state.value}")
    rt2.approve(m.id, pending["node_id"], "approve")
    print(f"[restart] after approval: state={rt2._missions[m.id].state.value}\n")

    _print_timeline(rt2, m.id)
    final = rt2._missions[m.id]
    print(f"\nOutcome: {final.outcome}")
    print(f"Business value learned for 'onboarding': {rt2.learning.business_value('onboarding')}")
    assert final.state == MissionState.SUCCEEDED, "mission should have succeeded"


if __name__ == "__main__":
    main()

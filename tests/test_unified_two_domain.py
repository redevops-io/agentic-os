"""P2 — the unified two-domain Mission vertical slice, end to end, headless.

Walks the whole acceptance diagram: intent → plan → CRM capability → Support capability →
WAITING_FOR_APPROVAL → approve → resume → side effect → receipt → force restart → rehydrate →
same Mission/state/receipt. Plus the two P2 invariants:

  * durable suspend/resume across a real process restart (fresh runtime + EventStore from disk);
  * credential invisibility — the Twenty/Chatwoot secret values never appear in mission state, events,
    the receipt, evidence, or the connections view the UI is allowed to see.
"""
from __future__ import annotations

import json
import os

import pytest

from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import LocalOperatorClient
from agentic_os.mission.receipt import mission_receipt
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import MissionState
from agentic_os.mission.unified_demo import (
    build_unified_operators, build_unified_runtime, connections_view, seed_mission)

TWENTY_SENTINEL = "TWENTY-SECRET-do-not-log-abc"
CHATWOOT_SENTINEL = "CHATWOOT-SECRET-do-not-log-xyz"


@pytest.fixture()
def creds(monkeypatch):
    # Real secret VALUES in the env-backed store; cores pointed at an unroutable address so writes fall to
    # the honest in-memory projection (no live Twenty/Chatwoot needed).
    monkeypatch.setenv("TWENTY_API_KEY", TWENTY_SENTINEL)
    monkeypatch.setenv("CHATWOOT_API_TOKEN", CHATWOOT_SENTINEL)
    monkeypatch.setenv("TWENTY_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("CHATWOOT_BASE_URL", "http://127.0.0.1:9")


def _dump(*objs) -> str:
    return json.dumps(objs, default=str)


def test_two_domain_mission_end_to_end_with_restart(tmp_path, creds):
    path = str(tmp_path / "unified_events.jsonl")

    # ── create + run to the human gate ────────────────────────────────────────
    rt, _ = build_unified_runtime(path)
    m = seed_mission(rt)
    assert rt.repo.state(m.id) == MissionState.WAITING_HUMAN

    pending = rt.repo.pending_human(m.id)
    assert pending and pending["capability"] == "support.contact.upsert"   # the consequential action gated

    # the first (CRM) domain already ran, provider-neutrally, before the gate
    results = rt.repo.node_results(m.id)
    crm_result = next(r for r in results.values() if "brief" in r)
    assert crm_result["realism"] == "SEEDED-DEMO" and crm_result["connected"] is False

    # invariant 2a — no secret value anywhere the UI could read pre-approval
    surface = _dump(rt.repo.timeline(m.id), rt.explain(m.id), pending, connections_view(rt), results)
    assert TWENTY_SENTINEL not in surface and CHATWOOT_SENTINEL not in surface

    # ── force restart BEFORE approving: fresh runtime + store from disk ────────
    rt2, _ = build_unified_runtime(path)
    m2 = rt2.rehydrate(m.id)
    assert m2.id == m.id and rt2.repo.state(m.id) == MissionState.WAITING_HUMAN   # durable suspend

    # ── approve on the rehydrated runtime → resume → side effect executes ─────
    node_id = rt2.repo.pending_human(m.id)["node_id"]
    rt2.approve(m.id, node_id, "approve")
    assert rt2.repo.state(m.id) == MissionState.SUCCEEDED

    # the SECOND domain (Support) actually ran through the world adapter (Gap #2: operator got the secret)
    results2 = rt2.repo.node_results(m.id)
    support_result = next(r for r in results2.values() if "native_id" in r)
    assert "contact" in support_result["native_id"] and support_result["realism"] == "SEEDED-DEMO"

    # ── the Action Receipt (Gap #1) ──────────────────────────────────────────
    receipt = mission_receipt(rt2, m.id)
    assert receipt["state"] == MissionState.SUCCEEDED.value
    assert receipt["outcome"] and receipt["outcome"]["success"] is True
    caps = {s["capability"] for s in receipt["steps"]}
    assert {"crm.read_account", "support.contact.upsert"} <= caps
    assert any(a["capability"] == "support.contact.upsert" for a in receipt["approvals"])

    # invariant 2b — no secret value in the receipt / full post-run surface
    surface2 = _dump(receipt, rt2.repo.timeline(m.id), rt2.explain(m.id), results2, connections_view(rt2))
    assert TWENTY_SENTINEL not in surface2 and CHATWOOT_SENTINEL not in surface2

    # ── force restart AFTER completion: same id, same state, same receipt ─────
    rt3, _ = build_unified_runtime(path)
    rt3.rehydrate(m.id)
    assert rt3.repo.state(m.id) == MissionState.SUCCEEDED
    assert mission_receipt(rt3, m.id) == receipt      # receipt is a pure fold of the durable log


def test_connections_view_is_credential_invisible(creds):
    """The view the UI may read shows connection status per domain and never a token."""
    view = connections_view(None)
    doms = {c["domain"]: c for c in view["connections"]}
    assert doms["CRM"]["connected"] is True and doms["Support"]["connected"] is True
    assert doms["CRM"]["credential"] == "broker-managed"
    assert TWENTY_SENTINEL not in json.dumps(view) and CHATWOOT_SENTINEL not in json.dumps(view)


def test_insufficient_authority_blocks_the_side_effect(tmp_path, creds):
    """If the mission authority does not cover support:write, the gated side effect never runs — the
    credential is never granted. Proves authority (not just approval) gates the consequential action."""
    from agentic_os.mission.registry import CapabilityRegistry
    from agentic_os.mission.unified_demo import (
        UnifiedTwoDomainPlanner, _authority_for, _credentials_for)
    from runtime_contracts import (
        AuthorityContext, EnvironmentSecretStore, LocalCredentialBroker, PrincipalRef)
    from agentic_os.mission.store import EventStore

    ops = build_unified_operators()
    reg = CapabilityRegistry()
    for op in ops.values():
        reg.register(op.manifest)
    # authority WITHOUT support:write
    authority = AuthorityContext(authority_id="weak", principal=PrincipalRef(id="weak", kind="service"),
                                 purpose="pilot", scope=("crm:read",))
    ex = Executor(LocalOperatorClient(ops), authority=authority,
                  broker=LocalCredentialBroker(EnvironmentSecretStore()),
                  credentials_for=_credentials_for, authority_for=_authority_for)
    rt = MissionRuntime(reg, ex, store=EventStore(path=str(tmp_path / "weak.jsonl")),
                        planner=UnifiedTwoDomainPlanner())
    m = rt.create_mission("cross-domain", policy_refs=["crm:read", "support:write"])
    rt.run(m.id)
    rt.approve(m.id, rt.repo.pending_human(m.id)["node_id"], "approve")
    # the support side-effect must NOT have produced a contact
    assert not any("native_id" in r for r in rt.repo.node_results(m.id).values())
    assert rt.repo.state(m.id) == MissionState.FAILED

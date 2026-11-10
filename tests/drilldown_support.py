"""Shared fixtures for the Projects mission drill-down contract tests.

Every mission here is produced by the REAL mission runtime (``MissionRuntime`` + ``Executor`` + the credential broker)
over the two-domain Revenue->Support shape (``unified_demo``): a CRM read, an approval gate, a gated Support write, a
verification of the write, an outcome. Nothing is a canned timeline: the event log under test is whatever the runtime
wrote. The application cores are NOT reachable in these tests (the adapters fall back to the in-memory projection and
say so in the result: ``realism = SEEDED-DEMO``, ``connected = false``); the live-stack plan covers real cores.
"""
from __future__ import annotations

import hashlib
import json
import os

from agentic_os.drilldown import DrilldownService
from agentic_os.evidence_query import JsonlAuditSink, MemoryAuditSink
from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import LocalOperatorClient
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.unified_demo import (
    UnifiedTwoDomainPlanner, _authority_for, _credentials_for, build_unified_operators, build_unified_runtime, seed_mission)
from agentic_os.overlays import Principal
from agentic_os.permissions import GrantStore, PermissionsPlane
from agentic_os.evidence_query import GrantsCeiling

TWENTY_SENTINEL = "TWENTY-SECRET-do-not-log-abc"
CHATWOOT_SENTINEL = "CHATWOOT-SECRET-do-not-log-xyz"
TOKEN = "owner-token-0123456789-abcdefghij"          # >= 24 characters (the single-owner authenticator refuses shorter)
SCOPE = "customer-ops"


def set_env(monkeypatch):
    monkeypatch.setenv("TWENTY_API_KEY", TWENTY_SENTINEL)
    monkeypatch.setenv("CHATWOOT_API_TOKEN", CHATWOOT_SENTINEL)
    monkeypatch.setenv("TWENTY_BASE_URL", "http://127.0.0.1:9")        # nothing listens: the adapters report SEEDED-DEMO honestly
    monkeypatch.setenv("CHATWOOT_BASE_URL", "http://127.0.0.1:9")


def completed_mission(path: str):
    """Run the two-domain mission for real to the approval gate, approve it, let it finish. Returns (runtime, mission_id)."""
    rt, _ = build_unified_runtime(path)
    m = seed_mission(rt)
    rt.approve(m.id, rt.repo.pending_human(m.id)["node_id"], "approve")
    return rt, m.id


def gated_mission(path: str):
    """The same mission, left parked at the approval gate."""
    rt, _ = build_unified_runtime(path)
    m = seed_mission(rt)
    return rt, m.id


def failed_mission(path: str):
    """Partial failure: the CRM read succeeds, the approved Support write fails because the authority lacks support:write."""
    from runtime_contracts import AuthorityContext, EnvironmentSecretStore, LocalCredentialBroker, PrincipalRef
    ops = build_unified_operators()
    reg = CapabilityRegistry()
    for op in ops.values():
        reg.register(op.manifest)
    # the weak authority differs from the demo's only in its scope: it carries the demo's tenant (the credential gate refuses a tenant-less
    # authority before the scope is looked at, and this test is about the scope)
    authority = AuthorityContext(authority_id="weak", principal=PrincipalRef(id="weak", kind="service", tenant="Meridian Wealth Management"),
                                 purpose="pilot", scope=("crm:read",))
    ex = Executor(LocalOperatorClient(ops), authority=authority, broker=LocalCredentialBroker(EnvironmentSecretStore()),
                  credentials_for=_credentials_for, authority_for=_authority_for)
    rt = MissionRuntime(reg, ex, store=EventStore(path=path), planner=UnifiedTwoDomainPlanner())
    m = rt.create_mission("cross-domain", policy_refs=["crm:read", "support:write"])
    rt.run(m.id)
    rt.approve(m.id, rt.repo.pending_human(m.id)["node_id"], "approve")
    return rt, m.id


def owner() -> Principal:
    from agentic_os.mission.principal import default_issuer
    return default_issuer().issue(Principal("owner", "user", ("operator",)), method="test-login")


def ceiling_for(tmp_path, grants=(("owner", f"evidence.{SCOPE}.inspect"),)):
    """The REAL permission plane (encrypted grant store in tmp). Returns (ceiling, grant_store)."""
    store = GrantStore(str(tmp_path / "perm"))
    for user, name in grants:
        store.add("user", user, "tool", name, actions=["read"], created_by="test")
    return GrantsCeiling(PermissionsPlane(store)), store


def make_service(store, tmp_path, *, scope_of=None, grants=(("owner", f"evidence.{SCOPE}.inspect"),), audit=None, resolver=None):
    ceiling, perm_store = ceiling_for(tmp_path, grants)
    sink = audit if audit is not None else MemoryAuditSink()
    scope_of = scope_of if scope_of is not None else (lambda mid: SCOPE if mid in set(store.mission_ids()) else None)
    svc = DrilldownService(store, ceiling, sink, scope_for=scope_of, resolver=resolver)
    return svc, sink, perm_store


# independent re-implementations used as oracles (deliberately NOT imported from the package under test)
def oracle_event_sha256(e) -> str:
    body = {"seq": e.seq, "ts": e.ts, "type": e.type, "mission_id": e.mission_id, "payload": e.payload}
    return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def rewrite_jsonl_line(path: str, seq: int, mutate) -> None:
    """Tamper with one persisted event in place (what an attacker or a bad migration could do to the file)."""
    out = []
    with open(path) as fh:
        for line in fh:
            d = json.loads(line)
            if d["seq"] == seq:
                mutate(d)
            out.append(json.dumps(d))
    with open(path, "w") as fh:
        fh.write("\n".join(out) + "\n")

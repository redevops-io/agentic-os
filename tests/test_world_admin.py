"""Admin snapshot — the Business-OS governance view. Runs the engine and reports cores/governance/budgets/
autonomy honestly. Offline; deterministic."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime_contracts import AuthorityContext, PrincipalRef  # noqa: E402
from agentic_os.world import ALL_WORLDS, build_admin_snapshot, core_status  # noqa: E402
from agentic_os.world.adapters import LagoBillingAdapter  # noqa: E402
from agentic_os.world.models import ExecutionMode  # noqa: E402
from agentic_os.world.orchestrator import ScenarioOrchestrator  # noqa: E402

_SCOPES = ("read:crm", "read:geo", "write:quote", "write:crm", "read:secrets", "write:vendor", "write:billing")
_SEEDS = {"after-hours-lead": "8842", "kyc-ownership": "clean", "finance-leakage": "4471",
          "gtm-pilot-discovery": "c1", "creator-sponsorship": "s1", "sponsorship-booking": "s1",
          "paid-acquisition": "s1", "contact-center": "c1"}


def _auth():
    return AuthorityContext(authority_id="c", principal=PrincipalRef(id="a", tenant="rd"), purpose="admin",
                            scope=_SCOPES)


def _snap():
    return build_admin_snapshot(ALL_WORLDS, authority=_auth(), seeds=_SEEDS, offline=True)


def test_core_status_reports_seeded_when_no_live_core(monkeypatch):
    for e in ("LAGO_API_URL", "LAGO_API_KEY", "TWENTY_BASE_URL", "TWENTY_API_KEY",
              "CHATWOOT_BASE_URL", "CHATWOOT_API_TOKEN"):
        monkeypatch.delenv(e, raising=False)
    cores = core_status()
    assert cores and all(c["status"] == "SEEDED" for c in cores)          # nothing wired -> honestly SEEDED
    assert {c["app"] for c in cores} >= {"twenty", "lago", "chatwoot"}


def test_snapshot_reports_governance_clean_across_missions():
    s = _snap()
    assert s["governance"] and all(g["clean"] for g in s["governance"])   # every invariant held
    assert s["summary"]["governance_clean"] is True


def test_snapshot_reports_budget_governor_ledger():
    s = _snap()
    paid = [b for b in s["budgets"] if b["world"] == "paid-acquisition"]
    assert paid and paid[0]["committed_total"] <= paid[0]["total_budget"]  # never over budget
    assert s["summary"]["budget_remaining"] >= 0


def test_snapshot_autonomy_ladder_marks_approval_gated_missions():
    s = _snap()
    rungs = {a["world"]: a["rung"] for a in s["autonomy"]}
    assert rungs["sponsorship-booking"] == "APPROVE" and rungs["paid-acquisition"] == "APPROVE"
    assert s["summary"]["approval_gated"] >= 5


def test_snapshot_summary_counts_cores_and_worlds():
    s = _snap()
    assert s["summary"]["cores_total"] == len(s["cores"]) and s["summary"]["worlds"] == len(s["worlds"])


def test_admin_snapshot_never_reaches_a_real_core_even_when_one_is_configured_and_reachable(monkeypatch):
    """B3 regression: the admin console's own default mode is SIMULATE and must stay fail-closed for real
    writes regardless of what credentials happen to be set in the environment — the kernel, not the
    deployer's .env, decides whether a run is allowed to touch a real external system."""
    monkeypatch.setenv("LAGO_API_URL", "https://billing.example.invalid")
    monkeypatch.setenv("LAGO_API_KEY", "not-a-real-key")
    monkeypatch.setattr(LagoBillingAdapter, "available", lambda self: True)   # "configured and reachable"
    called = []
    monkeypatch.setattr(LagoBillingAdapter, "upsert", lambda self, obj: called.append(obj) or "lago-fake-id")

    s = _snap()   # admin-console's own call path: no explicit mode -> ScenarioOrchestrator's SIMULATE default

    assert called == [], "a real Lago write happened under the admin console's default (SIMULATE) mode"
    assert all(p["realism"] != "REAL_LIVE" for w in ALL_WORLDS.values()
               for p in _run_and_collect_projections(w)), "a SIMULATE run recorded a REAL_LIVE projection"
    assert s["summary"]["cores_total"] >= 1   # the snapshot itself still works, just never writes for real


def test_live_mode_is_the_one_explicit_opt_in_that_may_reach_a_real_core(monkeypatch):
    """The other side of the same guarantee: explicitly asking for LIVE is still honoured — this is an
    opt-in a caller must request, not something SIMULATE accidentally grants."""
    monkeypatch.setenv("LAGO_API_URL", "https://billing.example.invalid")
    monkeypatch.setenv("LAGO_API_KEY", "not-a-real-key")
    monkeypatch.setattr(LagoBillingAdapter, "available", lambda self: True)
    called = []
    monkeypatch.setattr(LagoBillingAdapter, "upsert", lambda self, obj: called.append(obj) or "lago-fake-id")

    world = ALL_WORLDS["after-hours-lead"]
    ScenarioOrchestrator().run(world, mode=ExecutionMode.LIVE, seed="8842", authority=_auth(),
                               answers={}, offline=True)

    assert called, "ExecutionMode.LIVE must still be able to reach a real, configured + reachable core"


def _run_and_collect_projections(world):
    orch = ScenarioOrchestrator()
    run = orch.run(world, seed="seed-0", authority=_auth(), answers={}, offline=True)
    return orch._seeder.projections

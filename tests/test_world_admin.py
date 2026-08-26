"""Admin snapshot — the Business-OS governance view. Runs the engine and reports cores/governance/budgets/
autonomy honestly. Offline; deterministic."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime_contracts import AuthorityContext, PrincipalRef  # noqa: E402
from agentic_os.world import ALL_WORLDS, build_admin_snapshot, core_status  # noqa: E402

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

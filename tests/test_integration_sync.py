"""Integration plane §10 — the Bidirectional Sync Protocol.

Proves: one field is reconciled across N systems into a single convergent value by an explicit policy; the
divergent systems are converged by WRITING the value back and VERIFYING it (a dischargeable Obligation, not
"we pushed it"); systems already in agreement are left untouched; and when no safe value exists (two systems of
record disagree) it refuses to guess and raises a durable SYNC_CONFLICT — never a silent overwrite.
"""
from __future__ import annotations

from agentic_os.integration import (
    ConvergeTarget, InMemoryIntegrationProvider, Report, converge, plan_sync,
)
from agentic_os.integration.contracts import ExceptionCategory


# ── the decision ──────────────────────────────────────────────────────────────────────────────────────────
def test_all_agree_is_converged_no_write():
    d = plan_sync("person:1", "subscribed", [
        Report("crm", False), Report("marketing", False), Report("app", False)])
    assert d.converged and d.resolved_value is False and d.divergent == () and not d.needs_write


def test_authority_order_picks_the_system_of_record():
    d = plan_sync("person:1", "subscribed",
                  [Report("crm", False), Report("marketing", True)],
                  policy="authority_order", authority_order=("crm", "marketing"))
    assert d.resolved_value is False                      # CRM wins → unsubscribed
    assert d.in_agreement == ("crm",) and d.divergent == ("marketing",) and d.needs_write


def test_two_systems_of_record_disagree_raises_sync_conflict():
    d = plan_sync("acct:9", "status",
                  [Report("billing", "cancelled", authoritative=True),
                   Report("provisioning", "active", authoritative=True)],
                  policy="authority_order")
    assert d.exception is not None and not d.needs_write
    assert d.exception.category == ExceptionCategory.SYNC_CONFLICT
    assert "billing" in " ".join(d.exception.evidence) and d.resolved_value is None


def test_most_restrictive_booleans_and_scopes():
    # access: any system that says "no" wins
    d = plan_sync("person:1", "access", [Report("hr", True), Report("idp", False)], policy="most_restrictive")
    assert d.resolved_value is False
    # scopes: the intersection (least privilege)
    s = plan_sync("person:1", "scopes",
                  [Report("a", ["read", "write", "admin"]), Report("b", ["read", "write"])],
                  policy="most_restrictive")
    assert s.resolved_value == ("read", "write")


def test_latest_wins_by_timestamp_and_needs_one():
    d = plan_sync("person:1", "phone",
                  [Report("crm", "111", observed_at="2026-10-01T00:00:00Z"),
                   Report("app", "222", observed_at="2026-10-04T00:00:00Z")],
                  policy="latest")
    assert d.resolved_value == "222" and d.divergent == ("crm",)
    missing = plan_sync("person:1", "phone", [Report("crm", "111"), Report("app", "222")], policy="latest")
    assert missing.exception.category == ExceptionCategory.SYNC_CONFLICT


# ── the convergence (write-back verified by read-back) ───────────────────────────────────────────────────
def _provider(name, value):
    p = InMemoryIntegrationProvider(name)
    p.create_object("contact", {"id": "c1", "subscribed": value})
    return p


def test_converge_writes_only_divergent_systems_and_verifies():
    crm = _provider("crm", False)
    marketing = _provider("marketing", True)             # drifted — still subscribed
    d = plan_sync("person:1", "subscribed",
                  [Report("crm", False), Report("marketing", True)],
                  policy="authority_order", authority_order=("crm", "marketing"))
    results = converge(d, {
        "crm": ConvergeTarget(crm, "contact", "c1", "subscribed"),
        "marketing": ConvergeTarget(marketing, "contact", "c1", "subscribed")})
    assert set(results) == {"marketing"}                 # crm already agreed → not written
    assert results["marketing"].satisfied
    assert marketing.read_object("contact", "c1").normalized_fields["subscribed"] is False  # converged + verified


def test_converge_writes_nothing_on_sync_conflict():
    billing = _provider("billing", "cancelled")
    prov = _provider("provisioning", "active")
    d = plan_sync("acct:9", "status",
                  [Report("billing", "cancelled", authoritative=True),
                   Report("provisioning", "active", authoritative=True)], policy="authority_order")
    results = converge(d, {"provisioning": ConvergeTarget(prov, "contact", "c1", "subscribed")})
    assert results == {}                                 # unresolved → never overwrite
    assert prov.read_object("contact", "c1").normalized_fields["subscribed"] == "active"


def test_converge_catches_silent_writeback_failure():
    marketing = _provider("marketing", True)
    marketing._drop = True                               # writes return ok but never land (InMemory drop flag)
    d = plan_sync("person:1", "subscribed",
                  [Report("crm", False), Report("marketing", True)],
                  policy="authority_order", authority_order=("crm", "marketing"))
    results = converge(d, {"marketing": ConvergeTarget(marketing, "contact", "c1", "subscribed")})
    assert not results["marketing"].satisfied            # read-back shows it never converged → caught

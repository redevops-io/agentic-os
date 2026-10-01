"""Converged-breadth acceptance: the six self-contained security planes ported from the deployed
Edge Sentinel monolith import cleanly and degrade gracefully — the labelled-SAMPLE / offline paths
return the right shape when no NetBird/Portabase token and no deploy-scan URL are configured, and
the vuln-DB permissions demo computes an honest row-scope + column-mask decision from the sample.

Mirrors the existing suite: importlib under PYTHONPATH="apps:$PWD" (PEP-420 namespace package),
no context_runtime / agentic_os / tokens required.
"""
from __future__ import annotations

import importlib

netbird = importlib.import_module("edge-sentinel.netbird")
portabase = importlib.import_module("edge-sentinel.portabase")
deploy_scan = importlib.import_module("edge-sentinel.deploy_scan")
permissions_ui = importlib.import_module("edge-sentinel.permissions_ui")


# ── 1. NetBird — labelled SAMPLE until NETBIRD_API_TOKEN is set ──────────────────────────────
def test_netbird_sample_until_token():
    snap = netbird.snapshot()
    assert snap["live"] is False                       # no token → sample, never live
    assert snap["peers"] is netbird._NB_SAMPLE_PEERS
    assert snap["policies"] is netbird._NB_SAMPLE_POLICIES
    review = netbird.access_review({})
    assert "SAMPLE" in review["text"]                   # the label is preserved
    assert review["data"]["live"] is False
    # the sample intentionally carries an over-broad All→All policy + posture issues to flag
    assert review["data"]["risks"]
    assert any(r["kind"] == "broad_policy" for r in review["data"]["risks"])
    assert "SAMPLE" in netbird.posture({})["text"]


def test_netbird_propose_is_staged_for_approval():
    out = netbird.propose({})
    assert out["status"] == "pending_approval"          # sensitive action never auto-applied
    assert out["draft"]                                  # a concrete tightened draft is returned
    # approving without a reachable NetBird API degrades to an explicit error (no token)
    assert netbird.approve({"draft": out["draft"]})["status"] == "error"


# ── 2. Portabase — labelled SAMPLE until PORTABASE_API_TOKEN is set ──────────────────────────
def test_portabase_sample_until_token():
    snap = portabase.snapshot()
    assert snap["live"] is False
    assert snap["databases"] is portabase._PB_SAMPLE
    status = portabase.backup_status({})
    assert "SAMPLE" in status["text"]
    assert status["data"]["live"] is False
    # the sample has a never-backed-up db and a local-only (no offsite) db → gaps surface honestly
    assert status["data"]["never"] and status["data"]["no_offsite"]


# ── 3. deploy-scan — unavailable until DEPLOY_SCAN_MCP_URL is set ────────────────────────────
def test_deploy_scan_unavailable_when_unset():
    assert deploy_scan.DEPLOY_SCAN_MCP_URL == ""        # unset in the test env
    out = deploy_scan.scan({"target": "example.internal"})
    assert "unavailable" in out["text"].lower()
    # status/results on an unset plane also report unavailable rather than raising
    assert "unavailable" in deploy_scan.status({"job_id": "x"})["text"].lower()
    assert "unavailable" in deploy_scan.results({"job_id": "x"})["text"].lower()
    # a missing target is handled before the plane is even consulted
    assert "target" in deploy_scan.scan({})["text"].lower()


# ── 5. permissions demo — honest row-scope + column-mask over the sample vuln-DB ─────────────
def test_permissions_sample_and_honest_payload():
    rows, source = permissions_ui.fetch_all()
    assert source == "sample"                            # no Doris → the sample set
    assert rows
    # privileged role bypasses to full access
    assert permissions_ui.decide({"role": "security"})["allowed"] is True
    # an analyst scoped to one ecosystem only sees rows in that ecosystem, refs masked
    resp = permissions_ui.query({"limited": {"role": "analyst", "db": True, "table": True,
                                             "scope": ["npm"], "mask": ["refs"]}, "filters": {"q": ""}})
    assert resp["source"] == "sample"
    assert resp["full"]["count"] == resp["total"]        # permissionless sees everything
    lim = resp["limited"]
    assert lim["count"] < resp["total"]                  # scoped view withholds out-of-scope rows
    assert lim["withheld"] == resp["total"] - lim["count"]
    # masked VALUES never leave the server — only the column name is disclosed
    for rec in lim["records"]:
        assert rec["refs"] is None
        assert "refs" in rec["_masked"]
    # a denied grant (no table) yields nothing readable
    denied = permissions_ui.query({"limited": {"role": "analyst", "db": True, "table": False},
                                   "filters": {"q": ""}})
    assert denied["limited"]["decision"]["allowed"] is False
    assert denied["limited"]["count"] == 0


def test_perm_page_is_servable_html():
    assert permissions_ui.PERM_PAGE.lstrip().startswith("<!doctype html>")
    assert "Access Control" in permissions_ui.PERM_PAGE

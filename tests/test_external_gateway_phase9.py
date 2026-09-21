"""Phase 9 — Projects/Sidekick projections + the live deployment-inspection mission.

Offline tests use fixtures shaped like the real Edge Sentinel read endpoints. One live smoke actually
reads sentinel.redevops.io read-only (skipped if unreachable). Nothing here mutates the live SOC.

Run:  uv run python -m pytest tests/test_external_gateway_phase9.py -q
"""
from __future__ import annotations

import pytest

from agentic_os.overlays import Principal
from agentic_os.agent_gateway.external.contracts import AgentCapabilities, CapabilityStatus
from agentic_os.agent_gateway.external.deployment_inspection import (
    build_remediation_request, demo_inspection_mission, derive_findings, run_inspection)
from agentic_os.agent_gateway.external.projections import external_action_view, social_mission_view
from agentic_os.agent_gateway.social.discovery import to_opportunity
from agentic_os.agent_gateway.social.fake_provider import FakeSocialProvider, replay_corpus


# ── fixtures shaped like the real live endpoints ─────────────────────────────────────
_SNAPSHOT = {
    "/health": {"status": "ok", "core": "crowdsec", "connected": True},
    "/api/activity": {"connected": True, "has_threat": True,
                      "kpis": [{"label": "Threats blocked", "value": "5"}],
                      "decisions": [{"value": "45.143.200.14"}, {"value": "185.220.101.34"}]},
    "/api/backups/status": {"total": 5, "ok": 4, "never": ["listmonk (lifecycle)"],
                            "no_offsite": ["lago (billing)"], "unencrypted": []},
    "/api/network/review": {"peers": 4, "connected": 3, "risks": [
        {"kind": "broad_policy", "severity": "high", "detail": "policy Default allows All->All"},
        {"kind": "unapproved_peer", "severity": "high", "detail": "peer contractor-vm pending approval"},
        {"kind": "login_expired", "severity": "medium", "detail": "peer laptop-alex login expired"}]},
}


def _fixture_fetch(base_url, path):
    return _SNAPSHOT.get(path)


# ── deployment inspection (offline) ──────────────────────────────────────────────────
def test_derive_findings_from_real_shapes():
    findings = derive_findings(_SNAPSHOT)
    kinds = {f.kind for f in findings}
    assert {"broad_policy", "unapproved_peer", "login_expired"} <= kinds     # network risks
    assert "no_backup" in kinds and "no_offsite" in kinds                    # backup gaps
    assert "active_threats" in kinds                                         # crowdsec
    assert all(f.evidence_ref.startswith("dpev:") for f in findings)


def test_run_inspection_ranks_top_finding_high_severity():
    report = run_inspection("https://sentinel.redevops.io", fetch=_fixture_fetch)
    assert report.connected is True and report.kpis
    assert report.top_finding.severity == "high"                            # a high-sev risk leads


def test_demo_mission_governs_a_remediation_end_to_end():
    out = demo_inspection_mission("https://sentinel.redevops.io", fetch=_fixture_fetch)
    ga = out["governed_action"]
    assert ga is not None
    assert ga["receipt"]["status"] == "SUCCEEDED"
    assert ga["approval"]["authorized"] and ga["approval"]["decision_id"]
    assert ga["verification"] == "verified"
    # provenance carries the finding evidence, and no provider-private state leaks
    assert ga["evidence_refs"] and ga["evidence_refs"][0].startswith("dpev:")
    assert "cookie" not in str(ga).lower() and "token" not in str(ga).lower()


def test_remediation_request_is_approval_bound_and_carries_evidence():
    findings = derive_findings(_SNAPSHOT)
    req = build_remediation_request(findings[0], principal=Principal(id="u", kind="user", roles=(), tenant="t"),
                                    project_id="p", mission_id="m")
    assert req.capability == "personal_agent.connected_app_action"
    assert req.bounded_context["finding_evidence"] == findings[0].evidence_ref


# ── projections ──────────────────────────────────────────────────────────────────────
def test_social_mission_view_shows_provider_boundary_and_signal_counts():
    caps = FakeSocialProvider().capabilities()
    prov = {"fake-social": caps}
    # Reddit-style available source + an unverified Meta/Muse source
    reddit_caps = AgentCapabilities(provider="reddit", statuses={
        "social.search_public": CapabilityStatus.POLICY_SCOPED})
    muse_caps = AgentCapabilities(provider="meta-muse", statuses={
        "social.search_public": CapabilityStatus.UNKNOWN})
    prov = {"reddit": reddit_caps, "meta-muse": muse_caps}
    opps = [o for o in (to_opportunity(o, product_terms=["context", "rag"]) for o in replay_corpus()) if o]
    view = social_mission_view(observations=len(replay_corpus()), opportunities=opps,
                               market_signal_count=2, provider_capabilities=prov,
                               sources=[("Reddit", "reddit", "social.search_public"),
                                        ("Meta/Muse", "meta-muse", "social.search_public")])
    statuses = {s["source"]: s["status"] for s in view["sources"]}
    assert statuses["Reddit"].startswith("AVAILABLE")
    assert statuses["Meta/Muse"].startswith("UNAVAILABLE")
    # complaint != solution-seeking != purchase-intent, as explicit counts
    o = view["opportunities"]
    assert o["problem_signals"] >= o["solution_seeking"] >= o["commercial_intent_evidence"]
    assert o["unknown_commercial_intent"] >= 1


# ── live read-only smoke (skipped if unreachable) ────────────────────────────────────
def test_live_sentinel_inspection_readonly():
    httpx = pytest.importorskip("httpx")
    try:
        r = httpx.get("https://sentinel.redevops.io/health", timeout=8.0)
    except Exception:
        pytest.skip("sentinel.redevops.io unreachable")
    if r.status_code != 200:
        pytest.skip(f"sentinel.redevops.io /health -> {r.status_code}")
    report = run_inspection("https://sentinel.redevops.io")     # real GET-only reads
    assert report.connected is True
    # the live network review currently surfaces real risks → at least one finding
    assert report.findings, "expected real findings from the live deployment"

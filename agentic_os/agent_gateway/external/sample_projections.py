"""Projects-API views for the Phase 9 cards (Deployment Inspection, Social Intelligence).

These compose the EXISTING builders (:func:`demo_inspection_mission`, :func:`social_mission_view`) into the
exact shapes the Projects UI renders (``DeploymentInspection`` / ``SocialMissionView`` in redevops-projects
``src/data/types.ts``). Nothing is re-derived here: severity, availability, approval and verification come
straight from the builders. The sample data is offline and says so in ``boundary`` / ``data_source``; a
live read happens only when an operator opts in with an explicit inspection target.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from ..social.discovery import to_opportunity
from ..social.fake_provider import replay_corpus
from .contracts import AgentCapabilities, CapabilityStatus
from .deployment_inspection import demo_inspection_mission
from .projections import social_mission_view

_REMEDIATION_BOUNDARY = "simulated (governed fake adapter — the live SOC is never mutated)"

# Shaped like the real Edge Sentinel read endpoints (see tests/test_external_gateway_phase9.py).
SAMPLE_SNAPSHOT: Dict[str, dict] = {
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


def deployment_inspection_view(target: str = "", *, project_id: str = "",
                               fetch: Optional[Callable[[str, str], Optional[dict]]] = None) -> dict:
    """``target`` set → read that deployment live (read-only GETs); empty → the offline sample snapshot."""
    live = bool(target)
    use_fetch = fetch if fetch is not None else (None if live else (lambda _b, p: SAMPLE_SNAPSHOT.get(p)))
    kwargs: Dict[str, Any] = {"fetch": use_fetch}
    if project_id:
        kwargs["project_id"] = project_id
    out = demo_inspection_mission(target or "sample://deployment", **kwargs)
    ins = out["inspection"]
    by_sev: Dict[str, list] = {}
    for f in ins["findings"]:
        by_sev.setdefault(f["severity"], []).append(f)
    return {
        "mission": "Deployment Inspection",
        "target": ins["target"],
        "connected": ins["connected"],
        "boundary": {"inspection": "live" if live else "sample (offline fixture snapshot — no deployment was read)",
                     "remediation": _REMEDIATION_BOUNDARY},
        "kpis": ins["kpis"],
        "findings": ins["findings"],
        "findings_by_severity": by_sev,
        "finding_count": len(ins["findings"]),
        "proposed_actions": ins["proposed_actions"],
        "governed_action": out["governed_action"],
    }


def social_intelligence_view() -> dict:
    """The Social Intelligence surface over the offline replay corpus (no live social provider enabled)."""
    corpus = replay_corpus()
    opps = [o for o in (to_opportunity(o, product_terms=["context", "rag"]) for o in corpus) if o]
    caps = {"reddit": AgentCapabilities(provider="reddit", statuses={
                "social.search_public": CapabilityStatus.POLICY_SCOPED}),
            "meta-muse": AgentCapabilities(provider="meta-muse", statuses={
                "social.search_public": CapabilityStatus.UNKNOWN})}
    view = social_mission_view(observations=len(corpus), opportunities=opps, market_signal_count=2,
                               provider_capabilities=caps,
                               sources=[("Reddit", "reddit", "social.search_public"),
                                        ("Meta/Muse", "meta-muse", "social.search_public")])
    view["data_source"] = "fixture corpus (no live social provider is enabled)"
    return view

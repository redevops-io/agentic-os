"""Golden contract tests for the Projects projections (plan Phase 9).

These freeze the EXACT JSON the Projects UI renders, so the React card can render the projection verbatim
without reproducing backend semantics. The scenarios are the ones a reviewer must see hold:

  inspection: clean/no-findings · mixed severities · Sentinel unavailable
  governed action: approval pending · approval denied · success+verified · verification abstained ·
                   verification refuted (failure)
  social: Reddit policy-scoped (available) · Muse unavailable · the three distinct intent states,
          with UNKNOWN surviving to the projection (never collapsed to false).

Update goldens deliberately with:  UPDATE_GOLDEN=1 uv run python -m pytest tests/test_external_gateway_projection_golden.py
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from agentic_os.overlays import Principal
from agentic_os.agent_gateway.external.contracts import (
    AgentCapabilities, AgentIdentity, AgentPermissionScope, AgentTaskRequest, CapabilityStatus)
from agentic_os.agent_gateway.external.deployment_inspection import demo_inspection_mission
from agentic_os.agent_gateway.external.projections import (
    external_action_view, inspection_mission_view, social_mission_view)
from agentic_os.agent_gateway.external.deployment_inspection import run_inspection
from agentic_os.agent_gateway.social.discovery import to_opportunity
from agentic_os.agent_gateway.social.fake_provider import FakeSocialProvider, replay_corpus

_GOLDEN = Path(__file__).parent / "golden" / "external_gateway"
_PRODUCT = ["context", "memory", "conversation", "retrieval", "rag"]


# ── deterministic inputs ──────────────────────────────────────────────────────────────
def _identity():
    return AgentIdentity(provider="fake-external-agent", adapter_version="v1", instance_id="demo",
                         principal=Principal(id="user:operator", kind="user", roles=(), tenant="redevops"))


def _remediation_request(detail="original"):
    return AgentTaskRequest(
        identity=_identity(), capability="personal_agent.connected_app_action",
        goal="open a remediation ticket: broad_policy — over-broad rule", inputs={"detail": detail},
        bounded_context={"finding_evidence": "dpev:fixed01"},
        permission_scope=AgentPermissionScope(ask_before_capabilities=("personal_agent.connected_app_action",)),
        project_id="proj:redevops-demo", mission_id="mission:inspect-deployments",
        idempotency_key="remediate-dpev:fixed01")


def _receipt(status, *, decision_id="", receipt_id="rcpt_fixed", external_id="ext_fixed"):
    return {"status": status, "external_id": external_id, "receipt_id": receipt_id,
            "decision_id": decision_id}


_CLEAN_SNAPSHOT = {
    "/health": {"status": "ok", "core": "crowdsec", "connected": True},
    "/api/activity": {"connected": True, "has_threat": False, "kpis": [], "decisions": []},
    "/api/backups/status": {"total": 3, "ok": 3, "never": [], "no_offsite": [], "unencrypted": []},
    "/api/network/review": {"peers": 2, "connected": 2, "risks": []},
}
_MIXED_SNAPSHOT = {
    "/health": {"status": "ok", "core": "crowdsec", "connected": True},
    "/api/activity": {"connected": True, "has_threat": True, "kpis": [{"label": "Threats blocked", "value": "5"}],
                      "decisions": [{"value": "45.143.200.14"}, {"value": "185.220.101.34"}]},
    "/api/backups/status": {"total": 5, "ok": 4, "never": ["listmonk"], "no_offsite": ["lago"], "unencrypted": []},
    "/api/network/review": {"peers": 4, "connected": 3, "risks": [
        {"kind": "broad_policy", "severity": "high", "detail": "policy Default allows All->All"},
        {"kind": "login_expired", "severity": "medium", "detail": "peer laptop-alex login expired"}]},
}


_FIXED_NOW = 10 * 86_400_000     # pin the clock so freshness-derived fields are deterministic


def _social_view():
    prov = {
        "reddit": AgentCapabilities(provider="reddit",
                                    statuses={"social.search_public": CapabilityStatus.POLICY_SCOPED}),
        "meta-muse": AgentCapabilities(provider="meta-muse",
                                       statuses={"social.search_public": CapabilityStatus.UNKNOWN}),
    }
    corpus = replay_corpus(now_ms=_FIXED_NOW)
    opps = [o for o in (to_opportunity(o, product_terms=_PRODUCT, now_ms=_FIXED_NOW) for o in corpus) if o]
    return social_mission_view(observations=len(corpus), opportunities=opps,
                               market_signal_count=2, provider_capabilities=prov,
                               sources=[("Reddit", "reddit", "social.search_public"),
                                        ("Meta/Muse", "meta-muse", "social.search_public")])


# ── the frozen scenarios ────────────────────────────────────────────────────────────
def _fetch(snap):
    return lambda base, path: snap.get(path)


SCENARIOS = {
    "inspection_clean_no_findings":
        lambda: demo_inspection_mission("https://sentinel.redevops.io", fetch=_fetch(_CLEAN_SNAPSHOT)),
    "inspection_mixed_severity":
        lambda: demo_inspection_mission("https://sentinel.redevops.io", fetch=_fetch(_MIXED_SNAPSHOT)),
    "inspection_sentinel_unavailable":
        lambda: inspection_mission_view(
            run_inspection("https://sentinel.redevops.io", fetch=lambda b, p: None).view(), None, live=True),
    "action_approval_pending":
        lambda: external_action_view(_remediation_request(), approval_state="pending"),
    "action_approval_denied":
        lambda: external_action_view(_remediation_request(), approval_state="denied"),
    "action_success_verified":
        lambda: external_action_view(_remediation_request(), approval_state="authorized",
                                     decision_id="dec_fixed", receipt=_receipt("SUCCEEDED", decision_id="dec_fixed"),
                                     verification="verified", task_state="succeeded"),
    "action_verification_abstained":
        lambda: external_action_view(_remediation_request(), approval_state="authorized",
                                     decision_id="dec_fixed", receipt=_receipt("HELD", decision_id="dec_fixed"),
                                     verification="abstained", task_state="succeeded"),
    "action_verification_refuted":
        lambda: external_action_view(_remediation_request(), approval_state="authorized",
                                     decision_id="dec_fixed", receipt=_receipt("HELD", decision_id="dec_fixed"),
                                     verification="refuted", task_state="succeeded"),
    "social_reddit_available_muse_unavailable": _social_view,
}

# volatile ids that must not break a golden (structure is what matters).
_VOLATILE = re.compile(r"^(sopp_|task_|atr_|req_|dec_|rcpt_|srcpt_|engp_|draft_)")


def _normalize(obj):
    if isinstance(obj, dict):
        return {k: ("<id>" if k == "opportunity_id" else _normalize(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    if isinstance(obj, str) and _VOLATILE.match(obj):
        return "<id>"
    return obj


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_projection_matches_golden(name):
    produced = _normalize(SCENARIOS[name]())
    path = _GOLDEN / f"{name}.json"
    if os.environ.get("UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(produced, indent=2, sort_keys=True) + "\n")
        pytest.skip(f"updated golden {name}")
    assert path.exists(), f"missing golden {path}; run UPDATE_GOLDEN=1 to create it"
    expected = json.loads(path.read_text())
    assert produced == expected, f"projection drift in {name}"


# ── explicit invariants the goldens must encode (belt-and-suspenders) ────────────────
def test_unknown_commercial_intent_survives_to_projection():
    view = _social_view()
    # a pure complaint / seeking-without-buying must NOT be counted as commercial intent
    assert view["opportunities"]["unknown_commercial_intent"] >= 1
    assert view["opportunities"]["solution_seeking"] >= view["opportunities"]["commercial_intent_evidence"]


def test_provider_boundary_is_explicit_and_not_reconstructed():
    statuses = {s["source"]: s["status"] for s in _social_view()["sources"]}
    assert statuses["Reddit"].startswith("AVAILABLE") and "policy-scoped" in statuses["Reddit"]
    assert statuses["Meta/Muse"].startswith("UNAVAILABLE")


def test_remediation_boundary_is_labeled_simulated():
    out = demo_inspection_mission("https://sentinel.redevops.io", fetch=_fetch(_MIXED_SNAPSHOT))
    assert out["boundary"]["remediation"].startswith("simulated")
    assert out["boundary"]["inspection"] == "fixture"       # fetch injected → not live

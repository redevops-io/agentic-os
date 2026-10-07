"""SidekickOrchestrator — capabilities actually run end-to-end (plan §8/§24)."""
from __future__ import annotations

import pytest

from agentic_os.sidekick import (
    CapabilityRegistry, CapabilityUnavailable, SidekickOrchestrator, SidekickRequest, register_builtin_capabilities,
)
from agentic_os.commercial import Offer, OfferConstraints, bind_commercial_capabilities


def _orc():
    reg = CapabilityRegistry()
    register_builtin_capabilities(reg)
    bind_commercial_capabilities(reg)
    return SidekickOrchestrator(registry=reg)


def test_invoke_runs_bound_capability_and_projects_result():
    orc = _orc()
    res = orc.invoke("acquisition.offer_decide", subject="acme",
                     eligible_offers=[Offer("a", value=10, margin=0.3), Offer("b", value=20, margin=0.3)],
                     constraints=OfferConstraints())
    assert res.capability_id == "acquisition.offer_decide"
    assert res.candidate_actions[0]["offer_id"] == "b"           # projected from OfferDecision
    assert res.answer                                            # human-readable


def test_invoke_unbound_raises():
    reg = CapabilityRegistry(); register_builtin_capabilities(reg)
    orc = SidekickOrchestrator(registry=reg)   # nothing bound
    with pytest.raises(CapabilityUnavailable):
        orc.invoke("sales.deal_close")


def test_handle_routes_intent_to_a_bound_capability():
    orc = _orc()
    req = SidekickRequest(session_id="s", user_message="our demos aren't converting, improve it")
    from agentic_os.commercial import ConversionFunnel, ConversionStage, SurfaceType
    funnel = ConversionFunnel(funnel_id="redevops.io",
                              stages=(ConversionStage("visit", surface_type=SurfaceType.LANDING),
                                      ConversionStage("demo", surface_type=SurfaceType.DEMO)))
    resp = orc.handle(req, inputs={"funnel": funnel, "stage_conversion": {"visit": 0.9, "demo": 0.2}})
    assert "Leakiest stage: demo" in resp.answer                 # funnel_optimize ran


def test_handle_graceful_when_nothing_bound_matches():
    orc = _orc()
    resp = orc.handle(SidekickRequest(session_id="s", user_message="xyzzy nonsense zzz"))
    assert "No runnable capability" in resp.answer


def test_run_parallel_fans_out_and_merges():
    orc = _orc()
    from agentic_os.commercial import ConversionFunnel, ConversionStage, SurfaceType
    funnel = ConversionFunnel(funnel_id="f", stages=(ConversionStage("a"), ConversionStage("b")))
    merged = orc.run_parallel([
        ("acquisition.offer_decide",
         {"subject": "acme", "eligible_offers": [Offer("o1", value=5, margin=0.3)], "constraints": OfferConstraints()}),
        ("acquisition.funnel_optimize", {"funnel": funnel, "stage_conversion": {"a": 0.9, "b": 0.3}}),
    ], session_id="s")
    assert set(merged.contributing_workers) == {"acquisition.offer_decide", "acquisition.funnel_optimize"}
    assert merged.receipt is not None
    assert len(merged.candidate_actions) >= 1

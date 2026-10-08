"""Privacy / egress conformance (plan §7/§9/§10, N7): a private canary never leaves the boundary.

Seeds a CUSTOMER_RESTRICTED "canary" and asserts it is refused on EVERY governed egress path — model
routing (app.llm), the worker-pool split, and tool eligibility — and that inference receipts account
"private records to external: 0". The OPEN-mode counter-cases prove the flag fires honestly when a
deployment deliberately opts out. This is the governance-level canary suite (the apps-side of the
Private Data Plane P1-P3); the full network-capture canary test is a deployment harness on top.
"""
from __future__ import annotations

import pytest

from agentic_os.app.llm import GovernedLLM
from agentic_os.governance.classification import DataClassification as DC
from agentic_os.governance.pools import (
    ENGINEERING_POOL,
    PRIVATE_POOL,
    pool_for,
    worker_may_handle,
)
from agentic_os.governance.policy import PrivacyMode
from agentic_os.governance.receipt import private_records_to_external
from agentic_os.governance.routing import (
    ExecutionBoundary,
    GovernedModelRouter,
    ModelEndpoint,
    RoutingRefused,
)
from agentic_os.governance.tools import ToolSecurityProfile, tool_eligible

CANARY = DC.CUSTOMER_RESTRICTED   # the seeded private canary classification


def _external():
    return ModelEndpoint(model_id="frontier", provider="anthropic",
                         boundary=ExecutionBoundary.EXTERNAL, accepts=DC.SECRET,
                         external_data_processor=True)


def _in_boundary():
    return ModelEndpoint(model_id="local-qwen", provider="selfhost",
                         boundary=ExecutionBoundary.IN_BOUNDARY, accepts=DC.SECRET)


# ── model egress: the canary is refused externally in the safe modes ─────────

@pytest.mark.parametrize("mode", [PrivacyMode.STRICT_PRIVATE,
                                  PrivacyMode.PRIVATE_WITH_ENGINEERING_ASSIST])
def test_canary_never_routes_to_an_external_model(mode):
    # only an external endpoint is available; a private canary must find NO compliant route
    llm = GovernedLLM(GovernedModelRouter([_external()], mode=mode))
    with pytest.raises(RoutingRefused):
        llm.route_only("summarise the customer record", classifications=(CANARY,))


@pytest.mark.parametrize("mode", [PrivacyMode.STRICT_PRIVATE,
                                  PrivacyMode.PRIVATE_WITH_ENGINEERING_ASSIST])
def test_canary_in_boundary_receipt_counts_zero_external(mode):
    llm = GovernedLLM(GovernedModelRouter([_in_boundary(), _external()], mode=mode))
    res = llm.route_only("summarise the customer record", classifications=(CANARY,))
    assert res.endpoint.boundary is ExecutionBoundary.IN_BOUNDARY
    assert res.receipt.crossed_boundary is False
    assert private_records_to_external([res.receipt]) == 0


def test_batch_of_canaries_reports_zero_to_external_in_safe_mode():
    llm = GovernedLLM(GovernedModelRouter([_in_boundary()],
                                          mode=PrivacyMode.PRIVATE_WITH_ENGINEERING_ASSIST))
    receipts = [llm.route_only(f"record {i}", classifications=(CANARY,)).receipt for i in range(10)]
    assert private_records_to_external(receipts) == 0   # "records sent to external LLMs: 0"


def test_open_mode_flags_the_canary_honestly():
    llm = GovernedLLM(GovernedModelRouter([_external()], mode=PrivacyMode.OPEN))
    res = llm.route_only("summarise the customer record", classifications=(CANARY,))
    assert res.receipt.crossed_boundary is True
    assert private_records_to_external([res.receipt]) == 1   # the deliberate opt-out is counted


# ── worker-pool egress: a private canary is a private-pool-only job ──────────

def test_canary_is_a_private_pool_job_only():
    assert pool_for(CANARY) == PRIVATE_POOL.kind
    assert worker_may_handle(PRIVATE_POOL, CANARY) is True
    # an external/engineering worker may NOT handle the canary
    assert worker_may_handle(ENGINEERING_POOL, CANARY) is False


# ── tool egress: an external-processor / payload-logging tool can't touch it ─

def test_canary_refused_by_external_or_logging_tools():
    external_tool = ToolSecurityProfile(tool_id="web_search", network_boundary=ExecutionBoundary.EXTERNAL,
                                        external_data_processor=True)
    logging_tool = ToolSecurityProfile(tool_id="trace_sink", logs_payloads=True)
    assert tool_eligible(external_tool, CANARY) is False
    assert tool_eligible(logging_tool, CANARY) is False
    # an in-boundary, non-logging tool may handle it
    inboundary_tool = ToolSecurityProfile(tool_id="pg_query", network_boundary=ExecutionBoundary.IN_BOUNDARY)
    assert tool_eligible(inboundary_tool, CANARY) is True

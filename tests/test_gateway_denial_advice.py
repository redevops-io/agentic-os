"""A denial stays unexplained, except the one about the caller's own earlier, unresolved call: without a word it reads as
a person's refusal and the caller cannot recover."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from agentic_os.agent_gateway.contracts import DENIAL_ADVICE, GatewayDecision, GatewayResult, GatewayStatus, RiskTier  # noqa: E402
from tests.test_agent_gateway_media import world  # noqa: E402,F401


def test_a_corrected_retry_under_a_spent_key_is_told_what_happened_and_a_new_key_works(world):  # noqa: F811
    w = world
    source = w.ok("media.import", item="film")["asset"]
    bad = [{"id": "s1", "tool": "no-such-step", "params": {}}]
    good = [{"id": "s1", "tool": "trim", "params": {"input": "$film", "start": 0, "end": 3}}]
    first = w.call("media.compose", inputs={"film": source}, steps=bad, idempotency_key="retry-000001")
    assert first["status"] == "error" and "unknown step" in first["error"]
    again = w.call("media.compose", inputs={"film": source}, steps=good, idempotency_key="retry-000001")
    assert again["status"] == "denied" and "NEW idempotency_key" in again["reason"] and "not a person's decision" in again["reason"], again
    assert w.call("media.compose", inputs={"film": source}, steps=good, idempotency_key="retry-000002")["status"] == "ok"


@pytest.mark.parametrize("rule", ["unknown_capability", "authority:invalid_credential", "authority:capability_not_granted", "budget:agent_rate",
                                  "authority:idempotency_key_required", "permission_denied", ""])
def test_every_other_denial_stays_unexplained(rule):
    result = GatewayResult("req_1", GatewayStatus.DENIED, GatewayDecision(allowed=False, reason=rule, risk_tier=RiskTier.READ), error="detail")
    assert result.client_view() == {"status": "denied", "request_id": "req_1"}
    assert set(DENIAL_ADVICE) == {"authority:outcome_unknown"}

from __future__ import annotations

import json
from pathlib import Path

from agentic_os.mission.decision_episode import CONTRACT_VERSION as DECISION_EPISODE_VERSION
from agentic_os.mission.policy_promotion import CONTRACT_VERSION as POLICY_PROMOTION_VERSION


def test_decision_episode_schema_names_canonical_contract_and_required_fields() -> None:
    schema = json.loads(Path("docs/DECISION_EPISODE_SCHEMA.json").read_text(encoding="utf-8"))

    assert schema["properties"]["contract_version"]["const"] == DECISION_EPISODE_VERSION
    assert "selected_option" in schema["required"]
    assert "propensity" in schema["required"]
    assert "readback_ref" in schema["required"]
    assert "outcome_ref" in schema["required"]
    assert schema["properties"]["eligible_options"]["uniqueItems"] is True
    assert {"VERIFIED_SUCCESS", "CORRECT_HOLD", "VERIFIED_FAILURE"} <= set(
        schema["properties"]["oracle_verdict"]["enum"]
    )


def test_policy_promotion_protocol_names_canonical_gates() -> None:
    text = Path("docs/POLICY_PROMOTION_PROTOCOL.md").read_text(encoding="utf-8")

    assert POLICY_PROMOTION_VERSION in text
    for required in (
        "DRAFT -> SHADOW -> EVALUATED -> REQUIRE_REVIEW -> CANARY -> PROMOTED",
        "created_from_episode_digests",
        "effective_sample_size",
        "comparability_status",
        "duplicate_consequential_writes",
        "protected_slice_regressions",
        "REQUIRE_REVIEW",
        "PROMOTE",
        "rollback",
    ):
        assert required in text

from __future__ import annotations

import pytest

from agentic_os_voice.capabilities import default_tools
from agentic_os_voice.contracts import (
    MAX_ARGUMENT_BYTES, ArgSpec, ActionResult, ActionStatus, Reason, ToolSpec, ValidationError, derive_action_id)

TOOLS = default_tools()
UPDATE = TOOLS["update_lead"]
GOOD = {"lead_id": "lead_dana01", "status": "qualified", "budget_band": "10k_50k", "timeline": "now"}


def test_valid_arguments_are_returned_as_a_clean_copy():
    out = UPDATE.validate(GOOD)
    assert out == GOOD and out is not GOOD


@pytest.mark.parametrize("bad,code", [
    ({**GOOD, "owner": "attacker"}, Reason.INVALID_REQUEST),                       # unknown key
    ({k: v for k, v in GOOD.items() if k != "status"}, Reason.INVALID_REQUEST),    # missing required
    ({**GOOD, "status": "closed_won; DROP TABLE"}, Reason.INVALID_REQUEST),        # not in the enum
    ({**GOOD, "lead_id": "../../etc/passwd"}, Reason.INVALID_REQUEST),             # pattern
    ({**GOOD, "lead_id": 5}, Reason.INVALID_REQUEST),                              # type
    ({**GOOD, "need_summary": "x" * 201}, Reason.PAYLOAD_TOO_LARGE),               # per-field bound
    ({**GOOD, "need_summary": "a\x00b"}, Reason.INVALID_REQUEST),                  # control character
    ({**GOOD, "need_summary": "line break"}, Reason.INVALID_REQUEST),
])
def test_invalid_arguments_are_refused_with_a_fixed_code(bad, code):
    with pytest.raises(ValidationError) as e:
        UPDATE.validate(bad)
    assert e.value.code == code


def test_arguments_must_be_an_object():
    for raw in (None, [], "x", 3):
        with pytest.raises(ValidationError):
            UPDATE.validate(raw)


def test_total_payload_is_bounded():
    spec = ToolSpec("big", "x.y", {f"a{i}": ArgSpec("string", max_len=512) for i in range(8)})
    with pytest.raises(ValidationError) as e:
        spec.validate({f"a{i}": "z" * 400 for i in range(8)})
    assert e.value.code == Reason.PAYLOAD_TOO_LARGE and 8 * 400 > MAX_ARGUMENT_BYTES


def test_numbers_reject_booleans_nan_and_out_of_range():
    spec = ToolSpec("n", "x.y", {"n": ArgSpec("integer", minimum=0, maximum=10), "f": ArgSpec("number", required=False)})
    assert spec.validate({"n": 3}) == {"n": 3}
    for bad in ({"n": True}, {"n": 3.5}, {"n": 11}, {"n": 1, "f": float("nan")}, {"n": 1, "f": float("inf")}):
        with pytest.raises(ValidationError):
            spec.validate(bad)


def test_action_id_is_deterministic_per_intent_and_independent_of_the_session():
    a = derive_action_id("t1", "update_lead", GOOD)
    assert a == derive_action_id("t1", "update_lead", dict(reversed(list(GOOD.items()))))      # key order is irrelevant
    assert a != derive_action_id("t2", "update_lead", GOOD)                                    # tenant is part of the intent
    assert a != derive_action_id("t1", "update_lead", {**GOOD, "status": "nurture"})
    assert a != derive_action_id("t1", "lookup_lead", GOOD)


def test_the_tool_schema_forbids_extra_properties_and_lists_required():
    js = UPDATE.json_schema()
    assert js["additionalProperties"] is False and set(js["required"]) == {"lead_id", "status", "budget_band", "timeline"}


def test_only_fixed_sentences_describe_an_outcome():
    r = ActionResult(ActionStatus.PENDING_APPROVAL, Reason.APPROVAL_REQUIRED, "act_1", "corr_1")
    assert "not done yet" in r.say and "receipt_ref" not in r.as_tool_result()
    done = ActionResult(ActionStatus.COMPLETED, Reason.OK, "act_1", "corr_1", receipt_ref="msg_1")
    assert done.as_tool_result()["receipt_ref"] == "msg_1"

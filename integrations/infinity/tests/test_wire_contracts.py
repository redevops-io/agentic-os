"""The wire messages are built strictly from the RAP specification (docs/rap/spec at the pinned commit)."""
import json

import pytest

from redevops_infinity.contracts import (WireError, parse_invocation, parse_notice, subscription_event_body, tool_result_body)
from redevops_infinity.demo.capabilities import default_operations

GOOD = {"operation": "lookup_lead", "arguments": {"lead_id": "lead_dana01"}, "id": "call_1", "call_id": None,
        "callback_url": "http://127.0.0.1:9/cb/x", "group_id": "thread_xyz", "thread_ancestors": ["thread_root", "thread_parent"], "user_id": "user_42"}


def test_the_specification_example_invocation_parses():
    inv = parse_invocation(dict(GOOD))
    assert (inv.operation, inv.id, inv.group_id, inv.thread_ancestors, inv.user_id) == ("lookup_lead", "call_1", "thread_xyz", ("thread_root", "thread_parent"), "user_42")


@pytest.mark.parametrize("mut", [
    {"unexpected": 1}, {"operation": "bad name"}, {"operation": ""}, {"id": ""}, {"id": "x" * 129}, {"group_id": "a b"}, {"arguments": []},
    {"callback_url": ""}, {"thread_ancestors": "thread_root"}, {"thread_ancestors": ["a", "a"]}, {"thread_ancestors": ["thread_xyz"]},
    {"user_id": 5}, {"call_id": "bad id"}])
def test_malformed_invocations_are_refused(mut):
    with pytest.raises(WireError):
        parse_invocation({**GOOD, **mut})


def test_oversize_arguments_are_refused():
    with pytest.raises(WireError):
        parse_invocation({**GOOD, "arguments": {"x": "y" * 9000}})


def test_non_object_bodies_are_refused():
    for raw in ([], "x", 3, None):
        with pytest.raises(WireError):
            parse_invocation(raw)


def test_tool_result_echoes_the_invocation_identifiers():
    b = tool_result_body("g", "call_1", "ok", call_id="p1")
    assert b == {"type": "tool_result", "group_id": "g", "id": "call_1", "text": "ok", "call_id": "p1"}
    assert "subscription" not in b and tool_result_body("g", "c", "t", subscription=True)["subscription"] is True


def test_subscription_event_uses_tool_call_id_and_marks_the_last_one_final():
    e = subscription_event_body("g", "call_1", "{}", final=True)
    assert e == {"type": "subscription_event", "group_id": "g", "tool_call_id": "call_1", "text": "{}", "final": True}
    assert "id" not in e and "final" not in subscription_event_body("g", "c", "{}")


def test_notices_accept_exactly_the_specified_fields():
    assert parse_notice({"thread_id": "t", "tool_call_id": "c"}, "tool_call_id") == ("t", "c")
    assert parse_notice({"thread_id": "t"}, "") == ("t", None)
    for raw, key in (({"thread_id": "t", "x": 1}, ""), ({"thread_id": "t"}, "tool_call_id"), ({"tool_call_id": "c"}, "tool_call_id")):
        with pytest.raises(WireError):
            parse_notice(raw, key)


def test_the_toolset_matches_the_specification_shape():
    for spec in default_operations().values():
        td = spec.tool_def()
        assert set(td) <= {"name", "description", "inputSchema", "annotations", "displayScript"}
        assert td["inputSchema"]["type"] == "object" and td["inputSchema"]["additionalProperties"] is False
        assert 1 <= len(td["name"]) <= 128 and td["description"]
        json.dumps(td)


def test_argument_validation_is_strict():
    ops = default_operations()
    up = ops["update_lead"]
    assert up.validate({"lead_id": "lead_dana01", "status": "qualified"}) == {"lead_id": "lead_dana01", "status": "qualified"}
    for bad in ({"lead_id": "lead_dana01"}, {"lead_id": "x", "status": "qualified"}, {"lead_id": "lead_dana01", "status": "other"},
                {"lead_id": "lead_dana01", "status": "qualified", "extra": 1}, {"lead_id": "lead_dana01", "status": "qualified\x00"},
                {"lead_id": 5, "status": "qualified"}):
        with pytest.raises(WireError):
            up.validate(bad)

"""JSON Schemas (draft 2020-12 subset) for every product tool, and the small validator that enforces them.

The Sidekick capability contract (``agentic_os.sidekick.capability``) names inputs but does not type them. Every
product tool therefore carries an ``input_schema`` here, validated BEFORE any HTTP call, with
``additionalProperties: false`` everywhere: a client can never smuggle a path, URL, command, plan body or credential
through a tool, because an unknown key is a validation error and a string is only ever matched against a fixed
pattern or enum. Schemas are plain Python dicts (not JSON files) so the frozen desktop sidecar carries them with the
module graph.

The validator implements only what the schemas use: type (object, string, integer, number, boolean, array), enum,
pattern, minLength/maxLength, minimum/maximum, required, properties, additionalProperties false, items,
maxItems. It raises :class:`SchemaError` listing every violation (field paths, never values).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping

CASE_ID = r"^case-[0-9a-f]{16}$"
ACTION_ID = r"^act-[0-9a-f]{16}$"
RUN_ID = r"^r[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$"
TARGET_ID = r"^tgt-[0-9a-f]{12}$"
PROFILE_ID = r"^[a-z0-9-]{1,40}$"
DIGEST = r"^sha256:[0-9a-f]{64}$"
CURSOR = r"^[A-Za-z0-9_-]{1,200}$"


class SchemaError(ValueError):
    def __init__(self, problems: List[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def _type_ok(v: Any, t: str) -> bool:
    if t == "object":
        return isinstance(v, dict)
    if t == "string":
        return isinstance(v, str)
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "boolean":
        return isinstance(v, bool)
    if t == "array":
        return isinstance(v, list)
    return False


def _check(v: Any, schema: Mapping[str, Any], path: str, out: List[str]) -> None:
    t = schema.get("type")
    if t and not _type_ok(v, t):
        out.append(f"{path or '$'}: expected {t}")
        return
    if "enum" in schema and v not in schema["enum"]:
        out.append(f"{path or '$'}: not one of the allowed values")
    if isinstance(v, str):
        if "minLength" in schema and len(v) < schema["minLength"]:
            out.append(f"{path or '$'}: too short")
        if "maxLength" in schema and len(v) > schema["maxLength"]:
            out.append(f"{path or '$'}: too long")
        if "pattern" in schema and not re.search(schema["pattern"], v):
            out.append(f"{path or '$'}: wrong format")
        if "\x00" in v:
            out.append(f"{path or '$'}: contains NUL")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if "minimum" in schema and v < schema["minimum"]:
            out.append(f"{path or '$'}: below minimum")
        if "maximum" in schema and v > schema["maximum"]:
            out.append(f"{path or '$'}: above maximum")
    if isinstance(v, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", ()):
            if k not in v:
                out.append(f"{path + '.' if path else ''}{k}: required")
        if schema.get("additionalProperties", True) is False:
            for k in v:
                if k not in props:
                    out.append(f"{path + '.' if path else ''}{k}: not allowed")
        for k, sub in props.items():
            if k in v:
                _check(v[k], sub, f"{path + '.' if path else ''}{k}", out)
    if isinstance(v, list):
        if "maxItems" in schema and len(v) > schema["maxItems"]:
            out.append(f"{path or '$'}: too many items")
        if "items" in schema:
            for i, item in enumerate(v):
                _check(item, schema["items"], f"{path}[{i}]", out)


def validate(value: Any, schema: Mapping[str, Any]) -> None:
    """Raise :class:`SchemaError` unless ``value`` satisfies ``schema``."""
    problems: List[str] = []
    _check(value, schema, "", problems)
    if problems:
        raise SchemaError(problems)


def _obj(props: Mapping[str, Any], required: tuple = ()) -> Dict[str, Any]:
    return {"type": "object", "properties": dict(props), "required": list(required), "additionalProperties": False}


def _s(pattern: str = "", **kw: Any) -> Dict[str, Any]:
    d: Dict[str, Any] = {"type": "string", "maxLength": kw.pop("maxLength", 200)}
    if pattern:
        d["pattern"] = pattern
    d.update(kw)
    return d


_LIMIT = {"type": "integer", "minimum": 1, "maximum": 50}
_IP = {"type": "string", "minLength": 2, "maxLength": 45, "pattern": r"^[0-9A-Fa-f:.]{2,45}$"}

# class: read | stage | present | bounded_write   (decide and execute are NEVER tools)
INPUT_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "security.sentinel_posture": _obj({}),
    "security.sentinel_list_cases": _obj({
        "status": {"type": "string", "enum": ["open", "investigating", "contained", "closed"]},
        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "info"]},
        "q": _s(maxLength=80), "limit": _LIMIT, "cursor": _s(CURSOR)}),
    "security.sentinel_get_case": _obj({"case_id": _s(CASE_ID)}, ("case_id",)),
    "security.sentinel_list_actions": _obj({
        "state": {"type": "string", "enum": ["AWAITING_APPROVAL", "APPROVED", "EXECUTED", "FAILED", "REJECTED"]},
        "limit": _LIMIT, "cursor": _s(CURSOR)}),
    "security.sentinel_propose_ban": _obj({
        "ip": _IP, "duration_hours": {"type": "integer", "minimum": 1, "maximum": 720},
        "reason": _s(maxLength=200, minLength=1)}, ("ip", "reason")),
    "security.sentinel_propose_unban": _obj({"ip": _IP, "reason": _s(maxLength=200, minLength=1)}, ("ip", "reason")),
    "security.sentinel_request_decision": _obj({"request_id": _s(ACTION_ID)}, ("request_id",)),
    "security.sentinel_read_back": _obj({"request_id": _s(ACTION_ID)}, ("request_id",)),
    "quality.qa_info": _obj({}),
    "quality.qa_list_targets": _obj({}),
    "quality.qa_list_profiles": _obj({"target_id": _s(TARGET_ID)}),
    "quality.qa_estimate": _obj({"target_id": _s(TARGET_ID), "profile": _s(PROFILE_ID)}, ("target_id", "profile")),
    "quality.qa_start_run": _obj({
        "target_id": _s(TARGET_ID), "profile": _s(PROFILE_ID), "estimate_digest": _s(DIGEST),
        "defect": {"type": "string", "enum": ["none", "discount", "settings"]}}, ("target_id", "profile")),
    "quality.qa_get_run": _obj({"run_id": _s(RUN_ID)}, ("run_id",)),
    "quality.qa_run_events": _obj({"run_id": _s(RUN_ID), "after": {"type": "integer", "minimum": 0, "maximum": 10 ** 9},
                                   "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, ("run_id",)),
    "quality.qa_cancel_run": _obj({"run_id": _s(RUN_ID)}, ("run_id",)),
    "quality.qa_get_evidence": _obj({"run_id": _s(RUN_ID)}, ("run_id",)),
    "quality.qa_verify": _obj({"run_id": _s(RUN_ID)}, ("run_id",)),
}

# Documented result shapes (informational; the product's own record is the authority). Every consequential tool
# result carries record ids and NEVER ``executed: true``.
_APPROVAL_CARD = _obj({
    "kind": {"type": "string", "enum": ["approval"]}, "product": _s(), "record": {"type": "object"},
    "title": _s(), "consequence": _s(maxLength=500), "read_back": _s(maxLength=500), "digest": _s(DIGEST),
    "open": {"type": "object"}, "decide": {"type": "object"}})

OUTPUT_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "security.sentinel_propose_ban": _obj({"case_id": _s(CASE_ID), "request_id": _s(ACTION_ID),
                                           "state": {"type": "string", "enum": ["AWAITING_APPROVAL"]},
                                           "approval": _APPROVAL_CARD, "executed": {"type": "boolean", "enum": [False]}}),
    "security.sentinel_read_back": _obj({"request_id": _s(ACTION_ID), "state": {"type": "string"},
                                         "detail": _s(maxLength=500), "at": _s(), "receipt_id": _s()}),
    "quality.qa_start_run": _obj({"run_id": _s(RUN_ID), "state": {"type": "string"},
                                  "approval": _APPROVAL_CARD, "executed": {"type": "boolean", "enum": [False]}}),
}

#: tool classes (the product enforces the same classes on its side)
TOOL_CLASS: Dict[str, str] = {
    "security.sentinel_posture": "read", "security.sentinel_list_cases": "read", "security.sentinel_get_case": "read",
    "security.sentinel_list_actions": "read", "security.sentinel_propose_ban": "stage",
    "security.sentinel_propose_unban": "stage", "security.sentinel_request_decision": "present",
    "security.sentinel_read_back": "read",
    "quality.qa_info": "read", "quality.qa_list_targets": "read", "quality.qa_list_profiles": "read",
    "quality.qa_estimate": "read", "quality.qa_start_run": "bounded_write", "quality.qa_get_run": "read",
    "quality.qa_run_events": "read", "quality.qa_cancel_run": "bounded_write", "quality.qa_get_evidence": "read",
    "quality.qa_verify": "read",
}

#: capability ids that must never exist as tools, whatever a registry file declares
FORBIDDEN_TOOLS = frozenset({
    "security.sentinel_decide", "security.sentinel_execute", "security.sentinel_approve", "security.sentinel_retry",
    "security.sentinel_manage_users", "security.sentinel_apply_network_policy",
    "quality.qa_approve_run", "quality.qa_decide", "quality.qa_register_target", "quality.qa_delete_target",
    "quality.qa_execute"})

__all__ = ["INPUT_SCHEMAS", "OUTPUT_SCHEMAS", "TOOL_CLASS", "FORBIDDEN_TOOLS", "SchemaError", "validate",
           "CASE_ID", "ACTION_ID", "RUN_ID", "TARGET_ID", "DIGEST"]

"""A small, stdlib-only validator for the JSON Schema subset the kernel publishes for its tools and capabilities.

It implements only what those schemas use: type (object, string, integer, number, boolean, array), enum, pattern,
minLength/maxLength, minimum/maximum, required, properties, additionalProperties false, items, maxItems. It raises
:class:`SchemaError` listing every violation (field paths, never values).

It lives at the package root, with no third-party import, because the agent gateway validates EVERY call with it: it
once lived in ``agentic_os.products``, whose package needs an HTTP client, so on a base install every gateway call
failed.
"""
from __future__ import annotations

import re
from typing import Any, List, Mapping


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


__all__ = ["SchemaError", "validate"]

"""The closed set of RAP operations the bridge exposes, and how each binds to one Agent Gateway capability.

A RAP ``operation`` is a name in the toolset the bridge serves. Each one is declared here by the owner of the deployment:

* ``capability`` names an Agent Gateway capability. The gateway, not this module, decides whether the principal may use it, within which
  limits, and whether a human must approve. This module only bounds what can be ASKED: names, argument shapes, a deadline.
* there is no pass-through: a name that is not declared is refused (``not_allowlisted``), and no operation maps to an admin or
  management API unless the owner registers such a capability in the gateway and declares it here.
* two operations are served by the bridge itself from its own records and touch no business system (``local=True``): reading the status
  of one's own action and watching it. They return only records created by the same tenant and principal.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from .contracts import MAX_ARGUMENT_BYTES, TOOL_NAME_RE, WireError, canonical_json, sha256_hex

MAX_STRING = 512
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f  ]")
_KINDS = ("string", "integer", "number", "boolean", "enum")


@dataclass(frozen=True)
class Arg:
    kind: str
    required: bool = True
    max_len: int = MAX_STRING
    pattern: str = ""
    choices: Tuple[str, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    description: str = ""

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"unknown argument kind {self.kind!r}")
        if self.kind == "enum" and not self.choices:
            raise ValueError("an enum needs choices")
        if not 1 <= self.max_len <= MAX_STRING:
            raise ValueError(f"max_len must be 1..{MAX_STRING}")

    def json_schema(self) -> dict:
        if self.kind == "enum":
            out: dict = {"type": "string", "enum": list(self.choices)}
        elif self.kind == "string":
            out = {"type": "string", "maxLength": self.max_len}
            if self.pattern:
                out["pattern"] = "^(?:" + self.pattern + ")$"
        else:
            out = {"type": self.kind}
            if self.minimum is not None:
                out["minimum"] = self.minimum
            if self.maximum is not None:
                out["maximum"] = self.maximum
        if self.description:
            out["description"] = self.description
        return out

    def check(self, name: str, v: Any) -> Any:
        k = self.kind
        if k in ("string", "enum"):
            if not isinstance(v, str) or _CONTROL.search(v):
                raise WireError("invalid_request", f"{name} must be a plain string")
            if len(v) > self.max_len:
                raise WireError("invalid_request", f"{name} is too long")
            if k == "enum" and v not in self.choices:
                raise WireError("invalid_request", f"{name} is not an allowed value")
            if k == "string" and self.pattern and not re.fullmatch(self.pattern, v):
                raise WireError("invalid_request", f"{name} has the wrong form")
            return v
        if k == "boolean":
            if not isinstance(v, bool):
                raise WireError("invalid_request", f"{name} must be a boolean")
            return v
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (k == "integer" and not isinstance(v, int)):
            raise WireError("invalid_request", f"{name} has the wrong type")
        if isinstance(v, float) and not math.isfinite(v):
            raise WireError("invalid_request", f"{name} must be finite")
        if (self.minimum is not None and v < self.minimum) or (self.maximum is not None and v > self.maximum):
            raise WireError("invalid_request", f"{name} is out of range")
        return v


@dataclass(frozen=True)
class OperationSpec:
    name: str
    capability: str                          # Agent Gateway capability ("" for a local operation)
    arguments: Mapping[str, Arg]
    description: str = ""
    side_effecting: bool = False
    timeout_s: float = 30.0
    intent_args: Tuple[str, ...] = ()        # arguments that define the business intent (default: all)
    child_allowed: bool = False              # may a derived child thread (Infinity sub-agent) use it
    local: bool = False                      # served from the bridge's own records, no gateway call
    subscription: bool = False               # a local operation that streams events

    def __post_init__(self) -> None:
        if not TOOL_NAME_RE.match(self.name):
            raise ValueError("invalid operation name")
        if not self.local and (not self.capability or " " in self.capability):
            raise ValueError("a non-local operation needs a gateway capability")
        if self.local and self.capability:
            raise ValueError("a local operation has no gateway capability")
        if self.subscription and not self.local:
            raise ValueError("only a local operation can be a subscription")
        if self.local and self.side_effecting:
            raise ValueError("a local operation has no side effect")
        for a in self.intent_args:
            if a not in self.arguments:
                raise ValueError(f"intent arg {a!r} is not an argument")
        if not 0 < self.timeout_s <= 3600:
            raise ValueError("timeout_s must be in (0, 3600]")

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {k: v.json_schema() for k, v in self.arguments.items()},
                "required": [k for k, v in self.arguments.items() if v.required], "additionalProperties": False}

    def tool_def(self) -> dict:
        """spec: basic/toolsets. Names, description and schema are fixed by the owner; the annotation key is namespaced and informational."""
        d = {"name": self.name, "description": self.description or self.name, "inputSchema": self.input_schema(),
             "annotations": {"x-redevops-side-effecting": self.side_effecting}}
        return d

    def validate(self, raw: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise WireError("invalid_request", "arguments must be an object")
        unknown = [k for k in raw if k not in self.arguments]
        if unknown:
            raise WireError("invalid_request", "unknown argument")
        out: Dict[str, Any] = {}
        for name, spec in self.arguments.items():
            if name not in raw or raw[name] is None:
                if spec.required:
                    raise WireError("invalid_request", f"{name} is required")
                continue
            out[name] = spec.check(name, raw[name])
        if len(canonical_json(out)) > MAX_ARGUMENT_BYTES:
            raise WireError("payload_too_large", "arguments are too large")
        return out

    def intent(self, args: Mapping[str, Any]) -> Dict[str, Any]:
        keys = self.intent_args or tuple(args)
        return {k: args[k] for k in keys if k in args}


def derive_action_id(tenant: str, operation: str, intent: Mapping[str, Any]) -> str:
    """Deterministic per business intent. A runtime that lost an acknowledgement and asks again with a NEW tool-call id (the model retried)
    gets the same action id, hence the same gateway idempotency key and the same single effect. It does not depend on the thread or the call id.
    A deliberate second identical action must differ in an intent argument (for example a client-chosen ``request_ref``)."""
    return "act_" + sha256_hex({"v": 1, "tenant": tenant, "op": operation, "intent": dict(intent)})[:32]


def local_operations() -> Dict[str, OperationSpec]:
    aid = Arg("string", pattern=r"act_[0-9a-f]{32}", max_len=40, description="an action id returned earlier")
    return {
        "get_action_status": OperationSpec("get_action_status", "", {"action_id": aid}, local=True, timeout_s=5.0,
                                           description="Read the recorded status of an action requested earlier by this agent system.",
                                           child_allowed=True),
        "watch_action": OperationSpec("watch_action", "", {"action_id": aid}, local=True, subscription=True, timeout_s=5.0,
                                      description="Subscribe to status changes of an action requested earlier; the last event is marked final.",
                                      child_allowed=True),
    }

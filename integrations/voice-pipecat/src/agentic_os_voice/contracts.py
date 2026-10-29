"""Typed contracts of the voice action bridge.

Provider-neutral on purpose: nothing here imports a voice framework. A request describes ONE business
action a voice agent wants performed; a result says what became of it. The only way a result can say
``completed`` is a downstream receipt (see :mod:`agentic_os_voice.bridge`).

The caller's phone number, the transcript and every argument the model chooses are untrusted input.
Identity and tenant come from a :class:`SessionBinding` that the host application creates after its own
authentication step; they are never read from a request, a transcript or a tool argument.
"""
from __future__ import annotations

import enum
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple

CONTRACT_VERSION = "voice-action/v1"

# ── bounds (a request outside them is refused before it reaches the gateway) ─────────────────────────────────────
MAX_ARGUMENT_BYTES = 2048            # canonical JSON of the validated arguments
MAX_STRING_LEN = 512                 # default per-string cap; an ArgSpec may only lower it
MAX_ARGUMENTS = 16
MAX_EVIDENCE_REFS = 8
MAX_REF_LEN = 128
MAX_TOOL_TIMEOUT_S = 60.0
DEFAULT_READ_TIMEOUT_S = 5.0
DEFAULT_WRITE_TIMEOUT_S = 10.0

_ID = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")
_TENANT = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,62}$")
_REF = re.compile(r"^[A-Za-z0-9_.:\-/#]{1,%d}$" % MAX_REF_LEN)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f  ]")


class ActionStatus(str, enum.Enum):
    """What became of an action. ``COMPLETED`` always carries a downstream receipt reference."""
    APPROVED = "approved"                  # authorised and accepted for execution; the downstream receipt has not arrived yet
    DENIED = "denied"                      # refused before any effect
    PENDING_APPROVAL = "pending_approval"  # waiting for a named human; no effect yet
    COMPLETED = "completed"                # executed AND the downstream system returned a receipt
    FAILED = "failed"                      # did not execute, or provably had no effect; safe to ask again
    OUTCOME_UNKNOWN = "outcome_unknown"    # may have executed; never sent again until a person reconciles it


class ConfirmationState(str, enum.Enum):
    """Whether the CALLER was read back the proposed action and agreed. Evidence of consent, never authority."""
    NOT_REQUESTED = "not_requested"
    REQUESTED = "requested"
    CONFIRMED = "confirmed"
    DECLINED = "declined"


class IdentityLevel(enum.IntEnum):
    """How much the host knows about who is on the line. Caller ID alone is ``CLAIMED`` at best."""
    ANONYMOUS = 0
    CLAIMED = 1      # a caller ID or a stated name: untrusted metadata, recorded, never an authentication
    VERIFIED = 2     # the host completed its own verification step for the accounts in ``verified_accounts``


class Reason:
    """The fixed set of reason codes a result can carry (the model and the caller only ever see these)."""
    OK = "ok"
    REPLAYED = "replayed"
    INVALID_REQUEST = "invalid_request"
    NOT_ALLOWLISTED = "not_allowlisted"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    SESSION_UNKNOWN = "session_unknown"
    SESSION_CLOSED = "session_closed"
    SESSION_EXPIRED = "session_expired"
    TENANT_MISMATCH = "tenant_mismatch"
    IDENTITY_UNVERIFIED = "identity_unverified"
    CONFIRMATION_REQUIRED = "confirmation_required"
    HANDOFF_ACTIVE = "handoff_active"
    POLICY_DENIED = "policy_denied"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_UNAVAILABLE = "approval_unavailable"
    CANCELLED_BEFORE_DISPATCH = "cancelled_before_dispatch"
    TIMEOUT = "timeout"
    IN_FLIGHT = "in_flight"
    OUTCOME_UNKNOWN = "outcome_unknown"
    DOWNSTREAM_ERROR = "downstream_error"
    NO_DOWNSTREAM_RECEIPT = "no_downstream_receipt"
    BRIDGE_UNAVAILABLE = "bridge_unavailable"


class ValidationError(ValueError):
    """A request that fails schema validation. ``code`` is one of :class:`Reason`."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


# ── argument schema ──────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ArgSpec:
    """One bounded argument. ``kind`` is string | integer | number | boolean | enum."""
    kind: str
    required: bool = True
    max_len: int = MAX_STRING_LEN
    pattern: str = ""
    choices: Tuple[str, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    description: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("string", "integer", "number", "boolean", "enum"):
            raise ValueError(f"unknown argument kind {self.kind!r}")
        if self.kind == "enum" and not self.choices:
            raise ValueError("an enum argument needs choices")
        if not 1 <= self.max_len <= MAX_STRING_LEN:
            raise ValueError(f"max_len must be 1..{MAX_STRING_LEN}")

    def json_schema(self) -> dict:
        if self.kind == "enum":
            out: dict = {"type": "string", "enum": list(self.choices)}
        elif self.kind == "string":
            out = {"type": "string", "maxLength": self.max_len}
        else:
            out = {"type": self.kind}
        if self.description:
            out["description"] = self.description
        return out

    def check(self, name: str, value: Any) -> Any:
        k = self.kind
        if k in ("string", "enum"):
            if not isinstance(value, str):
                raise ValidationError(Reason.INVALID_REQUEST, f"{name} must be a string")
            if _CONTROL.search(value):
                raise ValidationError(Reason.INVALID_REQUEST, f"{name} contains control characters")
            if len(value) > self.max_len:
                raise ValidationError(Reason.PAYLOAD_TOO_LARGE, f"{name} is longer than {self.max_len}")
            if k == "enum" and value not in self.choices:
                raise ValidationError(Reason.INVALID_REQUEST, f"{name} is not one of the allowed values")
            if k == "string" and self.pattern and not re.fullmatch(self.pattern, value):
                raise ValidationError(Reason.INVALID_REQUEST, f"{name} does not match the required form")
            return value
        if k == "boolean":
            if not isinstance(value, bool):
                raise ValidationError(Reason.INVALID_REQUEST, f"{name} must be true or false")
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(Reason.INVALID_REQUEST, f"{name} must be a number")
        if k == "integer" and not isinstance(value, int):
            raise ValidationError(Reason.INVALID_REQUEST, f"{name} must be an integer")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValidationError(Reason.INVALID_REQUEST, f"{name} must be finite")
        if self.minimum is not None and value < self.minimum or self.maximum is not None and value > self.maximum:
            raise ValidationError(Reason.INVALID_REQUEST, f"{name} is out of range")
        return value


@dataclass(frozen=True)
class ToolSpec:
    """One allowlisted tool: the only description of what a voice agent may ask for.

    ``capability`` names an Agent Gateway capability; the gateway, not this spec, decides whether the caller's
    principal may use it, within which limits, and whether a human must approve. This spec only bounds what can
    be ASKED: names, argument shapes, identity precondition, deadline."""
    name: str
    capability: str
    arguments: Mapping[str, ArgSpec]
    side_effecting: bool = False
    min_identity: IdentityLevel = IdentityLevel.VERIFIED
    account_arg: str = ""                    # the argument naming the account the caller must be verified for
    requires_confirmation: bool = False      # the caller must have confirmed the read-back (consent evidence)
    timeout_s: float = 0.0                   # 0 = the bridge default for reads / writes
    intent_args: Tuple[str, ...] = ()        # arguments that define the business intent (default: all)
    description: str = ""
    allowed_while_handoff: bool = False      # only a handoff tool may run once a human has been requested

    def __post_init__(self) -> None:
        if not _ID.match(self.name) or not self.capability or " " in self.capability:
            raise ValueError(f"invalid tool name or capability for {self.name!r}")
        if len(self.arguments) > MAX_ARGUMENTS:
            raise ValueError("too many arguments")
        if self.account_arg and self.account_arg not in self.arguments:
            raise ValueError("account_arg must be one of the arguments")
        for a in self.intent_args:
            if a not in self.arguments:
                raise ValueError(f"intent arg {a!r} is not an argument")
        if self.timeout_s < 0 or self.timeout_s > MAX_TOOL_TIMEOUT_S:
            raise ValueError(f"timeout_s must be 0..{MAX_TOOL_TIMEOUT_S}")

    def json_schema(self) -> dict:
        return {"type": "object",
                "properties": {k: v.json_schema() for k, v in self.arguments.items()},
                "required": [k for k, v in self.arguments.items() if v.required],
                "additionalProperties": False}

    def validate(self, raw: Mapping[str, Any]) -> dict:
        """Strict: unknown keys, missing required keys, wrong types and oversize payloads are all refused."""
        if not isinstance(raw, Mapping):
            raise ValidationError(Reason.INVALID_REQUEST, "arguments must be an object")
        unknown = [k for k in raw if k not in self.arguments]
        if unknown:
            raise ValidationError(Reason.INVALID_REQUEST, f"unknown argument(s): {sorted(map(str, unknown))[:3]}")
        out: dict = {}
        for name, spec in self.arguments.items():
            if name not in raw or raw[name] is None:
                if spec.required:
                    raise ValidationError(Reason.INVALID_REQUEST, f"{name} is required")
                continue
            out[name] = spec.check(name, raw[name])
        if len(canonical_json(out)) > MAX_ARGUMENT_BYTES:
            raise ValidationError(Reason.PAYLOAD_TOO_LARGE, "arguments are too large")
        return out


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def sha256_hex(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def derive_action_id(tenant: str, tool: str, intent: Mapping[str, Any]) -> str:
    """Deterministic per business intent: the same tenant, tool and intent arguments give the same id on a retry,
    a reconnect, a duplicate webhook delivery or a model that asks twice. It does NOT depend on the session, so
    a second call from the same person about the same thing cannot create a second effect."""
    return "act_" + sha256_hex({"v": 1, "tenant": tenant, "tool": tool, "intent": dict(intent)})[:32]


# ── session ─────────────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SessionBinding:
    """What the HOST knows about a call, established outside the model's reach.

    ``caller_id`` is metadata for the audit trail. It is never used to authenticate or to pick a tenant."""
    session_id: str
    tenant: str
    expires_at: float                        # absolute (the bridge clock); a session never outlives it
    identity: IdentityLevel = IdentityLevel.ANONYMOUS
    verified_accounts: Tuple[str, ...] = ()
    caller_id: str = ""
    subject: str = "voice-agent"             # the service principal the agent acts as at the gateway
    scopes: Tuple[str, ...] = ()
    client_id: str = "voice-pipecat"

    def __post_init__(self) -> None:
        if not _ID.match(self.session_id):
            raise ValueError("invalid session id")
        if not _TENANT.match(self.tenant):
            raise ValueError("invalid tenant")


# ── request / result ───────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ActionRequest:
    session_id: str
    tenant: str
    tool_name: str
    arguments: Mapping[str, Any]
    action_id: str = ""                      # optional; if given it must equal the derived id
    evidence_refs: Tuple[str, ...] = ()
    confirmation_state: ConfirmationState = ConfirmationState.NOT_REQUESTED


def check_evidence_refs(refs: Any) -> Tuple[str, ...]:
    if isinstance(refs, str) or not isinstance(refs, (tuple, list)):
        raise ValidationError(Reason.INVALID_REQUEST, "evidence_refs must be a list")
    if len(refs) > MAX_EVIDENCE_REFS:
        raise ValidationError(Reason.PAYLOAD_TOO_LARGE, "too many evidence references")
    for r in refs:
        if not isinstance(r, str) or not _REF.match(r):
            raise ValidationError(Reason.INVALID_REQUEST, "an evidence reference is malformed")
    return tuple(refs)


#: sentences a voice agent may say about an action, chosen by status ONLY. No other text claims an outcome.
SAY = {
    ActionStatus.COMPLETED: "That is done, and the system returned a confirmation.",
    ActionStatus.PENDING_APPROVAL: "I have asked a team member to review this. It is not done yet, and I cannot say when it will be.",
    ActionStatus.APPROVED: "That has been accepted but I have not received confirmation yet, so I cannot say it is done.",
    ActionStatus.DENIED: "I am not able to do that on this call.",
    ActionStatus.FAILED: "That did not go through. Nothing was changed.",
    ActionStatus.OUTCOME_UNKNOWN: "I cannot confirm whether that went through. A team member will need to check before anything is repeated.",
}


@dataclass(frozen=True)
class ActionResult:
    status: ActionStatus
    reason: str
    action_id: str
    correlation_id: str
    session_id: str = ""
    tool_name: str = ""
    output: Any = None                       # tool output (reads), already through the gateway's egress policy
    receipt_ref: str = ""                    # the downstream system's receipt, required for COMPLETED
    approval_id: str = ""
    replayed: bool = False
    retry_safe: bool = False

    @property
    def say(self) -> str:
        return SAY[self.status]

    def as_tool_result(self) -> dict:
        """What the model receives. Minimal by design: status, a fixed reason code, ids, and the sentence it may use."""
        out: dict = {"status": self.status.value, "reason": self.reason, "action_id": self.action_id,
                     "say": self.say}
        if self.output is not None:
            out["output"] = self.output
        if self.status is ActionStatus.COMPLETED:
            out["receipt_ref"] = self.receipt_ref
        return out

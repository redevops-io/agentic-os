"""Typed contracts of the RAP bridge: the wire messages (built strictly from the RAP specification), statuses and reason codes.

The RAP specification pinned for this package is the one in hydro-project/infinity at the commit recorded in
docs/INFINITY_RAP_BRIDGE_ADR.md. In RAP the agent runtime (Infinity) is the *caller* and this bridge is a *tool server*: the runtime POSTs an
invocation, the bridge acknowledges with HTTP 200 and later POSTs a result to the callback URL the invocation carried.

Nothing the runtime sends is trusted for identity: tenant and principal come from the authenticated peer (callback_auth), never from the
invocation body. ``user_id`` is recorded as a hash for audit and is never used for an authorisation decision.
"""
from __future__ import annotations

import enum
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

CONTRACT_VERSION = "redevops-infinity-rap/v1"
RAP_TOOLSET_PATH = "/.well-known/rap-toolset"
PATH_INVOKE, PATH_CANCEL, PATH_CLOSE = "/invoke", "/cancel_tool_call", "/close_thread"

MAX_BODY_BYTES = 64 * 1024
MAX_ARGUMENT_BYTES = 8 * 1024
MAX_ID_LEN = 128
MAX_ANCESTORS = 16
MAX_URL_LEN = 512
MAX_RESULT_BYTES = 8 * 1024

ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,%d}$" % MAX_ID_LEN)
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


class ActionStatus(str, enum.Enum):
    """What became of an action. ``COMPLETED`` always carries a downstream receipt reference."""
    ACCEPTED = "accepted"                  # journaled, not yet decided
    PENDING_APPROVAL = "pending_approval"  # waiting for a named human in the approvals inbox; no effect yet
    COMPLETED = "completed"                # executed AND the downstream system returned a receipt
    DENIED = "denied"                      # refused before any effect
    FAILED = "failed"                      # did not execute, or provably had no effect
    CANCELLED = "cancelled"                # cancelled before dispatch
    OUTCOME_UNKNOWN = "outcome_unknown"    # may have executed; never sent again until a person reconciles it


TERMINAL = frozenset({ActionStatus.COMPLETED, ActionStatus.DENIED, ActionStatus.FAILED, ActionStatus.CANCELLED})


class Reason:
    OK = "ok"
    REPLAYED = "replayed"
    INVALID_REQUEST = "invalid_request"
    NOT_ALLOWLISTED = "not_allowlisted"
    LINEAGE_REFUSED = "lineage_refused"
    SPAWN_REFUSED = "spawn_refused"
    POLICY_DENIED = "policy_denied"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_EXPIRED = "approval_expired"
    CANCELLED_BEFORE_DISPATCH = "cancelled_before_dispatch"
    TIMEOUT = "timeout"
    OUTCOME_UNKNOWN = "outcome_unknown"
    DOWNSTREAM_ERROR = "downstream_error"
    NO_DOWNSTREAM_RECEIPT = "no_downstream_receipt"
    BRIDGE_UNAVAILABLE = "bridge_unavailable"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    GATEWAY_ERROR = "gateway_error"
    OUTPUT_HELD = "output_held"
    DEADLINE_EXPIRED = "deadline_expired"
    SUBSCRIPTION_REFUSED = "subscription_refused"


class WireError(ValueError):
    """A message that does not match the specification. ``code`` is a short fixed token returned to the peer."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def sha256_hex(data: "bytes | str | Any") -> str:
    if isinstance(data, (bytes, bytearray)):
        return hashlib.sha256(bytes(data)).hexdigest()
    if isinstance(data, str):
        return hashlib.sha256(data.encode()).hexdigest()
    return hashlib.sha256(canonical_json(data)).hexdigest()


def _id(name: str, v: Any, *, optional: bool = False) -> Optional[str]:
    if v is None and optional:
        return None
    if not isinstance(v, str) or not ID_RE.match(v):
        raise WireError("invalid_request", f"{name} must be 1..{MAX_ID_LEN} characters of [A-Za-z0-9_.:-]")
    return v


@dataclass(frozen=True)
class Invocation:
    """RAP tool invocation (spec: basic/tool-invocation). Fields exactly as specified; unknown fields are refused."""
    operation: str
    arguments: Mapping[str, Any]
    id: str
    callback_url: str
    group_id: str
    call_id: Optional[str] = None
    thread_ancestors: Tuple[str, ...] = ()
    user_id: Optional[str] = None


_INVOCATION_KEYS = {"operation", "arguments", "id", "call_id", "callback_url", "group_id", "thread_ancestors", "user_id"}


def parse_invocation(raw: Any) -> Invocation:
    if not isinstance(raw, dict):
        raise WireError("invalid_request", "the body must be a JSON object")
    extra = set(raw) - _INVOCATION_KEYS
    if extra:
        raise WireError("invalid_request", "unknown field(s)")
    op = raw.get("operation")
    if not isinstance(op, str) or not TOOL_NAME_RE.match(op):
        raise WireError("invalid_request", "operation must be a tool name")
    args = raw.get("arguments", {})
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise WireError("invalid_request", "arguments must be an object")
    try:
        if len(canonical_json(args)) > MAX_ARGUMENT_BYTES:
            raise WireError("payload_too_large", "arguments are too large")
    except (TypeError, ValueError):
        raise WireError("invalid_request", "arguments are not JSON") from None
    url = raw.get("callback_url")
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LEN:
        raise WireError("invalid_request", "callback_url is required")
    anc = raw.get("thread_ancestors")
    if anc is None:
        anc = []
    if not isinstance(anc, list) or len(anc) > MAX_ANCESTORS:
        raise WireError("invalid_request", "thread_ancestors must be a short list")
    ancestors = tuple(_id("thread_ancestors[]", a) for a in anc)
    uid = raw.get("user_id")
    if uid is not None and (not isinstance(uid, str) or len(uid) > 256):
        raise WireError("invalid_request", "user_id must be a string")
    gid = _id("group_id", raw.get("group_id"))
    if gid in ancestors or len(set(ancestors)) != len(ancestors):
        raise WireError("invalid_request", "thread_ancestors must be distinct and must not contain group_id")
    return Invocation(operation=op, arguments=args, id=_id("id", raw.get("id")), callback_url=url, group_id=gid,
                      call_id=_id("call_id", raw.get("call_id"), optional=True), thread_ancestors=ancestors, user_id=uid)


def parse_notice(raw: Any, id_key: str) -> Tuple[str, Optional[str]]:
    """``/close_thread`` -> ({"thread_id"}); ``/cancel_tool_call`` -> ({"thread_id", "tool_call_id"})."""
    if not isinstance(raw, dict):
        raise WireError("invalid_request", "the body must be a JSON object")
    allowed = {"thread_id"} | ({id_key} if id_key else set())
    if set(raw) - allowed:
        raise WireError("invalid_request", "unknown field(s)")
    tid = _id("thread_id", raw.get("thread_id"))
    return tid, (_id(id_key, raw.get(id_key)) if id_key else None)


def tool_result_body(group_id: str, call_id_: str, text: str, *, call_id: Optional[str] = None, subscription: bool = False) -> dict:
    """spec: basic/tool-result. ``id`` echoes the invocation id; ``call_id`` is echoed when the invocation had one."""
    body: dict = {"type": "tool_result", "group_id": group_id, "id": call_id_, "text": text}
    if call_id is not None:
        body["call_id"] = call_id
    if subscription:
        body["subscription"] = True
    return body


def subscription_event_body(group_id: str, tool_call_id: str, text: str, *, final: bool = False) -> dict:
    """spec: server/subscription-events. Uses ``tool_call_id`` (not ``id``) so the runtime treats it as an event of an existing subscription."""
    body: dict = {"type": "subscription_event", "group_id": group_id, "tool_call_id": tool_call_id, "text": text}
    if final:
        body["final"] = True
    return body

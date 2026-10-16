"""Operator SDK — how an agentic app becomes a capability operator (a v5 "process").

An app declares its capabilities (syscalls) once and gets, for free:
  * `GET /capabilities`  — the manifest the Mission Planner discovers + reasons over;
  * `POST /invoke`       — run a named capability with server-side idempotency dedupe
                           (exactly-once for side effects, keyed on the Idempotency-Key);
  * a `LocalOperatorClient` so the control plane can drive co-located operators in-process
    (no HTTP hop) while remote apps are driven by `HTTPOperatorClient`.

This is the seam that turns `agents/<app>` (today: an AgentConsole) into a Mission Runtime
operator without changing how the app runs standalone.

Idempotency cache: per-process, bounded LRU (AGENTIC_OS_IDEMPOTENCY_MAX, default 10000). Only results that
are not failure-shaped (``mission.verify.result_failed``) are cached — a failed or raised call re-executes
on retry (a side-effect-free staged ``pending_approval`` result is cached; the approved retry has its own key). With multiple replicas the cache is not shared (see ``approval.SharedStore``).
"""

import hashlib
import logging
import os
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Callable

from .compensation import CompensationSpec, coerce as coerce_compensation
from .types import CapabilityManifest, CapabilitySpec, NodeCost, to_jsonable

Handler = Callable[[dict], dict]


_log = logging.getLogger("agentic_os.operator")


def idempotency_max() -> int:
    try:
        n = int(os.environ.get("AGENTIC_OS_IDEMPOTENCY_MAX", "10000"))
    except ValueError:
        return 10000
    return n if n > 0 else 10000


def _warn_if_no_approval_secret(operator: str) -> None:
    """One startup warning (router mount time) when approved actions cannot be verified."""
    try:
        from .approval import _secret_bytes  # noqa: PLC0415
        if _secret_bytes():
            return
    except Exception:  # noqa: BLE001
        return
    _log.warning(
        "operator %s: AGENTIC_OS_APPROVAL_SECRET is not set, so approval tokens cannot be verified and approved "
        "actions will stay staged (fail closed). Run `agentic-os secrets init` (see docs/SECRETS.md) and give "
        "the same value to the mission runtime and every HTTP operator.", operator)


@dataclass
class Capability:
    """A capability spec bound to its handler."""
    spec: CapabilitySpec
    handler: Handler


def capability(name: str, handler: Handler, *, provides: list[str] | None = None,
               operator: str = "", inputs: dict[str, str] | None = None,
               outputs: dict[str, str] | None = None, side_effecting: bool = False,
               approval_required: bool = False, undo: str | None = None,
               permissions: list[str] | None = None, deterministic: bool = True,
               estimated_value: str = "medium", usd: float = 0.0, latency_ms: int = 0,
               required_authority: list[str] | None = None, isolation_class: str = "",
               network: list[str] | None = None,
               data_classifications: list[str] | None = None,
               concurrency_mode: str = "", concurrency_key: str = "",
               resource_keys: list[str] | None = None,
               max_parallelism: int | None = None,
               compensation: "dict | CompensationSpec | None" = None,
               irreversible: "bool | str" = False,
               mission_owned_temporary: bool = False) -> Capability:
    """Ergonomic builder for one capability + its handler.

    Compensation (docs/COMPENSATION.md, explicit-only): a consequential capability DECLARES either
    ``compensation={"supported": True, "capability": "<id>", "requires_approval": bool, "verify": "<id>",
    "reversibility": "REVERSIBLE_EXPLICIT", "notes": "..."}`` (``undo=`` is the legacy shorthand) or
    ``irreversible="<why it cannot be undone>"``. Nothing is assumed reversible; the conformance lint flags a side-effecting
    capability that declares neither.

    The security fields (v0.3.x, all optional) are the metadata the opt-in Executor security seams read:
    `required_authority` feeds the delegated-authority gate (`authority_for`), `isolation_class` the
    sandbox seam (`isolation_for`), and `network`/`data_classifications` the SecurityMonitor's
    boundary telemetry. Declaring nothing leaves the capability unchanged."""
    comp = coerce_compensation(compensation)
    irr_note = irreversible.strip() if isinstance(irreversible, str) else ""
    irreversible = bool(irreversible)       # `irreversible="why"` carries the mandatory note
    if comp is not None and comp.supported and not undo:
        undo = comp.capability        # one source of truth: the declared compensating capability
    spec = CapabilitySpec(
        name=name, operator=operator, provides=provides or [], inputs=inputs or {},
        outputs=outputs or {}, side_effecting=side_effecting, approval_required=approval_required,
        undo=undo, compensation=comp, irreversible=irreversible, irreversible_note=irr_note,
        mission_owned_temporary=bool(mission_owned_temporary),
        permissions=permissions or [], deterministic=deterministic,
        estimated_value=estimated_value, cost=NodeCost(usd=usd, latency_ms=latency_ms),
        required_authority=required_authority or [], isolation_class=isolation_class,
        network=network or [], data_classifications=data_classifications or [],
        concurrency_mode=concurrency_mode, concurrency_key=concurrency_key,
        resource_keys=resource_keys or [], max_parallelism=max_parallelism,
    )
    return Capability(spec=spec, handler=handler)


def pending_approval(action: str, **preview) -> dict:
    """The uniform 'staged, awaiting approval' result a side-effecting handler returns when it is invoked
    WITHOUT a runtime-delivered ``_approval``. A consequential capability must perform its real side effect
    ONLY after a genuine approval (the resumed Mission node carries ``_approval``; the open HTTP /invoke
    strips forged markers), so on the ungoverned path it stages this instead of acting. ``preview`` carries
    the declared-output marker set to a falsey value (so the N4 read-back still fails) plus any safe context."""
    return {"status": "pending_approval", "action": action, "executed": False, **preview}


def idempotency_wait_s() -> float:
    """How long a duplicate call waits for the in-flight one (``AGENTIC_OS_IDEMPOTENCY_WAIT_S``, default 300)."""
    try:
        return float(os.environ.get("AGENTIC_OS_IDEMPOTENCY_WAIT_S", "300"))
    except ValueError:
        return 300.0


class Operator:
    """One app's capability operator: manifest + handlers + idempotency dedupe."""

    def __init__(self, name: str, capabilities: list[Capability]):
        self.name = name
        for c in capabilities:
            c.spec.operator = c.spec.operator or name
        self.manifest = CapabilityManifest(operator=name, capabilities=[c.spec for c in capabilities])
        self._handlers: dict[str, Handler] = {c.spec.name: c.handler for c in capabilities}
        self._specs: dict[str, CapabilitySpec] = {c.spec.name: c.spec for c in capabilities}
        # (capability, inputs-hash, idem key) -> first SUCCESSFUL result (exactly-once). PER-PROCESS, bounded LRU.
        self._seen: "OrderedDict[tuple, dict]" = OrderedDict()
        self._seen_lock = threading.Lock()
        from .approval import ReplayGuard  # noqa: PLC0415
        self.replay_guard = ReplayGuard()      # SEAM: per-process; multi-replica deployments need a shared store
        self.calls: "deque[tuple[str, str]]" = deque(maxlen=10_000)  # (capability, idempotency_key) — recent, for assertions
        self._inflight: dict[tuple, dict] = {}   # dedupe key -> {"done": Event, "result": ..., "ok": bool}

    def invoke(self, capability: str, inputs: dict, idempotency_key: str = "",
               *, secrets: "dict | None" = None) -> dict:
        from .approval import canonical  # noqa: PLC0415
        # Dedupe on (capability, canonical inputs, key): the same key with different capability/inputs is a
        # different call. `_approval` stays IN the hash so an approved retry is never served the staged result.
        try:
            # the caller's ephemeral credential material scopes the key: two tenants never share a result
            _ctx = hashlib.sha256(canonical(secrets or {})).hexdigest() if secrets else ""
            _dk = (capability, canonical({k: v for k, v in (inputs or {}).items()
                                          if k not in {"_mission", "_goal", "_node"}}), idempotency_key, _ctx)
        except Exception:  # noqa: BLE001
            _dk = None
        if not (idempotency_key and _dk is not None):
            return self._execute(capability, inputs, idempotency_key, _dk, secrets)
        # Per-key in-flight claim: concurrent identical calls wait for the first and share its outcome, so the
        # handler runs once. If the first call raised, waiters retry (a failure is never cached).
        while True:
            with self._seen_lock:
                if _dk in self._seen:
                    self._seen.move_to_end(_dk)
                    return self._seen[_dk]
                slot = self._inflight.get(_dk)
                leader = slot is None
                if leader:
                    slot = self._inflight[_dk] = {"done": threading.Event(), "ok": False, "result": None}
            if leader:
                break
            if not slot["done"].wait(idempotency_wait_s()):
                raise TimeoutError(f"{capability}: an identical call is still in flight")
            if slot["ok"]:
                return slot["result"]
        try:
            result = self._execute(capability, inputs, idempotency_key, _dk, secrets)
            slot["result"], slot["ok"] = result, True
            return result
        finally:
            with self._seen_lock:
                self._inflight.pop(_dk, None)
            slot["done"].set()

    def _execute(self, capability: str, inputs: dict, idempotency_key: str, _dk, secrets: "dict | None") -> dict:
        fn = self._handlers.get(capability)
        if fn is None:
            raise KeyError(f"operator '{self.name}' has no capability '{capability}'")
        self.calls.append((capability, idempotency_key))
        # A handler that declares a second parameter receives the ephemeral redeemed credential material
        # (mirrors InMemoryOperatorClient). `secrets` is never persisted — the dedupe cache holds the
        # result only — so a capability can use a broker-issued credential without it entering any log.
        import inspect  # noqa: PLC0415
        try:
            arity = len(inspect.signature(fn).parameters)
        except (TypeError, ValueError):
            arity = 1
        result = (fn(inputs, secrets or {}) if arity >= 2 else fn(inputs)) or {}
        # Read-back contract (N4) — see below.
        spec = self._specs.get(capability)
        outs = list((getattr(spec, "outputs", None) or {}).keys()) if spec else []
        # Compatibility markers are synthesised ONLY for successful NON-side-effecting reads. A side-effecting
        # capability never receives a synthesised output key: it must return its own proof of the write (a real
        # read-back), otherwise the Mission verifier reads the declared key back as ABSENT and REJECTS. A result
        # that signals failure / denial / pending is likewise left as-is, so a LAPI 403 or a failed write can
        # never be minted into `{key: True}` and read back as success.
        # The failure classifier is shared with the Mission verifier (mission.verify.result_failed) so the two
        # can never disagree about what a failure looks like.
        from .verify import result_failed  # noqa: PLC0415
        _failed = result_failed(result, side_effecting=bool(getattr(spec, "side_effecting", False)),
                                declares_outputs=bool(outs))[0]
        can_synthesize_marker = bool(spec and not getattr(spec, "side_effecting", False))
        if (can_synthesize_marker and isinstance(result, dict) and result and outs
                and not _failed and not any(k in result for k in outs)):
            result = {**result, **{k: True for k in outs}}
        # Cache ONLY successes: a failed / staged (pending_approval) / empty-write result must not pin the key,
        # so a retry after the backend recovers (or after approval) re-executes.
        # A staged ("pending_approval") result is side-effect-free and keyed WITHOUT `_approval`, so caching it
        # cannot block the approved retry (which carries `_approval`, hence a different key) and keeps staging
        # idempotent. Every other failure-shaped result is left uncached.
        from .verify import is_staged  # noqa: PLC0415
        if idempotency_key and _dk is not None and (not _failed or is_staged(result)):
            cap = idempotency_max()
            with self._seen_lock:
                self._seen[_dk] = result
                self._seen.move_to_end(_dk)
                while len(self._seen) > cap:
                    self._seen.popitem(last=False)
        return result

    # ── HTTP surface (mount into the app's FastAPI) ──────────────────────────
    def router(self):
        from fastapi import APIRouter, Header, HTTPException
        from pydantic import BaseModel

        _warn_if_no_approval_secret(self.name)
        router = APIRouter(tags=[f"operator:{self.name}"])

        class InvokeBody(BaseModel):
            capability: str
            inputs: dict = {}
            idempotency_key: str = ""
            mission_id: str | None = None
            node_id: str | None = None
            approval_token: str | None = None

        @router.get("/capabilities")
        def capabilities():
            return to_jsonable(self.manifest)

        @router.post("/invoke")
        def invoke(body: InvokeBody, idempotency_key: str | None = Header(default=None)):
            key = body.idempotency_key or idempotency_key or ""
            # The HTTP /invoke is the UNTRUSTED "try" surface, so strip runtime-only approval markers a caller
            # could forge to bypass a gate: a direct POST of `{"_approval": true}` must NOT make a consequential
            # handler execute (it stages / stays pending instead). The runtime delivers a real approval through
            # the trusted in-process invoke (co-located) or an authenticated internal channel — never this open
            # endpoint — so legitimate approval flows are unaffected.
            from .approval import strip_approval_keys, verify_approval_token  # noqa: PLC0415
            inputs = strip_approval_keys(body.inputs or {})
            # A runtime-delivered approval arrives ONLY as a signed, operator/capability/inputs-bound,
            # single-use token. Verified => inject the marker AFTER the strip. Anything else => ignored (stages).
            if body.approval_token:
                marker = verify_approval_token(None, body.approval_token, operator=self.name,
                                               capability=body.capability, inputs=inputs,
                                               mission_id=body.mission_id, node_id=body.node_id,
                                               replay_guard=self.replay_guard)
                if marker is not None:
                    inputs["_approval"] = marker
                else:
                    _warn_unverified_approval()
            try:
                return {"result": self.invoke(body.capability, inputs, key)}
            except KeyError as e:
                raise HTTPException(404, str(e))

        return router


_WARN_AT: dict = {}


def _warn_unverified_approval() -> None:
    """Rate-limited (1/min per reason) WARNING: an approval_token did not verify, so approved actions stay
    staged. Never logs the token or the secret."""
    import logging, time  # noqa: PLC0415
    from .approval import ENV_SECRET, _secret_bytes  # noqa: PLC0415
    reason = "mismatch" if _secret_bytes() else "no-secret"
    now = time.monotonic()
    if now - _WARN_AT.get(reason, -1e9) < 60:
        return
    _WARN_AT[reason] = now
    why = ("approval token failed verification (secret mismatch, expired, replayed or tampered)"
           if reason == "mismatch" else f"{ENV_SECRET} is not configured on this operator")
    logging.getLogger("agentic_os.operator").warning(
        "%s; approved actions will stay staged. Run `agentic-os secrets init` and use the SAME "
        "%s on the runtime and every operator (docs/SECRETS.md).", why, ENV_SECRET)


class LocalOperatorClient:
    """An `OperatorClient` over in-process Operator objects — the control plane driving
    co-located operators without an HTTP hop. Remote apps use HTTPOperatorClient instead."""

    def __init__(self, operators: dict[str, Operator]):
        self._ops = operators

    def invoke(self, operator: str, capability: str, inputs: dict, idempotency_key: str,
               *, secrets: "dict | None" = None) -> dict:
        op = self._ops.get(operator)
        if op is None:
            raise KeyError(f"no operator '{operator}' registered")
        return op.invoke(capability, inputs, idempotency_key, secrets=secrets)

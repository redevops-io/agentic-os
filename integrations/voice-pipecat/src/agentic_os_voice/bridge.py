"""VoiceActionBridge: the one door from a voice conversation to a side effect.

A voice framework (Pipecat here) owns the conversation: turns, interruptions, speech. It does NOT own
authority. Whatever a model decides to ask for crosses this bridge as a typed :class:`ActionRequest`, and the
bridge hands an accepted request to the existing Agent Gateway, which already provides the capability
registry, permission check, authority envelope, budget, approval gate, idempotency, egress policy and audit.
This module adds only what the voice setting needs on top of that path:

* a closed tool allowlist with strict, bounded argument schemas (:class:`~agentic_os_voice.contracts.ToolSpec`);
* session binding: tenant and identity come from the host, never from the model, the transcript or caller ID;
* a deterministic action id per business intent, used as the gateway idempotency key;
* a durable, hash-chained journal of intents and outcomes (the repository's own ``EventStore``);
* cancellation that never abandons a committed write, and a replay that never repeats one;
* a result that can only say ``completed`` when the downstream system returned a receipt.

It is not a second permission system: it never grants anything. Every decision about whether an action may
run is the gateway's; this module only refuses requests that are malformed, out of the allowlist, or made by a
session that is not entitled to ask.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from agentic_os.agent_gateway import (
    AgentGateway, EgressAction, GatewayPrincipal, GatewayRequest, GatewayStatus)
from agentic_os.overlays import Principal

from .contracts import (
    DEFAULT_READ_TIMEOUT_S, DEFAULT_WRITE_TIMEOUT_S, ActionRequest, ActionResult, ActionStatus, ConfirmationState,
    IdentityLevel, Reason, SessionBinding, ToolSpec, ValidationError, canonical_json, check_evidence_refs,
    derive_action_id, sha256_hex)

_log = logging.getLogger("agentic_os_voice.bridge")

MAX_OUTPUT_BYTES = 4096
STREAM_PREFIX = "voice-actions:"           # one hash-chained stream per tenant in the journal
EV_SUBMITTED = "VoiceActionSubmitted"
EV_DECIDED = "VoiceActionDecided"
EV_REFUSED = "VoiceActionRefused"
EV_RECONCILED = "VoiceActionReconciled"
EV_REPLAYED = "VoiceActionReplayed"
EV_HANDOFF = "VoiceHandoffRequested"
EV_SESSION = "VoiceSession"


class ApprovalTap:
    """Wraps the gateway's ApprovalStore so the bridge can tell a person which approval belongs to which action.

    It decides nothing: ``is_satisfied`` returns exactly what the wrapped store returns, and an exception from the
    store propagates (the gateway then fails closed). It only records the inbox item the call parked, and whether
    the store was unreachable for a request."""

    def __init__(self, inner: Any):
        self.inner = inner
        self._lock = threading.RLock()
        self._by_fp: Dict[str, str] = {}
        self._failed: set = set()

    def is_satisfied(self, request: GatewayRequest, manifest: Any) -> bool:
        with self._lock:
            pending = getattr(self.inner, "pending", None)
            before = {p["id"] for p in pending()} if pending else set()
            try:
                ok = self.inner.is_satisfied(request, manifest)
            except BaseException:
                self._failed.add(request.request_id)
                raise
            if not ok and pending:
                now = pending()
                new = [p for p in now if p["id"] not in before]
                cands = new or [p for p in now if p["capability"] == request.capability
                                and p["subject"] == request.principal.subject and p["tenant"] == request.principal.tenant]
                if cands:
                    self._by_fp[request.intent_hash()] = cands[-1]["id"]
            return ok

    def approval_id_for(self, request: GatewayRequest) -> str:
        with self._lock:
            return self._by_fp.get(request.intent_hash(), "")

    def was_unavailable(self, request_id: str) -> bool:
        with self._lock:
            return request_id in self._failed


@dataclass
class _Pending:
    binding: SessionBinding
    spec: ToolSpec
    arguments: dict
    action_id: str
    evidence_refs: Tuple[str, ...]
    confirmation: ConfirmationState
    approval_id: str


class VoiceActionBridge:
    """See the module docstring. Thread-safe; the synchronous core is run off the event loop by ``submit_async``."""

    def __init__(self, gateway: AgentGateway, tools: Mapping[str, ToolSpec], journal: Any, *,
                 approvals: Optional[ApprovalTap] = None, clock: Callable[[], float] = time.time,
                 allow_unenforced_authority: bool = False, max_sessions: int = 256):
        if gateway.authority is None and not allow_unenforced_authority:
            raise ValueError(
                "the voice bridge requires a gateway with an authority guard; pass allow_unenforced_authority=True "
                "only for a local trial (authority is then NOT enforced and an unknown outcome cannot be detected)")
        self.gateway = gateway
        self.tools: Dict[str, ToolSpec] = dict(tools)
        self.journal = journal
        self.approvals = approvals
        self._clock = clock
        self._max_sessions = max_sessions
        self._lock = threading.RLock()
        self._sessions: Dict[str, SessionBinding] = {}
        self._closed: set = set()
        self._handoff: set = set()
        self._action_locks: Dict[str, threading.Lock] = {}
        self._live: set = set()                         # action ids with an attempt running in THIS process
        self._pending: Dict[str, _Pending] = {}
        self._tasks: set = set()
        self._index: Dict[str, dict] = {}
        for ev in journal.all():
            self._fold(ev)
        journal.subscribe(self._fold)

    # ── journal fold (the receipt of an action is the fold of its events) ────────────────────────────────────────
    def _fold(self, ev: Any) -> None:
        if not str(ev.mission_id).startswith(STREAM_PREFIX):
            return
        p = ev.payload or {}
        aid = p.get("action_id")
        if not aid:
            return
        with self._lock:
            rec = self._index.setdefault(aid, {"action_id": aid, "events": 0, "attempts": 0})
            rec["events"] += 1
            if ev.type == EV_SUBMITTED:
                rec.update(tenant=p["tenant"], session_id=p["session_id"], tool=p["tool"], capability=p["capability"],
                           side_effecting=p["side_effecting"], args_sha256=p["args_sha256"], state="in_flight",
                           last_correlation=p["correlation_id"], evidence_refs=p.get("evidence_refs", []),
                           identity=p.get("identity"), confirmation=p.get("confirmation"))
                rec["attempts"] += 1
            elif ev.type in (EV_DECIDED, EV_REFUSED, EV_RECONCILED):
                for k in ("status", "reason", "approval_id", "gateway_request_id", "audit_ref", "receipt_ref", "retry_safe"):
                    if k in p:
                        rec[k] = p[k]
                rec.setdefault("tenant", p.get("tenant"))
                rec.setdefault("session_id", p.get("session_id"))
                rec.setdefault("tool", p.get("tool"))
                rec["state"] = "decided"
                rec["last_correlation"] = p.get("correlation_id", rec.get("last_correlation"))
                rec["seq"] = ev.seq
                rec["ts"] = ev.ts
                if ev.type == EV_RECONCILED:
                    rec["reconciled_by"] = p.get("actor")

    def _append(self, etype: str, tenant: str, payload: dict) -> None:
        self.journal.append(etype, STREAM_PREFIX + tenant, payload)

    def receipt(self, action_id: str) -> Optional[dict]:
        """The durable receipt of an action: what was asked (by hash), what became of it, and the references that let
        a person check both. Contains no argument values and no credentials."""
        with self._lock:
            rec = self._index.get(action_id)
            if rec is None:
                return None
            out = dict(rec)
        out["status"], out["reason"] = self._effective_status(out)
        out["journal_head"] = self.journal.head(STREAM_PREFIX + str(out.get("tenant"))) if hasattr(self.journal, "head") else ""
        return out

    def _effective_status(self, rec: dict) -> Tuple[str, str]:
        if rec.get("state") == "in_flight":
            if rec["action_id"] in self._live:
                return ActionStatus.APPROVED.value, Reason.IN_FLIGHT
            # An intent was journaled and nothing decided it, and no thread of this process is running it: the process died (or
            # the attempt was abandoned) at the write boundary. The gateway's authority ledger knows how far the attempt got.
            if not rec.get("side_effecting"):
                return ActionStatus.FAILED.value, Reason.BRIDGE_UNAVAILABLE
            lease = self.gateway.authority.lease(rec.get("last_correlation", "")) if self.gateway.authority is not None else None
            state = (lease or {}).get("state", "none" if self.gateway.authority is not None else "unknown")
            if state in ("none", "reserved", "released"):
                return ActionStatus.FAILED.value, Reason.BRIDGE_UNAVAILABLE         # it never left: asking again is safe
            ref = ((lease or {}).get("meta") or {}).get("provider_ref")
            if state == "settled" and ref:
                rec["receipt_ref"] = str(ref)                                       # it executed and the ledger kept the downstream receipt
                return ActionStatus.COMPLETED.value, Reason.OK
            return ActionStatus.OUTCOME_UNKNOWN.value, Reason.OUTCOME_UNKNOWN
        return rec.get("status", ActionStatus.FAILED.value), rec.get("reason", Reason.BRIDGE_UNAVAILABLE)

    # ── sessions ─────────────────────────────────────────────────────────────────────────────────────────────────
    def open_session(self, binding: SessionBinding) -> "ActionProxy":
        """Called by the host after ITS authentication step. Idempotent for the same call id and the same binding."""
        with self._lock:
            have = self._sessions.get(binding.session_id)
            if have is not None:
                if have.tenant != binding.tenant or have.subject != binding.subject:
                    raise ValueError("a session cannot be re-bound to another tenant or subject")
                return ActionProxy(self, binding.session_id)
            if binding.session_id in self._closed:
                raise ValueError("this session is closed")
            if len(self._sessions) >= self._max_sessions:
                raise ValueError("too many open sessions")
            self._sessions[binding.session_id] = binding
        self._append(EV_SESSION, binding.tenant, {"action_id": "", "event": "opened", "session_id": binding.session_id,
                                                   "identity": int(binding.identity),
                                                   "caller_id_sha256": sha256_hex(binding.caller_id)[:16] if binding.caller_id else ""})
        return ActionProxy(self, binding.session_id)

    def mark_verified(self, session_id: str, accounts: Tuple[str, ...], *, method: str) -> None:
        """Host-only (not reachable through a proxy): the host completed its own verification for these accounts."""
        with self._lock:
            b = self._sessions[session_id]
            from dataclasses import replace
            self._sessions[session_id] = replace(b, identity=IdentityLevel.VERIFIED,
                                                 verified_accounts=tuple(sorted(set(b.verified_accounts) | set(accounts))))
        self._append(EV_SESSION, b.tenant, {"action_id": "", "event": "verified", "session_id": session_id, "method": str(method)[:64],
                                             "accounts": len(accounts)})

    def close_session(self, session_id: str) -> None:
        with self._lock:
            b = self._sessions.pop(session_id, None)
            self._closed.add(session_id)               # a hand-off request outlives the call: it still blocks a parked action
        if b is not None:
            self._append(EV_SESSION, b.tenant, {"action_id": "", "event": "closed", "session_id": session_id})

    def binding(self, session_id: str) -> SessionBinding:
        with self._lock:
            b = self._sessions.get(session_id)
        if b is None:
            raise ValidationError(Reason.SESSION_CLOSED if session_id in self._closed else Reason.SESSION_UNKNOWN, "no such open session")
        if not (self._clock() < b.expires_at):
            raise ValidationError(Reason.SESSION_EXPIRED, "the session has expired")
        return b

    def handoff_active(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._handoff

    # ── the request path ─────────────────────────────────────────────────────────────────────────────────────────
    def _refuse(self, request: ActionRequest, code: str, corr: str, *, tenant: str = "", action_id: str = "",
                tool: str = "", status: ActionStatus = ActionStatus.DENIED, retry_safe: bool = True) -> ActionResult:
        res = ActionResult(status, code, action_id, corr, request.session_id, tool, retry_safe=retry_safe)
        tenant = tenant or "unbound"
        try:
            self._append(EV_REFUSED, tenant, {"action_id": action_id or "refused:" + corr, "correlation_id": corr, "tenant": tenant,
                                              "session_id": request.session_id, "tool": tool or "", "status": status.value,
                                              "reason": code, "retry_safe": retry_safe})
        except Exception:  # noqa: BLE001 - a refusal stays a refusal when the journal is down
            _log.warning("could not journal a refusal (correlation %s)", corr)
        return res

    def prepare(self, request: ActionRequest, corr: str) -> Tuple[Optional[ActionResult], Optional[tuple]]:
        """Everything that can be decided without the gateway. Returns (refusal, None) or (None, (binding, spec, args, action_id))."""
        tool = request.tool_name if isinstance(request.tool_name, str) else ""
        try:
            binding = self.binding(request.session_id)
            if request.tenant != binding.tenant:
                return self._refuse(request, Reason.TENANT_MISMATCH, corr, tenant=binding.tenant, tool=tool), None
            spec = self.tools.get(tool)
            if spec is None:
                return self._refuse(request, Reason.NOT_ALLOWLISTED, corr, tenant=binding.tenant, tool=tool[:64]), None
            args = spec.validate(request.arguments)
            check_evidence_refs(request.evidence_refs)
            intent = {k: args[k] for k in (spec.intent_args or tuple(args)) if k in args}
            action_id = derive_action_id(binding.tenant, spec.name, intent)
            if request.action_id and request.action_id != action_id:
                return self._refuse(request, Reason.INVALID_REQUEST, corr, tenant=binding.tenant, tool=tool), None
            if self.handoff_active(request.session_id) and not spec.allowed_while_handoff:
                return self._refuse(request, Reason.HANDOFF_ACTIVE, corr, tenant=binding.tenant, action_id=action_id, tool=tool), None
            if binding.identity < spec.min_identity or (
                    spec.account_arg and args[spec.account_arg] not in binding.verified_accounts
                    and spec.min_identity >= IdentityLevel.VERIFIED):
                return self._refuse(request, Reason.IDENTITY_UNVERIFIED, corr, tenant=binding.tenant, action_id=action_id, tool=tool), None
            if spec.requires_confirmation and request.confirmation_state is not ConfirmationState.CONFIRMED:
                return self._refuse(request, Reason.CONFIRMATION_REQUIRED, corr, tenant=binding.tenant, action_id=action_id, tool=tool), None
            return None, (binding, spec, args, action_id)
        except ValidationError as e:
            tenant = ""
            try:
                tenant = self._sessions[request.session_id].tenant
            except KeyError:
                pass
            return self._refuse(request, e.code, corr, tenant=tenant, tool=tool[:64]), None

    def submit(self, request: ActionRequest, *, cancel: Optional[threading.Event] = None) -> ActionResult:
        """Synchronous core. Safe to call from any thread."""
        corr = "corr_" + uuid.uuid4().hex[:20]
        refusal, ok = self.prepare(request, corr)
        if refusal is not None:
            return refusal
        binding, spec, args, action_id = ok
        return self._run(binding, spec, args, action_id, tuple(request.evidence_refs), request.confirmation_state, corr, cancel)

    def _gateway_principal(self, b: SessionBinding) -> GatewayPrincipal:
        return GatewayPrincipal(Principal(b.subject, "service", (), b.tenant), client_id=b.client_id,
                                workspace=b.tenant, scopes=tuple(b.scopes))

    def _entitled(self, b: SessionBinding, spec: ToolSpec, args: dict) -> bool:
        """Is this principal allowed to make this call RIGHT NOW? Used before the bridge answers a repeat from its own journal, so a
        replay never outlives the right to the call (the gateway does the same before serving a stored result; the journal is the
        bridge's, so the bridge asks). Nothing is reserved and no counter moves. Any fault answers no."""
        try:
            gp = self._gateway_principal(b)
            reg = self.gateway.registry
            manifest = reg.get(spec.capability)
            if manifest is None or not reg.is_visible(spec.capability, gp, self.gateway.authorize):
                return False
            guard = self.gateway.authority
            if guard is None:
                return True
            from agentic_os.mission.authority import Action, Verdict   # noqa: PLC0415
            key = self.gateway.authority_key(gp) if self.gateway.authority_key else f"{gp.tenant}:{gp.subject}"
            return guard.check(key, gp.tenant, Action(capability=spec.capability, arguments=dict(args), risk_tier=int(manifest.risk_tier),
                                                      side_effecting=bool(manifest.side_effecting))).verdict is Verdict.ALLOW
        except Exception:  # noqa: BLE001
            return False

    def _action_lock(self, action_id: str) -> threading.Lock:
        with self._lock:
            return self._action_locks.setdefault(action_id, threading.Lock())

    def _run(self, binding: SessionBinding, spec: ToolSpec, args: dict, action_id: str, evidence: Tuple[str, ...],
             confirmation: ConfirmationState, corr: str, cancel: Optional[threading.Event], *, resumed: bool = False) -> ActionResult:
        sid = binding.session_id
        with self._action_lock(action_id):
            prior = self.receipt(action_id)
            if spec.side_effecting and prior:
                # an earlier attempt decides before anything is sent again
                if prior["status"] == ActionStatus.COMPLETED.value:
                    if not self._entitled(binding, spec, args):
                        return self._refuse_known(binding, spec, action_id, corr, Reason.POLICY_DENIED, ActionStatus.DENIED)
                    self._note_replay(binding, spec, action_id, corr, ActionStatus.COMPLETED)
                    return ActionResult(ActionStatus.COMPLETED, Reason.REPLAYED, action_id, corr, sid, spec.name,
                                        receipt_ref=prior.get("receipt_ref", ""), replayed=True)
                if prior["status"] == ActionStatus.OUTCOME_UNKNOWN.value:
                    self._note_replay(binding, spec, action_id, corr, ActionStatus.OUTCOME_UNKNOWN)
                    return ActionResult(ActionStatus.OUTCOME_UNKNOWN, Reason.OUTCOME_UNKNOWN, action_id, corr, sid, spec.name)
            if cancel is not None and cancel.is_set():
                return self._refuse_known(binding, spec, action_id, corr, Reason.CANCELLED_BEFORE_DISPATCH, ActionStatus.FAILED)
            try:
                self._append(EV_SUBMITTED, binding.tenant, {
                    "action_id": action_id, "correlation_id": corr, "tenant": binding.tenant, "session_id": sid, "tool": spec.name,
                    "capability": spec.capability, "side_effecting": spec.side_effecting, "args_sha256": sha256_hex(args),
                    "evidence_refs": list(evidence), "identity": int(binding.identity), "confirmation": confirmation.value,
                    "resumed": resumed})
            except Exception:  # noqa: BLE001 - without a journal there is no receipt: nothing is dispatched
                _log.warning("journal unavailable: action %s not dispatched", action_id)
                return ActionResult(ActionStatus.FAILED, Reason.BRIDGE_UNAVAILABLE, action_id, corr, sid, spec.name, retry_safe=True)
            with self._lock:
                self._live.add(action_id)
            try:
                gw_req = GatewayRequest(self._gateway_principal(binding), spec.capability, dict(args),
                                        idempotency_key=action_id if spec.side_effecting else None,
                                        protocol="voice", request_id=corr)
                try:
                    res = self.gateway.invoke(gw_req)
                except Exception:  # noqa: BLE001 - the gateway handles its own faults; this is a last resort
                    _log.warning("gateway raised for correlation %s", corr)
                    out = self._unknown_or_failed(spec, corr, action_id, binding)
                else:
                    out = self._map(res, gw_req, spec, action_id, corr, binding)
                self._decide(binding, spec, out, gateway_request_id=corr)
                if out.status is ActionStatus.PENDING_APPROVAL or out.reason == Reason.OUTPUT_HELD:
                    with self._lock:
                        self._pending[action_id] = _Pending(binding, spec, dict(args), action_id, evidence, confirmation, out.approval_id)
                else:
                    with self._lock:
                        self._pending.pop(action_id, None)
                return out
            finally:
                with self._lock:
                    self._live.discard(action_id)

    def _note_replay(self, b: SessionBinding, spec: ToolSpec, action_id: str, corr: str, status: ActionStatus) -> None:
        """A repeated request answered from the journal. Nothing is dispatched; the repeat is still on the record."""
        try:
            self._append(EV_REPLAYED, b.tenant, {"action_id": action_id, "correlation_id": corr, "tenant": b.tenant, "session_id": b.session_id,
                                                 "tool": spec.name, "answered_with": status.value})
        except Exception:  # noqa: BLE001 - the answer does not depend on it
            _log.warning("could not journal a replay (correlation %s)", corr)

    def _refuse_known(self, b: SessionBinding, spec: ToolSpec, action_id: str, corr: str, code: str, status: ActionStatus) -> ActionResult:
        res = ActionResult(status, code, action_id, corr, b.session_id, spec.name, retry_safe=True)
        try:
            self._append(EV_REFUSED, b.tenant, {"action_id": action_id, "correlation_id": corr, "tenant": b.tenant, "session_id": b.session_id,
                                                "tool": spec.name, "status": status.value, "reason": code, "retry_safe": True})
        except Exception:  # noqa: BLE001
            pass
        return res

    def _decide(self, b: SessionBinding, spec: ToolSpec, out: ActionResult, *, gateway_request_id: str) -> None:
        try:
            self._append(EV_DECIDED, b.tenant, {
                "action_id": out.action_id, "correlation_id": out.correlation_id, "tenant": b.tenant, "session_id": b.session_id,
                "tool": spec.name, "status": out.status.value, "reason": out.reason, "approval_id": out.approval_id,
                "gateway_request_id": gateway_request_id, "receipt_ref": out.receipt_ref, "retry_safe": out.retry_safe})
        except Exception:  # noqa: BLE001 - the effect (if any) already happened; the journal head stays 'in flight' -> outcome_unknown
            _log.warning("could not journal the outcome of %s (correlation %s)", out.action_id, out.correlation_id)

    def _lease_state(self, corr: str) -> str:
        g = self.gateway.authority
        if g is None:
            return "unknown"
        lease = g.lease(corr)
        return (lease or {}).get("state", "none")

    def _unknown_or_failed(self, spec: ToolSpec, corr: str, action_id: str, b: SessionBinding, reason: str = Reason.DOWNSTREAM_ERROR) -> ActionResult:
        if spec.side_effecting and self._lease_state(corr) not in ("released", "none"):
            return ActionResult(ActionStatus.OUTCOME_UNKNOWN, Reason.OUTCOME_UNKNOWN, action_id, corr, b.session_id, spec.name)
        return ActionResult(ActionStatus.FAILED, reason, action_id, corr, b.session_id, spec.name, retry_safe=True)

    def _map(self, res: Any, gw_req: GatewayRequest, spec: ToolSpec, action_id: str, corr: str, b: SessionBinding) -> ActionResult:
        sid = b.session_id
        st = res.status
        if st is GatewayStatus.DENIED:
            if res.decision.reason == "authority:outcome_unknown":
                return ActionResult(ActionStatus.OUTCOME_UNKNOWN, Reason.OUTCOME_UNKNOWN, action_id, corr, sid, spec.name)
            return ActionResult(ActionStatus.DENIED, Reason.POLICY_DENIED, action_id, corr, sid, spec.name, retry_safe=True)
        if st is GatewayStatus.PENDING_APPROVAL and spec.side_effecting and res.decision.egress_action is EgressAction.REQUIRE_APPROVAL:
            # The write RAN; the egress policy is holding its output. That is not an approval to wait for: asking the gateway again
            # replays the held outcome under the policy of that moment and never sends the write again. It is not 'completed'
            # either, because the downstream receipt has not been released.
            return ActionResult(ActionStatus.APPROVED, Reason.OUTPUT_HELD, action_id, corr, sid, spec.name)
        if st is GatewayStatus.PENDING_APPROVAL:
            aid = self.approvals.approval_id_for(gw_req) if self.approvals else ""
            return ActionResult(ActionStatus.PENDING_APPROVAL, Reason.APPROVAL_REQUIRED, action_id, corr, sid, spec.name, approval_id=aid)
        if st is GatewayStatus.ERROR:
            # The gateway refuses a key that is already bound to something else, or to a call that has not finished, BEFORE it reserves
            # any authority or dispatches anything. Neither is ever a completed action, and neither reveals the earlier result.
            if res.error == "idempotency_conflict":
                return ActionResult(ActionStatus.FAILED, Reason.IDEMPOTENCY_CONFLICT, action_id, corr, sid, spec.name, retry_safe=False)
            if res.error == "idempotency_in_progress":
                return ActionResult(ActionStatus.APPROVED, Reason.IDEMPOTENCY_IN_PROGRESS, action_id, corr, sid, spec.name)
            if self.approvals is not None and self.approvals.was_unavailable(gw_req.request_id):
                return ActionResult(ActionStatus.FAILED, Reason.APPROVAL_UNAVAILABLE, action_id, corr, sid, spec.name, retry_safe=True)
            return self._unknown_or_failed(spec, corr, action_id, b, Reason.GATEWAY_ERROR if res.error == "gateway_error" else Reason.DOWNSTREAM_ERROR)
        replayed = res.request_id != corr
        out = res.output
        if spec.side_effecting:
            ref = ""
            if isinstance(out, dict) and out.get("provider_object_id"):
                ref = str(out["provider_object_id"])
            elif res.evidence_refs:
                ref = str(res.evidence_refs[0])
            if not ref:
                # the gateway reports success but the downstream system gave no receipt: it is not 'completed'
                return ActionResult(ActionStatus.OUTCOME_UNKNOWN, Reason.NO_DOWNSTREAM_RECEIPT, action_id, corr, sid, spec.name)
            return ActionResult(ActionStatus.COMPLETED, Reason.REPLAYED if replayed else Reason.OK, action_id, corr, sid, spec.name,
                                receipt_ref=ref, replayed=replayed)
        try:
            if out is not None and len(canonical_json(out)) > MAX_OUTPUT_BYTES:
                return ActionResult(ActionStatus.FAILED, Reason.PAYLOAD_TOO_LARGE, action_id, corr, sid, spec.name, retry_safe=True)
        except (TypeError, ValueError):
            return ActionResult(ActionStatus.FAILED, Reason.DOWNSTREAM_ERROR, action_id, corr, sid, spec.name, retry_safe=True)
        return ActionResult(ActionStatus.COMPLETED, Reason.OK, action_id, corr, sid, spec.name, output=out, receipt_ref=res.audit_ref)

    # ── asynchronous entry (what a Pipecat tool callback uses, through the proxy) ────────────────────────────────
    def _default_timeout(self, request: ActionRequest) -> float:
        spec = self.tools.get(request.tool_name) if isinstance(request.tool_name, str) else None
        if spec is None:
            return DEFAULT_READ_TIMEOUT_S
        return spec.timeout_s or (DEFAULT_WRITE_TIMEOUT_S if spec.side_effecting else DEFAULT_READ_TIMEOUT_S)

    async def submit_async(self, request: ActionRequest, *, timeout: Optional[float] = None) -> ActionResult:
        """Cancellation semantics, in one place:

        * the work runs in its own task and is shielded: cancelling the CALLER (a barge-in, a framework timeout, a
          dropped call) never cancels the work;
        * a request that has not reached its dispatch point when the caller goes away is cancelled and journaled as
          ``cancelled_before_dispatch`` (nothing happened);
        * a request that has been dispatched runs to its end and its outcome is journaled; the caller that left can
          read it with :meth:`status`, and asking again returns the same receipt instead of a second effect;
        * when the deadline passes first, the caller gets ``approved/in_flight`` (a write) or ``failed/timeout`` (a read),
          never ``completed``.
        """
        deadline = timeout if timeout is not None else self._default_timeout(request)
        cancel = threading.Event()
        task = asyncio.ensure_future(asyncio.to_thread(self.submit, request, cancel=cancel))
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        try:
            return await asyncio.wait_for(asyncio.shield(task), deadline)
        except asyncio.TimeoutError:
            cancel.set()
            return self._interim(request)
        except asyncio.CancelledError:
            cancel.set()
            raise

    def _task_done(self, task: "asyncio.Future") -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:       # consume: never an un-retrieved exception
            _log.warning("an action task raised: %s", type(task.exception()).__name__)

    def _interim(self, request: ActionRequest) -> ActionResult:
        corr = "corr_" + uuid.uuid4().hex[:20]
        spec = self.tools.get(request.tool_name) if isinstance(request.tool_name, str) else None
        action_id = ""
        try:
            b = self.binding(request.session_id)
            if spec is not None:
                args = spec.validate(request.arguments)
                action_id = derive_action_id(b.tenant, spec.name, {k: args[k] for k in (spec.intent_args or tuple(args)) if k in args})
        except ValidationError:
            pass
        rec = self.receipt(action_id) if action_id else None
        if rec and rec["status"] in (ActionStatus.COMPLETED.value, ActionStatus.PENDING_APPROVAL.value, ActionStatus.DENIED.value):
            return ActionResult(ActionStatus(rec["status"]), rec["reason"], action_id, corr, request.session_id, request.tool_name,
                                receipt_ref=rec.get("receipt_ref", ""), approval_id=rec.get("approval_id", ""))
        if spec is not None and spec.side_effecting and rec and rec.get("state") == "in_flight":
            return ActionResult(ActionStatus.APPROVED, Reason.IN_FLIGHT, action_id, corr, request.session_id, spec.name)
        return ActionResult(ActionStatus.FAILED, Reason.TIMEOUT, action_id, corr, request.session_id, request.tool_name,
                            retry_safe=not (spec and spec.side_effecting))

    async def drain(self, timeout: float = 10.0) -> None:
        """Wait for every dispatched task (shutdown, tests)."""
        pending = list(self._tasks)
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    # ── host-level operations (never reachable through a proxy) ─────────────────────────────────────────────────
    def status(self, session_id: str, action_id: str) -> Optional[ActionResult]:
        b = self.binding(session_id)
        rec = self.receipt(action_id)
        if rec is None or rec.get("tenant") != b.tenant or rec.get("session_id") != session_id:
            return None
        return ActionResult(ActionStatus(rec["status"]), rec["reason"], action_id, rec.get("last_correlation", ""), session_id,
                            rec.get("tool", ""), receipt_ref=rec.get("receipt_ref", ""), approval_id=rec.get("approval_id", ""))

    def pending_actions(self) -> Dict[str, str]:
        """action id -> approval id, for the approvals inbox a person works from."""
        with self._lock:
            return {k: v.approval_id for k, v in self._pending.items()}

    def resume(self, action_id: str) -> ActionResult:
        """After a person approved the parked action in the approvals inbox: run the identical request again. The
        gateway accepts it only if the approval matches this exact request, is unexpired and unused."""
        with self._lock:
            p = self._pending.get(action_id)
        if p is None:
            raise KeyError("no pending action with this id (it was decided, or the process restarted and the approval inbox is gone)")
        corr = "corr_" + uuid.uuid4().hex[:20]
        if self.handoff_active(p.binding.session_id) and not p.spec.allowed_while_handoff:
            # the caller asked for a person (or was routed to one) after this was parked: an approval cannot turn an automated
            # follow-up into something sent to someone who is now being handled by a human
            with self._lock:
                self._pending.pop(action_id, None)
            return self._refuse_known(p.binding, p.spec, action_id, corr, Reason.HANDOFF_ACTIVE, ActionStatus.DENIED)
        return self._run(p.binding, p.spec, p.arguments, action_id, p.evidence_refs, p.confirmation, corr, None, resumed=True)

    async def resume_async(self, action_id: str) -> ActionResult:
        return await asyncio.shield(asyncio.ensure_future(asyncio.to_thread(self.resume, action_id)))

    def reconcile(self, action_id: str, *, executed: bool, evidence: dict, actor: str, reason: str,
                  result: Optional[dict] = None) -> ActionResult:
        """Resolve an action whose outcome is unknown, with evidence and a named person (the gateway's own rules)."""
        rec = self.receipt(action_id)
        if rec is None or rec["status"] != ActionStatus.OUTCOME_UNKNOWN.value:
            raise KeyError("this action is not in the outcome-unknown state")
        corr = rec.get("last_correlation", "")
        if executed and not (isinstance(result, dict) and result.get("provider_object_id")):
            raise ValueError("an executed outcome needs the downstream receipt (result with provider_object_id)")
        self.gateway.reconcile_call(corr, executed=executed, evidence=evidence, actor=actor, reason=reason, result=result)
        status = ActionStatus.COMPLETED if executed else ActionStatus.FAILED
        self._append(EV_RECONCILED, rec["tenant"], {
            "action_id": action_id, "correlation_id": corr, "tenant": rec["tenant"], "session_id": rec["session_id"], "tool": rec["tool"],
            "status": status.value, "reason": Reason.OK, "receipt_ref": str(result["provider_object_id"]) if executed else "",
            "retry_safe": not executed, "actor": str(actor)[:80]})
        return ActionResult(status, Reason.OK, action_id, corr, rec["session_id"], rec["tool"],
                            receipt_ref=str(result["provider_object_id"]) if executed else "", retry_safe=not executed)

    def request_handoff(self, session_id: str, category: str) -> ActionResult:
        """Latch the session into human handoff: from now on only tools marked ``allowed_while_handoff`` may run. If a
        handoff tool is allowlisted it is submitted through the same governed path; its receipt is what the
        conversation may later cite. The latch does not depend on that call succeeding."""
        b = self.binding(session_id)
        with self._lock:
            self._handoff.add(session_id)
        self._append(EV_HANDOFF, b.tenant, {"action_id": "", "session_id": session_id, "category": str(category)[:32]})
        spec = next((s for s in self.tools.values() if s.allowed_while_handoff), None)
        if spec is None:
            return ActionResult(ActionStatus.DENIED, Reason.HANDOFF_ACTIVE, "", "corr_" + uuid.uuid4().hex[:20], session_id, "")
        return self.submit(ActionRequest(session_id, b.tenant, spec.name, {"session_ref": session_id, "category": str(category)[:32]}))


class ActionProxy:
    """The only object a framework tool callback receives. It can ask for allowlisted actions on ONE session and
    read their status; it holds no credential, no gateway, no tenant of its own to change. (In-process Python
    is not a sandbox: the proxy narrows what well-behaved handler code can reach, and the checks behind it do not
    trust the arguments it passes.)"""
    __slots__ = ("_bridge", "session_id")

    def __init__(self, bridge: VoiceActionBridge, session_id: str):
        object.__setattr__(self, "_bridge", bridge)
        object.__setattr__(self, "session_id", session_id)

    def __setattr__(self, *_: Any) -> None:
        raise AttributeError("an action proxy is read-only")

    @property
    def tool_specs(self) -> Mapping[str, ToolSpec]:
        return dict(self._bridge.tools)

    @property
    def identity(self) -> IdentityLevel:
        """What the host currently knows about the caller (read-only; only the host can raise it)."""
        try:
            return self._bridge.binding(self.session_id).identity
        except ValidationError:
            return IdentityLevel.ANONYMOUS

    async def call(self, tool_name: str, arguments: Mapping[str, Any], *, evidence_refs: Tuple[str, ...] = (),
                   confirmation: ConfirmationState = ConfirmationState.NOT_REQUESTED) -> ActionResult:
        try:
            tenant = self._bridge.binding(self.session_id).tenant
        except ValidationError as e:
            return ActionResult(ActionStatus.DENIED, e.code, "", "", self.session_id, str(tool_name)[:64], retry_safe=False)
        return await self._bridge.submit_async(ActionRequest(
            self.session_id, tenant, tool_name, dict(arguments) if isinstance(arguments, Mapping) else {}, "",
            tuple(evidence_refs), confirmation))

    async def status(self, action_id: str) -> Optional[ActionResult]:
        return self._bridge.status(self.session_id, action_id)

    async def request_handoff(self, category: str) -> ActionResult:
        return await asyncio.shield(asyncio.ensure_future(asyncio.to_thread(self._bridge.request_handoff, self.session_id, category)))

"""RapGateway: the RAP tool server in front of the Agent Gateway.

A RAP runtime (Infinity) invokes a tool, receives HTTP 200, hibernates, and is woken by a result POSTed to its callback URL. This class is the
tool server's brain. Every accepted invocation crosses ONE door into the effect: ``AgentGateway.invoke`` (identity, permission, authority
envelope, budget, approval, idempotency, egress, audit). There is no second path and nothing here grants anything.

What it adds, and where each guarantee lives:

* durable acceptance (``store.insert_action`` commits BEFORE the 200 is sent): the runtime will not ask again, so the arguments are kept
  until the action is decided;
* exactly one dispatch (``accepted -> dispatching`` is a compare-and-set) and a gateway idempotency key derived from the business intent;
* recovery from the authority ledger, never from guesswork: a row found ``dispatching`` after a crash is re-dispatched only if its
  reservation never left (none / reserved / released), completed only if the ledger kept the provider's receipt, and ``outcome_unknown``
  otherwise, which is never sent again until a person reconciles it;
* a transactional outbox for results: the result is queued in the same transaction that decides the action and delivered at least once with a
  stable ``event-id``; a receiver drops the duplicate;
* cancellation that never abandons a committed write and never mutates a finished action; thread closure that ends the thread's authority.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from agentic_os.agent_gateway import (
    AgentGateway, CapabilityKind, EgressAction, GatewayPrincipal, GatewayRequest, GatewayStatus)
from agentic_os.overlays import Principal

from . import event_mapping as em
from .callback_auth import CallbackClient, CallbackRefused, Peer, PeerRegistry, callback_headers, check_callback_url
from .contracts import (
    ActionStatus, Reason, WireError, parse_invocation, parse_notice, sha256_hex, RAP_TOOLSET_PATH)
from .lineage import Authority, LineageManager, LineageRefused
from .operation_binding import OperationSpec, derive_action_id, local_operations
from .receipt_adapter import receipt_ref
from .store import OutboxStore, rec_key

_log = logging.getLogger("redevops_infinity.gateway")
STREAM = "rap-actions:"
FINAL_STATUSES = (ActionStatus.COMPLETED.value, ActionStatus.FAILED.value, ActionStatus.DENIED.value, ActionStatus.CANCELLED.value)
ARG_KEYS = ("tenant", "group_id", "tool_call_id", "operation", "action_id")


class RapGateway:
    def __init__(self, gateway: AgentGateway, operations: Mapping[str, OperationSpec], store: OutboxStore, journal: Any, peers: PeerRegistry,
                 *, lineage: Optional[LineageManager] = None, callbacks: Optional[CallbackClient] = None, clock: Callable[[], float] = time.time,
                 workers: int = 4, result_ttl_s: float = 24 * 3600.0, max_backoff_s: float = 300.0, approval_poll_s: float = 15.0,
                 name: str = "redevops-governed-actions", allow_unenforced_authority: bool = False):
        if gateway.authority is None and not allow_unenforced_authority:
            raise ValueError("the RAP bridge requires a gateway with an authority guard; pass allow_unenforced_authority=True only for a local trial")
        self.gateway, self.store, self.journal, self.peers, self.clock = gateway, store, journal, peers, clock
        self.operations: Dict[str, OperationSpec] = {**dict(operations), **local_operations()}
        for op in self.operations.values():
            if not op.local and gateway.registry.get(op.capability) is None:
                raise ValueError(f"operation {op.name!r} names a capability that is not registered in the gateway")
        self.lineage = lineage or LineageManager(store, gateway.authority, None, self.operations)
        self.callbacks = callbacks or CallbackClient()
        self.result_ttl_s, self.max_backoff_s, self.approval_poll_s, self.name = result_ttl_s, max_backoff_s, approval_poll_s, name
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rap-dispatch")
        self._invoke_pool = ThreadPoolExecutor(max_workers=workers * 2, thread_name_prefix="rap-invoke")
        self._live: set = set()
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.audit_failures = 0

    # ── audit (the hash-chained EventStore) ───────────────────────────────────────────────────────────────────
    def _audit(self, etype: str, tenant: str, payload: dict, *, required: bool = False) -> bool:
        try:
            self.journal.append(etype, STREAM + tenant, payload)
            return True
        except Exception:  # noqa: BLE001
            self.audit_failures += 1
            _log.warning("could not append %s to the audit journal", etype)
            return False

    # ── discovery ─────────────────────────────────────────────────────────────────────────────────────────────
    def toolset(self, peer: Peer, base_url: str) -> dict:
        tools = [o.tool_def() for o in self.operations.values() if peer.operations is None or o.name in peer.operations]
        return {"name": self.name, "description": "Governed actions: every call is checked by the ReDevOps Agent Gateway before it runs.",
                "endpoint": base_url.rstrip("/") + "/invoke", "tools": tools}

    # ── POST /invoke ──────────────────────────────────────────────────────────────────────────────────────────
    def handle_invoke(self, peer: Peer, body: bytes) -> Tuple[int, str]:
        try:
            inv = parse_invocation(json.loads(body))
        except (ValueError, UnicodeDecodeError) as e:
            return 400, getattr(e, "code", "invalid_json") if isinstance(e, WireError) else "invalid_json"
        try:
            check_callback_url(inv.callback_url, peer.callback_origins, self.callbacks.resolver)
        except CallbackRefused as e:
            self._audit("RapRefused", peer.tenant, {"reason": "callback_not_allowed", "detail": e.reason, "key_id": peer.key_id, "group_id": inv.group_id,
                                                       "tool_call_id": inv.id})
            return 400, "callback_not_allowed"
        if self.store.inflight_count(peer.tenant, peer.key_id) >= peer.max_inflight:
            return 429, "too_many_in_flight"
        spec = self.operations.get(inv.operation)
        allowed = spec is not None and (peer.operations is None or spec.name in peer.operations)
        refusal: Optional[Tuple[str, str]] = None
        try:
            self.lineage.ensure(peer, inv.group_id, inv.thread_ancestors)
        except LineageRefused as e:
            if e.reason == Reason.LINEAGE_REFUSED:
                self._audit("RapRefused", peer.tenant, {"reason": e.code, "key_id": peer.key_id, "group_id": inv.group_id, "tool_call_id": inv.id})
                return 403, "lineage_refused"
            refusal = (Reason.SPAWN_REFUSED, e.code)
        args: Dict[str, Any] = {}
        if refusal is None and not allowed:
            refusal = (Reason.NOT_ALLOWLISTED, "operation")
        if refusal is None:
            try:
                args = spec.validate(inv.arguments)
            except WireError as e:
                refusal = (Reason.INVALID_REQUEST, e.code)
        if refusal is None and not spec.child_allowed and self._is_child(peer, inv.group_id):
            refusal = (Reason.POLICY_DENIED, "child_scope")
        now = self.clock()
        key = rec_key(peer.tenant, inv.group_id, inv.id)
        action_id = derive_action_id(peer.tenant, inv.operation, spec.intent(args)) if spec is not None and refusal is None else \
            "act_" + sha256_hex([peer.tenant, inv.operation, inv.group_id, inv.id])[:32]
        row = {"rec_key": key, "tenant": peer.tenant, "key_id": peer.key_id, "principal": peer.principal, "group_id": inv.group_id, "tool_call_id": inv.id,
               "call_id": inv.call_id, "operation": inv.operation, "action_id": action_id,
               "args_sha256": sha256_hex(inv.arguments), "args_json": json.dumps(args, sort_keys=True) if refusal is None else None,
               "callback_url": inv.callback_url, "user_hash": sha256_hex(inv.user_id)[:16] if inv.user_id else None,
               "state": "accepted", "status": ActionStatus.ACCEPTED.value, "reason": "", "receipt_ref": "", "corr": "", "approval_id": "",
               "side_effecting": int(bool(spec and spec.side_effecting)), "local": int(bool(spec and spec.local)),
               "subscription": int(bool(spec and spec.subscription)), "cancel_requested": 0, "created_at": now, "expires_at": now + self.result_ttl_s,
               "decided_at": None, "next_at": 0.0}
        delivery = None
        if refusal is not None:
            row.update(state="decided", status=ActionStatus.DENIED.value if refusal[0] != Reason.INVALID_REQUEST else ActionStatus.FAILED.value,
                       reason=refusal[0], decided_at=now)
            delivery = self._delivery(row, em.result_payload(ActionStatus(row["status"]), refusal[0], action_id, retry_safe=refusal[0] == Reason.INVALID_REQUEST))
        outcome, existing = self.store.insert_action(row, delivery)
        if outcome == "conflict":
            self._audit("RapRefused", peer.tenant, {"reason": "invocation_id_reused", "key_id": peer.key_id, "group_id": inv.group_id, "tool_call_id": inv.id})
            return 409, "invocation_id_reused"
        if outcome == "new":
            self._audit("RapAccepted", peer.tenant, {"rec_key": key, "key_id": peer.key_id, "principal": peer.principal, "group_id": inv.group_id,
                                                      "tool_call_id": inv.id, "operation": inv.operation, "action_id": action_id,
                                                      "args_sha256": row["args_sha256"], "decided": refusal is not None, "reason": row["reason"],
                                                      "ancestors": list(inv.thread_ancestors), "user_hash": row["user_hash"],
                                                      "refusal": refusal[1] if refusal else ""})
        return 200, ""

    def _is_child(self, peer: Peer, group_id: str) -> bool:
        r = self.store.lineage(peer.tenant, peer.key_id, group_id)
        return bool(r and r["child_id"])

    def _delivery(self, row: dict, payload: dict, *, subscription: bool = False, seq: int = 1) -> dict:
        body = em.tool_result(row["group_id"], row["tool_call_id"], row["call_id"], payload, subscription=subscription)
        return {"event_id": "evt_" + uuid.uuid4().hex, "rec_key": row["rec_key"], "seq": seq, "kind": "tool_result", "body_json": json.dumps(body, sort_keys=True),
                "next_at": 0.0, "created_at": self.clock()}

    # ── POST /cancel_tool_call and /close_thread ─────────────────────────────────────────────────────────────
    def handle_cancel(self, peer: Peer, body: bytes) -> None:
        """Always succeeds from the caller's point of view (spec). Only the peer that created the call can touch it; a finished action is not mutated."""
        try:
            tid, cid = parse_notice(json.loads(body), "tool_call_id")
        except (ValueError, UnicodeDecodeError):
            return
        self._cancel(peer, rec_key(peer.tenant, tid, cid), reason="cancel_tool_call")

    def _cancel(self, peer: Peer, key: str, reason: str) -> None:
        row = self.store.get(key)
        if row is None or row["key_id"] != peer.key_id or row["tenant"] != peer.tenant:
            return
        sub = self.store.sub(key)
        if sub is not None and sub["active"]:
            if self.store.end_sub(key):
                self._drop_undelivered_events(key)
                self._audit("RapSubscriptionCancelled", peer.tenant, {"rec_key": key, "reason": reason})
            return
        if row["state"] in ("accepted", "awaiting_approval"):
            payload = em.result_payload(ActionStatus.CANCELLED, Reason.CANCELLED_BEFORE_DISPATCH, row["action_id"], retry_safe=True)
            ok = self.store.cas(key, ("accepted", "awaiting_approval"), "decided", status=ActionStatus.CANCELLED.value,
                                reason=Reason.CANCELLED_BEFORE_DISPATCH, decided_at=self.clock(), cancel_requested=1,
                                delivery=self._delivery(row, payload))
            if ok:
                self._audit("RapCancelled", peer.tenant, {"rec_key": key, "action_id": row["action_id"], "reason": reason, "before_dispatch": True})
        elif row["state"] == "dispatching":
            self.store.update(key, cancel_requested=1)          # a dispatched write is never abandoned; the flag is for the audit
            self._audit("RapCancelRequested", peer.tenant, {"rec_key": key, "action_id": row["action_id"], "after_dispatch": True})

    def _drop_undelivered_events(self, key: str) -> None:
        with self.store.tx() as c:
            c.execute("UPDATE deliveries SET state='dropped' WHERE rec_key=? AND state='pending' AND kind='subscription_event'", (key,))

    def handle_close_thread(self, peer: Peer, body: bytes) -> None:
        try:
            tid, _ = parse_notice(json.loads(body), "")
        except (ValueError, UnicodeDecodeError):
            return
        for row in self.store.thread_actions(peer.tenant, tid):
            if row["key_id"] == peer.key_id:
                self._cancel(peer, row["rec_key"], reason="close_thread")
        n = self.lineage.close_thread(peer, tid)
        self._audit("RapThreadClosed", peer.tenant, {"group_id": tid, "key_id": peer.key_id, "lineage_closed": n})

    # ── scheduler ─────────────────────────────────────────────────────────────────────────────────────────────
    def start(self, interval_s: float = 0.2) -> None:
        self.recover()
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:  # noqa: BLE001
                    _log.exception("scheduler tick failed")
                self._stop.wait(interval_s)
        self._thread = threading.Thread(target=loop, name="rap-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._pool.shutdown(wait=True)
        self._invoke_pool.shutdown(wait=True)

    def tick(self, *, inline: bool = False) -> int:
        """One scheduler pass. ``inline=True`` runs dispatches in the calling thread (deterministic tests)."""
        now = self.clock()
        n = 0
        for row in self.store.in_state("awaiting_approval", due_before=now):
            if self.store.cas(row["rec_key"], ("awaiting_approval",), "accepted", next_at=now):
                n += 1
        futs = []
        for row in self.store.in_state("accepted", due_before=now):
            if row["rec_key"] in self._live:
                continue
            if inline:
                self._dispatch(row["rec_key"])
            else:
                with self._lock:
                    self._live.add(row["rec_key"])
                futs.append(self._pool.submit(self._dispatch_live, row["rec_key"]))
            n += 1
        for d in self.store.due_deliveries(now):
            self._deliver(d)
            n += 1
        n += self._tick_subscriptions()
        return n

    def _dispatch_live(self, key: str) -> None:
        try:
            self._dispatch(key)
        finally:
            with self._lock:
                self._live.discard(key)

    def resume_awaiting(self, action_id: Optional[str] = None) -> int:
        """Host hook: a person decided in the approvals inbox. Re-run parked actions now instead of at the next poll. The gateway runs the
        identical request only if the approval matches it exactly, is unexpired and unused."""
        n = 0
        for row in self.store.in_state("awaiting_approval"):
            if action_id in (None, row["action_id"]) and self.store.cas(row["rec_key"], ("awaiting_approval",), "accepted", next_at=0.0):
                n += 1
        return n

    # ── dispatch ──────────────────────────────────────────────────────────────────────────────────────────────
    def _decide(self, row: dict, from_states: tuple, status: ActionStatus, reason: str, *, receipt: str = "", output: Any = None,
                retry_safe: bool = False, extra_audit: Optional[dict] = None) -> bool:
        payload = em.result_payload(status, reason, row["action_id"], receipt_ref=receipt, output=output, retry_safe=retry_safe)
        ok = self.store.cas(row["rec_key"], from_states, "decided", status=status.value, reason=reason, receipt_ref=receipt, decided_at=self.clock(),
                            delivery=self._delivery(row, payload))
        if ok:
            self._audit("RapDecided", row["tenant"], {"rec_key": row["rec_key"], "action_id": row["action_id"], "operation": row["operation"],
                                                       "status": status.value, "reason": reason, "receipt_ref": receipt, "corr": row["corr"],
                                                       **(extra_audit or {})})
        return ok

    def _defer(self, key: str, seconds: float) -> None:
        self.store.cas(key, ("dispatching",), "accepted", next_at=self.clock() + seconds)

    def _dispatch(self, key: str) -> None:
        row0 = self.store.get(key)
        if row0 is None or row0["state"] != "accepted":
            return
        corr = "corr_" + uuid.uuid4().hex[:20]
        if not self.store.cas(key, ("accepted",), "dispatching", corr=corr):
            return
        with self._lock:
            self._live.add(key)
        try:
            self._dispatch_claimed(self.store.get(key))
        finally:
            with self._lock:
                self._live.discard(key)

    def _dispatch_claimed(self, row: dict) -> None:
        key, spec = row["rec_key"], self.operations.get(row["operation"])
        peer = self.peers.get(row["key_id"])
        D = ("dispatching",)
        if spec is None or peer is None or peer.revoked or (peer.expires_at and self.clock() >= peer.expires_at):
            self._decide(row, D, ActionStatus.DENIED, Reason.POLICY_DENIED)
            return
        if row["cancel_requested"]:
            self._decide(row, D, ActionStatus.CANCELLED, Reason.CANCELLED_BEFORE_DISPATCH, retry_safe=True)
            return
        if self.clock() >= row["expires_at"]:
            self._decide(row, D, ActionStatus.FAILED, Reason.DEADLINE_EXPIRED, retry_safe=not spec.side_effecting)
            return
        if spec.local:
            self._run_local(row, spec, peer)
            return
        args = json.loads(row["args_json"] or "{}")
        try:
            auth = self.lineage.authority(peer, row["group_id"])
        except LineageRefused as e:
            self._decide(row, D, ActionStatus.DENIED, Reason.SPAWN_REFUSED if e.reason == Reason.SPAWN_REFUSED else Reason.POLICY_DENIED,
                         extra_audit={"detail": e.code})
            return
        if spec.side_effecting and self._prior_blocks(row, spec, peer, auth, args):
            return
        if not self._audit("RapDispatching", row["tenant"], {"rec_key": key, "action_id": row["action_id"], "corr": row["corr"],
                                                              "operation": row["operation"], "authority_key": auth.key}, required=True):
            self._defer(key, 5.0)                        # no audit record, no dispatch
            return
        gp = GatewayPrincipal(Principal(auth.subject, "service", (), row["tenant"]), client_id=row["key_id"], workspace=row["tenant"],
                              scopes=tuple(peer.gateway_scopes))
        greq = GatewayRequest(gp, spec.capability, dict(args), idempotency_key=row["action_id"] if spec.side_effecting else None, protocol="rap",
                              request_id=row["corr"], credential=auth.credential)
        fut = self._invoke_pool.submit(self.gateway.invoke, greq)
        try:
            res = fut.result(timeout=spec.timeout_s)
        except FutureTimeout:
            fut.add_done_callback(lambda f, r=row, s=spec: self._late(r, s, f))
            if spec.side_effecting:
                self._decide(row, D, ActionStatus.OUTCOME_UNKNOWN, Reason.TIMEOUT)
            else:
                self._decide(row, D, ActionStatus.FAILED, Reason.TIMEOUT, retry_safe=True)
            return
        except Exception:  # noqa: BLE001 - the gateway handles its own faults; last resort
            st, rs = self._unknown_or_failed(row, spec)
            self._decide(row, D, st, rs, retry_safe=st is ActionStatus.FAILED)
            return
        self._apply_result(row, spec, res)

    def _apply_result(self, row: dict, spec: OperationSpec, res: Any, *, late: bool = False) -> None:
        D = ("dispatching",)
        st = res.status
        if st is GatewayStatus.DENIED:
            if res.decision.reason == "authority:outcome_unknown":
                self._decide(row, D, ActionStatus.OUTCOME_UNKNOWN, Reason.OUTCOME_UNKNOWN)
            else:
                self._decide(row, D, ActionStatus.DENIED, Reason.POLICY_DENIED, retry_safe=True)
            return
        if st is GatewayStatus.PENDING_APPROVAL:
            held = spec.side_effecting and res.decision.egress_action is EgressAction.REQUIRE_APPROVAL
            reason = Reason.OUTPUT_HELD if held else Reason.APPROVAL_REQUIRED
            if self.store.cas(row["rec_key"], D, "awaiting_approval", status=ActionStatus.PENDING_APPROVAL.value, reason=reason,
                              next_at=self.clock() + self.approval_poll_s):
                self._audit("RapAwaitingApproval", row["tenant"], {"rec_key": row["rec_key"], "action_id": row["action_id"], "reason": reason,
                                                                     "corr": row["corr"]})
            return
        if st is GatewayStatus.ERROR:
            if res.error == "idempotency_conflict":
                self._decide(row, D, ActionStatus.FAILED, Reason.IDEMPOTENCY_CONFLICT, retry_safe=False)
            elif res.error == "idempotency_in_progress":
                self._defer(row["rec_key"], 1.0)
            else:
                stt, rs = self._unknown_or_failed(row, spec, Reason.GATEWAY_ERROR if res.error == "gateway_error" else Reason.DOWNSTREAM_ERROR)
                self._decide(row, D, stt, rs, retry_safe=stt is ActionStatus.FAILED)
            return
        out = res.output
        if spec.side_effecting:
            ref = receipt_ref(out, tuple(res.evidence_refs))
            if not ref:
                self._decide(row, D, ActionStatus.OUTCOME_UNKNOWN, Reason.NO_DOWNSTREAM_RECEIPT)
                return
            self._decide(row, D, ActionStatus.COMPLETED, Reason.REPLAYED if res.request_id != row["corr"] else Reason.OK, receipt=ref, output=out)
            return
        self._decide(row, D, ActionStatus.COMPLETED, Reason.OK, output=out, receipt=res.audit_ref or "")

    def _late(self, row: dict, spec: OperationSpec, fut: Any) -> None:
        """The gateway call finished after its deadline. The caller was already told (unknown / timeout); the record is corrected so a status read
        or a watch shows the truth. Nothing is delivered again and nothing is sent again."""
        try:
            res = fut.result()
        except BaseException:  # noqa: BLE001
            return
        if res.status is GatewayStatus.OK and spec.side_effecting:
            ref = receipt_ref(res.output, tuple(res.evidence_refs))
            if ref:
                self.store.update(row["rec_key"], status=ActionStatus.COMPLETED.value, reason=Reason.OK, receipt_ref=ref)
                self._audit("RapLateOutcome", row["tenant"], {"rec_key": row["rec_key"], "action_id": row["action_id"], "status": "completed",
                                                               "receipt_ref": ref})

    def _lease(self, corr: str) -> Optional[dict]:
        g = self.gateway.authority
        return g.lease(corr) if g is not None and corr else None

    def _unknown_or_failed(self, row: dict, spec: OperationSpec, reason: str = Reason.DOWNSTREAM_ERROR) -> Tuple[ActionStatus, str]:
        if spec.side_effecting:
            state = (self._lease(row["corr"]) or {}).get("state", "none" if self.gateway.authority is not None else "unknown")
            if state not in ("released", "none"):
                return ActionStatus.OUTCOME_UNKNOWN, Reason.OUTCOME_UNKNOWN
        return ActionStatus.FAILED, reason

    def _entitled(self, peer: Peer, auth: Authority, spec: OperationSpec, args: dict) -> bool:
        """May this principal make this call RIGHT NOW? Asked before a repeat is answered from the record, so a replay never outlives the right
        to the call. Nothing is reserved. Any fault answers no."""
        try:
            gp = GatewayPrincipal(Principal(auth.subject, "service", (), peer.tenant), client_id=peer.key_id, workspace=peer.tenant, scopes=tuple(peer.gateway_scopes))
            manifest = self.gateway.registry.get(spec.capability)
            if manifest is None or not self.gateway.registry.is_visible(spec.capability, gp, self.gateway.authorize):
                return False
            guard = self.gateway.authority
            if guard is None:
                return True
            from agentic_os.mission.authority import Action, Verdict
            return guard.check(auth.key, peer.tenant, Action(capability=spec.capability, arguments=dict(args), risk_tier=int(manifest.risk_tier),
                                                             side_effecting=bool(manifest.side_effecting))).verdict is Verdict.ALLOW
        except Exception:  # noqa: BLE001
            return False

    def _prior_blocks(self, row: dict, spec: OperationSpec, peer: Peer, auth: Authority, args: dict) -> bool:
        """An earlier attempt of the same business intent decides before anything is sent again. True = this row was decided or deferred here."""
        D = ("dispatching",)
        others = [o for o in self.store.by_action(row["tenant"], row["action_id"], exclude=row["rec_key"]) if not o["local"]]
        for o in others:
            if o["state"] == "decided" and o["status"] == ActionStatus.COMPLETED.value and o["receipt_ref"]:
                if not self._entitled(peer, auth, spec, args):
                    self._decide(row, D, ActionStatus.DENIED, Reason.POLICY_DENIED, retry_safe=True)
                else:
                    self._decide(row, D, ActionStatus.COMPLETED, Reason.REPLAYED, receipt=o["receipt_ref"])
                return True
            if o["state"] == "decided" and o["status"] == ActionStatus.OUTCOME_UNKNOWN.value:
                self._decide(row, D, ActionStatus.OUTCOME_UNKNOWN, Reason.OUTCOME_UNKNOWN)
                return True
            if o["state"] in ("dispatching", "accepted", "awaiting_approval") and (o["created_at"], o["rec_key"]) < (row["created_at"], row["rec_key"]):
                self._defer(row["rec_key"], 1.0)               # an earlier identical request is still being worked on: wait for its outcome
                return True
        return False

    # ── operations served from the bridge's own records ───────────────────────────────────────────────────────
    def _run_local(self, row: dict, spec: OperationSpec, peer: Peer) -> None:
        D = ("dispatching",)
        args = json.loads(row["args_json"] or "{}")
        target = self.store.newest_by_action(row["tenant"], args["action_id"])
        own = target is not None and target["principal"] == row["principal"] and target["key_id"] == row["key_id"]
        if not own:
            self._decide(row, D, ActionStatus.FAILED, Reason.INVALID_REQUEST, output={"found": False}, retry_safe=True)
            return
        view = {"found": True, "action_id": target["action_id"], "status": target["status"], "reason": target["reason"],
                "state": target["state"], **({"receipt_ref": target["receipt_ref"]} if target["receipt_ref"] else {})}
        if not spec.subscription or target["status"] in FINAL_STATUSES:
            self._decide(row, D, ActionStatus.COMPLETED, Reason.OK, output=view)
            return
        sig = self._sig(target)
        payload = em.result_payload(ActionStatus.COMPLETED, Reason.OK, row["action_id"], output={**view, "subscription_id": row["tool_call_id"]})
        self.store.add_sub({"rec_key": row["rec_key"], "tenant": row["tenant"], "principal": row["principal"], "group_id": row["group_id"],
                            "tool_call_id": row["tool_call_id"], "target_action_id": args["action_id"], "last_sig": sig, "next_seq": 2})
        d = self._delivery(row, payload, subscription=True)
        if self.store.cas(row["rec_key"], D, "decided", status=ActionStatus.COMPLETED.value, reason=Reason.OK, decided_at=self.clock(), delivery=d):
            self._audit("RapSubscribed", row["tenant"], {"rec_key": row["rec_key"], "target": args["action_id"]})

    @staticmethod
    def _sig(t: dict) -> str:
        return f"{t['state']}|{t['status']}|{t['reason']}|{t['receipt_ref']}"

    def _tick_subscriptions(self) -> int:
        n = 0
        for s in self.store.active_subs():
            t = self.store.newest_by_action(s["tenant"], s["target_action_id"])
            if t is None:
                continue
            sig = self._sig(t)
            if sig == s["last_sig"]:
                continue
            final = t["status"] in FINAL_STATUSES
            body = em.status_event(s["group_id"], s["tool_call_id"], t["action_id"], t["status"], t["reason"], t["receipt_ref"], final=final)
            d = {"event_id": "evt_" + uuid.uuid4().hex, "rec_key": s["rec_key"], "seq": s["next_seq"], "kind": "subscription_event",
                 "body_json": json.dumps(body, sort_keys=True), "next_at": 0.0, "created_at": self.clock()}
            if self.store.sub_event(s["rec_key"], sig, d, final=final):
                n += 1
        return n

    # ── delivery (the outbox) ─────────────────────────────────────────────────────────────────────────────────
    def _deliver(self, d: dict) -> None:
        row = self.store.get(d["rec_key"])
        now = self.clock()
        if row is None:
            self.store.mark_delivery(d["event_id"], "dropped", detail="no_action")
            return
        peer = self.peers.get(row["key_id"])
        if peer is None or peer.revoked or (peer.expires_at and now >= peer.expires_at):
            self.store.mark_delivery(d["event_id"], "dropped", detail="peer_revoked", attempts_inc=0)
            self._audit("RapDeliveryDropped", row["tenant"], {"event_id": d["event_id"], "reason": "peer_revoked"})
            return
        if now >= row["expires_at"]:
            self.store.mark_delivery(d["event_id"], "expired", detail="expired", attempts_inc=0)
            self._audit("RapDeliveryExpired", row["tenant"], {"event_id": d["event_id"], "rec_key": row["rec_key"]})
            return
        body = d["body_json"].encode()
        headers = callback_headers(peer, url=row["callback_url"], body=body, event_id=d["event_id"], operation=row["operation"], group_id=row["group_id"],
                                   tool_call_id=row["tool_call_id"], expires_at=row["expires_at"], now=now)
        out = self.callbacks.post(row["callback_url"], body, headers, peer.callback_origins)
        if out.kind == "delivered":
            self.store.mark_delivery(d["event_id"], "delivered", detail=str(out.status))
            self._audit("RapDelivered", row["tenant"], {"event_id": d["event_id"], "rec_key": row["rec_key"], "kind": d["kind"], "attempts": d["attempts"] + 1})
            if d["kind"] == "tool_result":
                self.store.purge_arguments(row["rec_key"])
        elif out.kind == "retry":
            backoff = min(self.max_backoff_s, 2.0 ** min(d["attempts"], 12))
            self.store.mark_delivery(d["event_id"], "pending", next_at=now + backoff, detail=f"{out.status}:{out.detail}")
        else:
            self.store.mark_delivery(d["event_id"], "failed", detail=f"{out.kind}:{out.status}:{out.detail}")
            self._audit("RapDeliveryFailed", row["tenant"], {"event_id": d["event_id"], "rec_key": row["rec_key"], "kind": out.kind, "status": out.status,
                                                              "detail": out.detail})

    # ── recovery ──────────────────────────────────────────────────────────────────────────────────────────────
    def recover(self) -> Dict[str, int]:
        """Run at startup (``start`` does). Decides every row left ``dispatching`` by a process that died, from the authority ledger only."""
        counts = {"redispatch": 0, "completed": 0, "unknown": 0}
        for row in self.store.in_state("dispatching"):
            if row["rec_key"] in self._live:
                continue
            spec = self.operations.get(row["operation"])
            if spec is None or not row["side_effecting"]:
                if self.store.cas(row["rec_key"], ("dispatching",), "accepted", next_at=0.0):
                    counts["redispatch"] += 1
                continue
            lease = self._lease(row["corr"])
            state = (lease or {}).get("state", "none" if self.gateway.authority is not None else "unknown")
            ref = ((lease or {}).get("meta") or {}).get("provider_ref")
            if state in ("none", "reserved", "released"):
                if self.store.cas(row["rec_key"], ("dispatching",), "accepted", next_at=0.0):
                    counts["redispatch"] += 1
            elif state == "settled" and ref:
                self._decide(row, ("dispatching",), ActionStatus.COMPLETED, Reason.OK, receipt=str(ref)[:128], extra_audit={"recovered": True})
                counts["completed"] += 1
            else:
                self._decide(row, ("dispatching",), ActionStatus.OUTCOME_UNKNOWN, Reason.OUTCOME_UNKNOWN, extra_audit={"recovered": True})
                counts["unknown"] += 1
        return counts

    # ── host operations (never reachable through the RAP endpoints) ──────────────────────────────────────────
    def receipt(self, tenant: str, action_id: str) -> Optional[dict]:
        r = self.store.newest_by_action(tenant, action_id)
        if r is None:
            return None
        return {k: r[k] for k in ("action_id", "operation", "state", "status", "reason", "receipt_ref", "corr", "created_at", "decided_at")}

    def reconcile(self, tenant: str, action_id: str, *, executed: bool, evidence: dict, actor: str, reason: str, result: Optional[dict] = None) -> dict:
        """Resolve an outcome_unknown action with evidence and a named person (the gateway's own rules). The runtime learns the truth through a
        watch or a status read; a result already delivered is never rewritten."""
        r = self.store.newest_by_action(tenant, action_id)
        if r is None or r["status"] != ActionStatus.OUTCOME_UNKNOWN.value:
            raise KeyError("this action is not in the outcome_unknown state")
        if executed and not (isinstance(result, dict) and result.get("provider_object_id")):
            raise ValueError("an executed outcome needs the downstream receipt (result with provider_object_id)")
        self.gateway.reconcile_call(r["corr"], executed=executed, evidence=evidence, actor=actor, reason=reason, result=result)
        status = ActionStatus.COMPLETED if executed else ActionStatus.FAILED
        ref = str(result["provider_object_id"])[:128] if executed else ""
        self.store.update(r["rec_key"], status=status.value, reason=Reason.OK, receipt_ref=ref, decided_at=self.clock())
        self._audit("RapReconciled", tenant, {"rec_key": r["rec_key"], "action_id": action_id, "status": status.value, "receipt_ref": ref, "actor": str(actor)[:80]})
        return self.receipt(tenant, action_id)

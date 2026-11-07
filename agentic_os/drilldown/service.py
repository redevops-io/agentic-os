"""The read-only drill-down service: Timeline, Evidence list, Evidence Inspect over a mission's persisted event log.

Design points (each is pinned by a test):

* **A store and nothing else.** The service is constructed with an event store, a permission ceiling, an audit sink and a
  scope function. It holds no execution engine and no reference to the running mission object, so a read can neither
  trigger a tool call nor change what the mission would do. It reads the DURABLE log, so it answers the same before and
  after a restart and for a mission nobody has loaded into memory.
* **Authorization on the ordinary permission plane**, the same ``evidence.<scope>.<action>`` names as the D-1 replay
  boundary. Reading is ``inspect``. The scope is the project. An unknown mission, a mission in another project and a
  caller without the grant all produce the same ``not_found``; the grant is evaluated on EVERY request, so a revocation
  takes effect on the next one.
* **Cited or unavailable.** Every entry carries the seq and sha256 of the event it was derived from; a field the log does
  not hold is an ``unavailable`` object with a reason, never a guess.
* **The principal is verified, not believed.** The service accepts only a principal that carries the proof the authentication layer
  issued (``mission.principal``: not a plain ``Principal``, not another issuer's, not edited, not older than ``principal_max_age_s``),
  and a mission recorded under a tenant is visible only to that tenant's principal (or the all-tenant one). Anything else is the same
  401 as no credential (verification) or the same 404 as an unknown mission (tenant).
* **Audited.** Each read appends a hash-chained receipt (who, scope, mission, head, lineage digest, counts, zero side
  effects); if the audit sink cannot write, nothing is returned.
"""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Callable, Optional

from ..evidence_query.audit import AuditSink, new_receipt
from ..evidence_query.authz import Ceiling, allowed, perm
from ..evidence_query.replay import lineage_digest
from . import fold
from .models import (ANCHOR_STATUSES, EVIDENCE_INSPECT_SCHEMA_ID, EVIDENCE_LIST_SCHEMA_ID, MAX_LIMIT, TIMELINE_SCHEMA_ID, DrilldownRefused, unavailable)

_PIN = re.compile(r"#(sha256:[0-9a-f]{64})$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


def _int(v: Any, lo: int, hi: Optional[int] = None) -> int:
    if isinstance(v, bool):
        raise DrilldownRefused("bad_request")
    if isinstance(v, str) and re.fullmatch(r"-?\d{1,12}", v):
        v = int(v)
    if not isinstance(v, int) or v < lo or (hi is not None and v > hi):
        raise DrilldownRefused("bad_request")
    return v


class DrilldownService:
    #: a principal is issued for one request; one older than this is a replay of an earlier authentication
    PRINCIPAL_MAX_AGE_S = 300.0

    def __init__(self, store, ceiling: Ceiling, audit: AuditSink, *, scope_for: Callable[[str], Optional[str]],
                 resolver: Optional[Callable[[str], Any]] = None, verifier: Optional[Callable[[Any], bool]] = None,
                 clock: Callable[[], float] = time.time, principal_max_age_s: float = PRINCIPAL_MAX_AGE_S):
        self.store = store
        self.ceiling = ceiling
        self.audit = audit
        self.scope_for = scope_for
        self.resolver = resolver
        self._verifier = verifier
        self._clock = clock
        self.principal_max_age_s = float(principal_max_age_s)

    # ───────────────────────── plumbing ─────────────────────────
    def _record(self, kind: str, principal: Any, scope: str, mission_id: str, **fields) -> None:
        mid = mission_id if isinstance(mission_id, str) and _SAFE_ID.match(mission_id) else "sha256:" + hashlib.sha256(str(mission_id).encode()).hexdigest()
        try:
            self.audit.append(new_receipt(kind, getattr(principal, "id", "") or "", scope or "", mission_id=mid, side_effects=0, **fields))
        except Exception:                                   # noqa: BLE001 - an unwritable audit log fails the request closed
            raise DrilldownRefused("audit_unavailable") from None

    def _verified(self, principal: Any) -> bool:
        """True only for a principal the authentication layer issued, recently. Fails closed on any error."""
        try:
            if self._verifier is not None:
                ok = bool(self._verifier(principal))
            else:
                from ..mission.principal import default_issuer      # noqa: PLC0415 - the process's authentication-layer issuer
                ok = default_issuer().verify(principal)
            if not ok:
                return False
            issued = getattr(principal, "issued_at", None)
            if isinstance(issued, (int, float)) and not isinstance(issued, bool) and issued > 0:
                return -5.0 <= self._clock() - float(issued) <= self.principal_max_age_s
            return self._verifier is not None                 # a principal with no issue time is believable only to an injected verifier
        except Exception:                                   # noqa: BLE001
            return False

    @staticmethod
    def _tenant_ok(principal: Any, events: list) -> bool:
        """A mission recorded under a tenant is for that tenant's principal (or the all-tenant admin) only; one recorded under none is open
        to whoever holds the project grant."""
        from ..mission.principal import ALL_TENANTS             # noqa: PLC0415
        created = events[0].payload if events and isinstance(events[0].payload, dict) else {}
        tenant = created.get("tenant")
        if not (isinstance(tenant, str) and tenant):
            return True
        return getattr(principal, "tenant", None) in (tenant, ALL_TENANTS)

    def _authorize(self, kind: str, principal: Any, mission_id: str, project_id: Optional[str]) -> tuple:
        """Returns (scope, events) or raises not_found / unauthenticated. Unknown, other-project, other-tenant and unauthorized are
        indistinguishable."""
        if not self._verified(principal):
            self._record(kind, None, "", mission_id, outcome="refused", reason="unauthenticated")      # nothing the caller claims is trusted or written
            raise DrilldownRefused("unauthenticated")
        scope = None
        try:
            scope = self.scope_for(mission_id) if isinstance(mission_id, str) else None
        except Exception:                                   # noqa: BLE001
            scope = None
        ok = False
        if principal is not None and isinstance(scope, str) and scope and (project_id is None or project_id == scope):
            try:
                ok = allowed(self.ceiling, principal, perm(scope, "inspect"))
            except ValueError:
                ok = False
        events = []
        if ok:
            refresh = getattr(self.store, "refresh", None)       # a log another process appends to: read what is there NOW
            if callable(refresh):
                try:
                    refresh()
                except Exception:                               # noqa: BLE001 - serve what is loaded; the integrity report says the rest
                    pass
            events = list(self.store.for_mission(mission_id))
            ok = bool(events) and events[0].type == "MissionCreated" and self._tenant_ok(principal, events)
        if not ok:
            self._record(kind, principal, scope if isinstance(scope, str) else "", mission_id, outcome="refused", reason="not_authorized")
            raise DrilldownRefused("not_found")
        return scope, events

    def _provenance(self) -> dict:
        return {"origin": "event_log", "sample_data": False, "read_only": True, "store": fold.store_descriptor(self.store)}

    @staticmethod
    def _head(events: list) -> dict:
        return {"seq": events[-1].seq, "event_count": len(events), "lineage_sha256": lineage_digest(events),
                "mission_state": fold.mission_state(events)}

    def _chain(self, events: list) -> Optional[dict]:
        """What the store's own checks say about this mission's log, or None for a store that runs none (then the UI says the server did
        not report it). ``ok`` is False if the hash chain of this mission OR the tail anchor of the log fails; ``anchored`` is True only
        when the anchor matches the log exactly."""
        check = getattr(self.store, "check_integrity", None)
        if not callable(check):
            return None
        from ..mission.store import verify_chain                # noqa: PLC0415
        try:
            anchor_fn = getattr(self.store, "check_anchor", None)
            a = anchor_fn() if callable(anchor_fn) else check()
            ch = verify_chain(events)
        except Exception:                                       # noqa: BLE001 - a store that fails to check reports nothing
            return None
        status = a.status if a.status in ANCHOR_STATUSES else "not_reported"
        return {"ok": bool(ch.ok and a.ok), "checked": ch.checked, "unchained": ch.unchained, "scope": "this mission",
                "anchored": status == "ok", "anchor": {"status": status, "records": int(a.count), "anchored_records": int(a.anchored)}}

    # ───────────────────────── Timeline ─────────────────────────
    def timeline(self, principal: Any, mission_id: str, *, project_id: Optional[str] = None, after_seq: Any = 0, limit: Any = 100,
                 expect_lineage: Optional[str] = None, upto: Any = None) -> dict:
        scope, events = self._authorize("drilldown.timeline", principal, mission_id, project_id)
        try:
            after = _int(after_seq, 0)
            lim = _int(limit, 1, MAX_LIMIT)
            if expect_lineage is not None and not (isinstance(expect_lineage, str) and len(expect_lineage) <= 80):
                raise DrilldownRefused("bad_request")
            up = _int(upto, 0) if upto is not None else None
        except DrilldownRefused:
            self._record("drilldown.timeline", principal, scope, mission_id, outcome="refused", reason="bad_request")
            raise
        head = self._head(events)
        later = [e for e in events if e.seq > after]
        shown = later[:lim]
        entries = []
        for e in shown:
            p = e.payload if isinstance(e.payload, dict) else {}
            entries.append({
                "seq": e.seq, "ts": e.ts, "type": e.type, "category": fold.category_of(e.type), "outcome": fold.outcome_of(e.type, p),
                "node_id": p.get("node_id") if isinstance(p.get("node_id"), str) else None,
                "capability": p.get("capability") if isinstance(p.get("capability"), str) else None,
                "summary": fold.summary_of(e.type, p), "actor": fold.actor_of(p), "payload": fold.payload_view(p), "cite": fold.cite(e)})
        check: dict = {"status": "not_requested"}
        if expect_lineage is not None:
            upto_seq = head["seq"] if up is None else up
            prefix = [e for e in events if e.seq <= upto_seq]
            check = {"status": "match" if lineage_digest(prefix) == expect_lineage else "mismatch", "upto_seq": upto_seq}
        out = {"schema": TIMELINE_SCHEMA_ID, "mission_id": mission_id, "provenance": self._provenance(), "head": head,
               "page": {"after_seq": after, "limit": lim, "returned": len(entries),
                        "next_after_seq": (entries[-1]["seq"] if len(later) > lim else None)},
               "entries": entries, "lineage_check": check}
        chain = self._chain(events)
        if chain is not None:
            out["chain"] = chain
        self._record("drilldown.timeline", principal, scope, mission_id, outcome="ok", reason="", head_seq=head["seq"],
                     lineage_sha256=head["lineage_sha256"], returned=len(entries))
        return out

    # ───────────────────────── Evidence ─────────────────────────
    @staticmethod
    def _items(events: list) -> list:
        caps = fold.node_capabilities(events)
        items = []
        for e in events:
            p = e.payload if isinstance(e.payload, dict) else {}
            nid = p.get("node_id") if isinstance(p.get("node_id"), str) else None
            cap = p.get("capability") if isinstance(p.get("capability"), str) else caps.get(nid or "")
            if e.type == "VerificationRecorded":
                items.append({"id": str(p.get("id") or f"verification:{e.seq}"), "kind": "verification", "capability": cap, "node_id": nid,
                              "summary": fold.summary_of("VerificationRecorded", p), "cite": fold.cite(e), "_event": e})
            elif e.type in ("AosddStepVerified", "AosddUndoVerified"):
                # an independent read-back the AOS-DD integration recorded (checks, the verifier kind exactly as recorded): a verification item
                items.append({"id": f"{'readback' if e.type == 'AosddStepVerified' else 'undo-readback'}:{e.seq}", "kind": "verification", "capability": None,
                              "node_id": None, "summary": fold.summary_of(e.type, p), "cite": fold.cite(e), "_event": e})
            elif e.type == "ClaimRaised":
                items.append({"id": str(p.get("id") or f"claim:{e.seq}"), "kind": "claim", "capability": None, "node_id": None,
                              "summary": (str(p.get("statement"))[:200] if p.get("statement") else "Claim raised"), "cite": fold.cite(e), "_event": e})
            elif e.type == "EvidenceRecorded":
                items.append({"id": str(p.get("id") or f"evidence:{e.seq}"), "kind": "evidence_record", "capability": None, "node_id": None,
                              "summary": f"Evidence from {p.get('source')}" if p.get("source") else "Evidence recorded", "cite": fold.cite(e), "_event": e})
            elif e.type == "NodeSucceeded":
                items.append({"id": f"result:{nid}:{e.seq}", "kind": "node_result", "capability": cap, "node_id": nid,
                              "summary": f"Result of {cap}" if cap else "Step result", "cite": fold.cite(e), "_event": e})
            elif e.type == "MissionCreated":
                for ref in (p.get("evidence_refs") or []):
                    if isinstance(ref, str) and ref:
                        items.append({"id": "ref:" + hashlib.sha256(ref.encode()).hexdigest()[:16], "kind": "mission_evidence_ref",
                                      "capability": None, "node_id": None, "summary": f"Sealed-intent evidence reference: {ref[:160]}",
                                      "cite": fold.cite(e), "_event": e, "_ref": ref})
        return items

    def evidence_list(self, principal: Any, mission_id: str, *, project_id: Optional[str] = None) -> dict:
        scope, events = self._authorize("drilldown.evidence", principal, mission_id, project_id)
        items = self._items(events)
        count = lambda k: sum(1 for i in items if i["kind"] == k)           # noqa: E731
        evidence_ids = {i["id"] for i in items if i["kind"] == "evidence_record"}
        cited = [x for i in items if i["kind"] == "verification" for x in (i["_event"].payload.get("evidence_ids") or [])]
        complete = bool(count("claim") and count("evidence_record") and all(x in evidence_ids for x in cited))
        gaps = []
        if not complete:
            gaps.append({"what": "claim_to_evidence_lineage", "reason": "not_recorded",
                         "detail": "the runtime records no claim or evidence events in a normal run and its verification records carry no evidence ids; "
                                   "only the verification outcome and the step results are persisted"})
        if not count("mission_evidence_ref"):
            gaps.append({"what": "mission_evidence_refs", "reason": "not_recorded",
                         "detail": "this mission was created without a sealed intent, so no evidence references were recorded"})
        out = {"schema": EVIDENCE_LIST_SCHEMA_ID, "mission_id": mission_id, "provenance": self._provenance(),
               "items": [{k: v for k, v in i.items() if not k.startswith("_")} for i in items],
               "coverage": {"claims": count("claim"), "evidence_records": count("evidence_record"), "verifications": count("verification"),
                            "node_results": count("node_result"), "mission_evidence_refs": count("mission_evidence_ref"),
                            "lineage_complete": complete, "unavailable": gaps}}
        self._record("drilldown.evidence", principal, scope, mission_id, outcome="ok", reason="", head_seq=events[-1].seq,
                     lineage_sha256=lineage_digest(events), returned=len(items))
        return out

    def evidence_inspect(self, principal: Any, mission_id: str, evidence_id: str, *, project_id: Optional[str] = None) -> dict:
        scope, events = self._authorize("drilldown.evidence_item", principal, mission_id, project_id)
        item = next((i for i in self._items(events) if i["id"] == evidence_id), None) if isinstance(evidence_id, str) else None
        if item is None:
            self._record("drilldown.evidence_item", principal, scope, mission_id, outcome="refused", reason="unknown_item")
            raise DrilldownRefused("not_found")
        e, p = item["_event"], (item["_event"].payload if isinstance(item["_event"].payload, dict) else {})
        reference = item.get("_ref")
        integrity = {"status": "no_recorded_hash", "recorded_sha256": None, "basis": "the runtime records no hash of this item when it is written"}
        content: Any
        produced_by: Any = unavailable("not_recorded", "the event does not say what produced this item")
        if item["kind"] == "node_result":
            content = fold.redact(p.get("result"))
            produced_by = {"node_id": item["node_id"] or "", "capability": item["capability"]}
        elif item["kind"] == "mission_evidence_ref":
            content, integrity = self._resolve(reference)
        else:
            content = fold.redact({k: v for k, v in p.items()})
            if item["node_id"]:
                produced_by = {"node_id": item["node_id"], "capability": item["capability"]}
        withheld = isinstance(content, dict) and content.get("available") is False
        if integrity["status"] == "recorded_hash_mismatch":
            computed = integrity["computed_sha256"]
        else:
            computed = None if withheld else fold.sha256_of(content)
        out = {"schema": EVIDENCE_INSPECT_SCHEMA_ID, "mission_id": mission_id, "id": item["id"], "kind": item["kind"], "reference": reference,
               "content": content, "content_sha256": computed, "content_sha256_basis": ("computed_at_read_time" if computed else None),
               "integrity": integrity, "produced_by": produced_by, "cite": item["cite"], "provenance": self._provenance()}
        self._record("drilldown.evidence_item", principal, scope, mission_id, outcome="ok", reason="", head_seq=events[-1].seq,
                     lineage_sha256=lineage_digest(events), item_kind=item["kind"], integrity=integrity["status"])
        return out

    def _resolve(self, ref: str) -> tuple:
        pin = _PIN.search(ref or "")
        recorded = pin.group(1) if pin else None
        base = {"status": "no_recorded_hash", "recorded_sha256": None, "basis": "the reference carries no hash pin"}
        if recorded:
            base = {"status": "recorded_hash_unchecked", "recorded_sha256": recorded, "basis": "pin in the recorded reference; content unavailable"}
        if self.resolver is None:
            return unavailable("source_offline", "no resolver is configured for this reference"), base
        try:
            body = self.resolver(ref)
        except Exception:                                   # noqa: BLE001 - never echo a resolver error to the caller
            body = None
        if body is None:
            return unavailable("source_offline", "the source did not return this reference"), base
        if not recorded:
            return fold.redact(body), base
        got = fold.sha256_of(body)
        if got == recorded:
            return fold.redact(body), {"status": "recorded_hash_verified", "recorded_sha256": recorded, "basis": "pin in the recorded reference"}
        return (unavailable("integrity_failed", "the resolved content does not match the hash pinned in the recorded reference"),
                {"status": "recorded_hash_mismatch", "recorded_sha256": recorded, "basis": "pin in the recorded reference", "computed_sha256": got})

"""Projects projections backed by the products' own APIs.

``ProductProjectionsMixin`` layers on whatever ProjectionProvider Projects selected (``mixin_over``): the existing
Missions, Attention, Activity, Apps and Today views then show REAL progress, approvals, results and evidence read from
each product, with the product's own record ids in ``source_refs``. Rules:

* nothing is stored: rows are derived per request from a short read cache (``hub.cached``, a few seconds, never
  authoritative, never used for a decision);
* a product that is not configured, not reachable or gated yields an explicit ``unavailable`` row (and an Apps row that
  says why), never a fabricated mission and never silence;
* the sample demo rows of the base provider are dropped when products are configured (they would sit next to real rows
  and read as real) unless the operator set ``keep_sample_rows: true`` in the registry;
* Today (the shipped UI's only approve control): a product approval is a Today ``case`` whose approve button drives the
  decision, and, after the product shows ``APPROVED``, the SAME button drives the separate execute step. Both are
  ``human_only`` and need an identifiable principal (``principal.py``); the digest the button binds changes between the
  two phases, so a double click can never decide and execute in one gesture.
"""
from __future__ import annotations

import hashlib
import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import approvals, qa as qamod, sentinel as sn
from .client import ProductError
from .hub import ProductHub
from .principal import current_principal
from .registry import ProductSpec

_TENANT = "demo"          # agentic_os.today.TodaySessions is built with this tenant in SampleProjectionProvider._today
MAX_ROWS = 50


def _prov(runtime: str, *refs: str) -> Dict[str, Any]:
    return {"source_runtime": runtime, "source_refs": list(refs)}


def _ago(checked_at: float) -> str:
    n = max(0, int(time.time() - checked_at))
    return "Verified just now" if n < 5 else f"Verified {n} s ago" if n < 120 else f"Verified {n // 60} min ago"


# ── collection (read cache, never authoritative) ─────────────────────────────────────────────────
def _actions(hub: ProductHub, spec: ProductSpec) -> List[Mapping[str, Any]]:
    def fetch():
        r = hub.sentinel(spec.id).actions(limit=MAX_ROWS, actor="projects:projection", source="projects")
        return [hub.with_mode(spec.id, a) for a in (r.get("items") or []) if isinstance(a, Mapping)] if isinstance(r, Mapping) else []
    return hub.cached(f"{spec.id}:actions", fetch)


def _runs(hub: ProductHub, spec: ProductSpec) -> List[Mapping[str, Any]]:
    def fetch():
        r = hub.qa(spec.id).runs(limit=25, actor="projects:projection", source="projects")
        return [x for x in (r.get("items") or []) if isinstance(x, Mapping)] if isinstance(r, Mapping) else []
    return hub.cached(f"{spec.id}:runs", fetch)


def _changes(hub: ProductHub, spec: ProductSpec) -> List[Mapping[str, Any]]:
    def fetch():
        r = hub.sentinel(spec.id).changes(after=0, limit=40, actor="projects:projection")
        return [c for c in (r.get("items") or []) if isinstance(c, Mapping)] if isinstance(r, Mapping) else []
    return hub.cached(f"{spec.id}:changes", fetch)


def _records(hub: ProductHub, spec: ProductSpec) -> Tuple[List[Mapping[str, Any]], Optional[ProductError]]:
    """The product's action/run records, or the error that stopped us from reading them."""
    try:
        return (_actions(hub, spec) if spec.kind == "security" else _runs(hub, spec)), None
    except ProductError as e:
        hub.invalidate(spec.id)
        return [], e


def _is_gate(u: Mapping[str, str]) -> bool:
    return str(u.get("code", "")).endswith("not-met")


def unavailable_rows(hub: ProductHub, spec: ProductSpec, reason: str) -> Dict[str, Dict[str, Any]]:
    refs = _prov("security" if spec.kind == "security" else "quality", f"product:{spec.id}")
    return {
        "mission": {"id": f"{spec.id}:unavailable", "title": f"{spec.display_name} is unavailable",
                    "workflow": f"{spec.display_name} response" if spec.kind == "security" else "QA runs",
                    "state": "failed", "progress": "Unavailable", "context_used": [reason], **refs},
        "attention": {"id": f"{spec.id}:unavailable", "kind": "reconnect", "title": f"Reconnect {spec.display_name}",
                      "reason": reason, "consequence": "Approvals, progress and results from this product are not shown "
                                                       "until it is reachable again.",
                      "available_actions": ["Connect"], "priority": 70, **refs},
        "activity": {"id": f"{spec.id}:unavailable", "time": "now", "text": f"{spec.display_name} unavailable: {reason}",
                     **_prov("connector", f"product:{spec.id}")},
    }


# ── the rows ──────────────────────────────────────────────────────────────────────────────────────
def product_apps(hub: ProductHub) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for spec in hub.specs():
        st = hub.status(spec.id)
        h = st["health"]["state"]
        ready = bool(st["ready"])
        row = {
            "provider": spec.id, "display_name": spec.display_name,
            "used_for": ("Security cases, proposed actions and approvals from the CrowdSec edge." if spec.kind == "security"
                         else "Frozen test plans run through pinned engines, with evidence you can re-verify."),
            "state": "MISSION_READY" if ready else "NOT_CONNECTED",
            "health": "ok" if ready else "warn" if h == "degraded" else "bad" if h == "down" else "mut",
            "verified": _ago(st["health"]["checked_at"]) if ready else "",
            "capabilities": [c for c in (spec.capabilities or ()) if ready] if spec.capabilities else
                            ([] if not ready else _default_caps(spec)),
            "setup_url": spec.ui_url,
            "manual_steps": [f"{u.get('reason', '')}" + (f" {u.get('remedy')}." if u.get("remedy") else "")
                             for u in st["unavailable"] if u.get("reason")],
            "required_scopes": [], "credential_fields": [],
            **_prov("security" if spec.kind == "security" else "quality", f"product:{spec.id}")}
        out.append(row)
    return out


def _default_caps(spec: ProductSpec) -> List[str]:
    from .schemas import INPUT_SCHEMAS
    return sorted(c for c in INPUT_SCHEMAS if c.startswith(spec.kind + "."))


def extend_apps(hub: Optional[ProductHub], apps: List[dict]) -> List[dict]:
    if hub is None:
        return apps
    ids = {a.get("provider") for a in product_apps(hub)}
    return [a for a in apps if a.get("provider") not in ids] + product_apps(hub)


def product_missions(hub: ProductHub) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for spec in hub.specs():
        st = hub.status(spec.id)
        if not st["ready"]:
            u = hub.unavailable_reason(spec.id)
            if not _is_gate(u):
                rows.append(unavailable_rows(hub, spec, u.get("reason", "unavailable"))["mission"])
            continue
        recs, err = _records(hub, spec)
        if err is not None:
            rows.append(unavailable_rows(hub, spec, err.detail or err.code)["mission"])
            continue
        for r in recs:
            rows.append(sn.mission_row(spec, r) if spec.kind == "security" else qamod.mission_row(spec, r))
    return rows


def product_attention(hub: ProductHub) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for spec in hub.specs():
        st = hub.status(spec.id)
        if not st["ready"]:
            u = hub.unavailable_reason(spec.id)
            if not _is_gate(u):
                rows.append(unavailable_rows(hub, spec, u.get("reason", "unavailable"))["attention"])
            continue
        recs, err = _records(hub, spec)
        if err is not None:
            rows.append(unavailable_rows(hub, spec, err.detail or err.code)["attention"])
            continue
        for r in recs:
            it = sn.attention_item(spec, r) if spec.kind == "security" else qamod.attention_item(spec, r)
            if it:
                rows.append(it)
    return rows


def product_activity(hub: ProductHub) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for spec in hub.specs():
        st = hub.status(spec.id)
        if not st["ready"]:
            u = hub.unavailable_reason(spec.id)
            if not _is_gate(u):
                rows.append(unavailable_rows(hub, spec, u.get("reason", "unavailable"))["activity"])
            continue
        try:
            if spec.kind == "security":
                for ch in _changes(hub, spec)[-20:][::-1]:
                    rows.append({"id": f"{spec.id}:chg-{ch.get('seq')}", "time": str(ch.get("at", ""))[11:16] or "",
                                 "text": sn.activity_text(ch), **_prov("security", f"{spec.id}:{ch.get('request_id') or ch.get('case_id') or ''}")})
            else:
                for r in _runs(hub, spec)[:10]:
                    rows.append({"id": f"{spec.id}:run-{r.get('id')}", "time": str(r.get("finished") or r.get("created") or "")[11:16],
                                 "text": f"QA run {r.get('id')} {r.get('state')}", **_prov("quality", f"{spec.id}:{r.get('id')}")})
        except ProductError as e:
            rows.append(unavailable_rows(hub, spec, e.detail or e.code)["activity"])
    return rows


def _evidence_items(spec: ProductSpec, bundle: Mapping[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for ev in bundle.get("evidence") or []:
        if not isinstance(ev, Mapping):
            continue
        aid = str(ev.get("artifact_id") or ev.get("id") or "")
        items.append({
            "source_id": aid or "evidence", "source_name": str(ev.get("source") or ev.get("kind") or "evidence"),
            "provider": spec.id, "kind": "app_evidence", "evidence_kind": "query",
            "retrieved": {"count": 1, "observed_at": str(ev.get("acquired_at") or "")},
            "identity": {"fingerprint": str(ev.get("sha256") or ""), "version": ""} if ev.get("sha256") else None,
            "refs": [{"ref": f"{spec.id}:{aid}", "summary": str(ev.get("summary") or ev.get("kind") or "")}],
            "why": "content-addressed evidence the product holds; open it in the product to inspect the raw record"})
    return items


def product_mission_detail(hub: ProductHub, mission_id: str) -> Optional[Dict[str, Any]]:
    """Detail for ``<product>:<record id>`` or None when it is not a product mission."""
    pid, _, rec = mission_id.partition(":")
    spec = hub.spec(pid)
    if spec is None or not rec:
        return None
    if rec == "unavailable":
        u = hub.unavailable_reason(pid)
        row = unavailable_rows(hub, spec, u.get("reason", "unavailable"))["mission"]
        return {"summary": row, "steps": [], "context_used": [], "context_plan_note": u.get("remedy", "")}
    if not hub.ready(pid):
        return None
    try:
        if spec.kind == "security":
            a = hub.with_mode(pid, hub.sentinel(pid).action(rec, actor="projects:projection"))
            ctx: List[Dict[str, Any]] = []
            if a.get("case_id"):
                try:
                    ctx = _evidence_items(spec, hub.sentinel(pid).case(str(a["case_id"]), actor="projects:projection"))
                except ProductError:
                    ctx = []
            return {"summary": sn.mission_row(spec, a), "steps": sn.mission_steps(a), "context_used": ctx,
                    "context_plan_note": f"Read from {spec.display_name}'s own API; the record ids are the product's. "
                                         + (sn.verification_line(a) or "")}
        r = hub.qa(pid).run(rec, actor="projects:projection")
        ev = hub.qa(pid).events(rec, 0, 200, actor="projects:projection")
        steps = qamod.mission_steps([e for e in (ev.get("items") or []) if isinstance(e, Mapping)])
        return {"summary": qamod.mission_row(spec, r), "steps": steps, "context_used": [],
                "context_plan_note": f"Read from {spec.display_name}'s own API; level {r.get('level') or 'n/a'}."}
    except ProductError:
        return None


# ── Today integration ─────────────────────────────────────────────────────────────────────────────
def _phase(a: Mapping[str, Any]) -> str:
    if sn.settled_dry_run(a):
        return ""
    return {"AWAITING_APPROVAL": "decide", "APPROVED": "execute"}.get(str(a.get("state")), "")


def phase_digest(a: Mapping[str, Any]) -> Optional[str]:
    ph = _phase(a)
    d = str(a.get("digest") or "")
    if not ph or not d:
        return None
    return d if ph == "decide" else "sha256:" + hashlib.sha256(("execute:" + d).encode()).hexdigest()


def _today_state(a: Mapping[str, Any]) -> Tuple[str, str]:
    st = str(a.get("state"))
    v = a.get("verification") if isinstance(a.get("verification"), Mapping) else {}
    vs = str(v.get("state") or "")
    if st == "AWAITING_APPROVAL":
        return "needs_action", "Awaiting your approval. Nothing has been blocked."
    if sn.settled_dry_run(a):
        return "verified", "Dry run: nothing was blocked. Enable blocking in the product to execute for real."
    if st == "APPROVED":
        d = a.get("decision") if isinstance(a.get("decision"), Mapping) else {}
        return "needs_action", (f"Approved by {d.get('actor') or d.get('actor_user') or 'a named user'}; click again to execute. "
                                "Execution is a separate step.")
    if st == "EXECUTED":
        if vs in ("verified", "dry_run"):
            return "verified", sn.verification_line(a)
        if vs in ("unverified", "failed"):
            return "failed", sn.verification_line(a)
        return "waiting", "Executed; the read-back has not been recorded yet."
    if st == "FAILED":
        return "failed", "The product could not execute the action; nothing was blocked."
    if st == "REJECTED":
        return "dismissed", "Rejected."
    return "waiting", st.lower()


def today_case(spec: ProductSpec, a: Mapping[str, Any], pid: str, att_id: str, now: float) -> Dict[str, Any]:
    from agentic_os.today import _item, stable_id   # noqa: PLC0415
    state, line = _today_state(a)
    st = str(a.get("state"))
    dec = a.get("decision") if isinstance(a.get("decision"), Mapping) else {}
    rid = str(a.get("request_id", ""))
    it = _item(tenant=_TENANT, project=pid, key=f"attention:{att_id}", dedupe=f"product:{spec.id}:{rid}",
               title=sn.action_title(a).replace("Approve ", "Execute ") if st == "APPROVED" and not sn.settled_dry_run(a) else sn.action_title(a),
               reason=line or sn.action_consequence(a), state=state, score=0.9,
               label="Execute the approved action" if st == "APPROVED" else "Approve the proposal", executable=True,
               source_refs=[f"{spec.id}:{rid}"] + ([f"{spec.id}:{a['case_id']}"] if a.get("case_id") else []),
               confidence=None, now=now, kind="case", domain="security", channel="CrowdSec", resurfaced=False,
               lifecycle={"suggested": True, "approved": st in ("APPROVED", "EXECUTED"),
                          "sent": st == "EXECUTED" or sn.settled_dry_run(a), "delivery_verified": state == "verified",
                          "outcome_observed": False},
               simulated=False, status_line=line, evidence_refs=[f"{spec.id}:{rid}"],
               approval_ref=(str(dec.get("decision_id") or "") or None) if dec else None,
               last_receipt_ref=(str((a.get("receipt") or {}).get("receipt_id") or "") or None) if isinstance(a.get("receipt"), Mapping) else None)
    it["id"] = stable_id(_TENANT, pid, f"attention:{att_id}")
    return it


def today_detail(spec: ProductSpec, a: Mapping[str, Any], pid: str, att_id: str, now: float) -> Dict[str, Any]:
    item = today_case(spec, a, pid, att_id, now)
    digest = phase_digest(a)
    st = str(a.get("state"))
    ip = sn.action_ip(a)
    hist = []
    for d in ([a["decision"]] if isinstance(a.get("decision"), Mapping) else []):
        hist.append({"at": now, "kind": "approved" if d.get("approved") else "rejected",
                     "text": f"{'Approved' if d.get('approved') else 'Rejected'} by {d.get('actor') or d.get('actor_user') or 'a named user'}"
                             + (f" via {d['via']}" if d.get("via") else "")})
    if isinstance(a.get("receipt"), Mapping):
        r = a["receipt"]
        hist.append({"at": now, "kind": "executed", "text": f"Receipt {r.get('receipt_id') or r.get('id') or ''}: {r.get('status', '')}".strip()})
    vl = sn.verification_line(a)
    if vl:
        hist.append({"at": now, "kind": "verified" if "Verified" in vl else "unverified", "text": vl})
    item.update({
        "why": f"{spec.display_name} proposed this on case {a.get('case_id', '')}. {sn.action_consequence(a)}",
        "evidence": [{"kind": "case", "label": f"Case {a.get('case_id', '')}", "observed_at": now,
                      "ref": f"{spec.id}:{a.get('case_id', '')}"}],
        "action": {
            "kind": "product_action", "channel": "CrowdSec " + ("unban" if sn.is_unban(a) else "ban"),
            "to": [{"id": ip, "label": ip or "address", "address": ""}], "skipped": [],
            "subject": sn.action_title(a), "body": sn.action_consequence(a) + " " + sn.read_back_text(a),
            "payload_digest": digest, "attempt": int(a.get("attempts") or 0),
            "will_happen": ("Executes the approved action through the product, which then re-reads the edge. "
                            if st == "APPROVED" else "Records your approval in the product, attributed to you. "
                                                     "It does not execute the action. "),
            "nothing_sent_yet": False,
            "reconcile_required": False,
            "policy": {"decision": "requires_approval",
                       "reason": "The product's own mandatory approval and read-back apply; this button only carries your click."},
            "blocked_reason": "" if digest else "This action is not waiting for a decision.",
            "disambiguate": [], "can_approve": digest is not None},
        "history": hist, "follow_up": {"at": None, "after_seconds": 0},
        "receipt": ({"status": str(a["receipt"].get("status", "")), "receipt_id": str(a["receipt"].get("receipt_id") or a["receipt"].get("id") or ""),
                     "provider": spec.id} if isinstance(a.get("receipt"), Mapping) else None)})
    return item


class ProductProjectionsMixin:
    """Mixed in front of the selected provider by :func:`mixin_over`; ``products_hub`` is its only state."""

    products_hub: ProductHub
    _products_today: Dict[str, Tuple[str, str, str]]       # today item id -> (product id, request id, attention id)

    # -- the four existing views ---------------------------------------------------------------
    def _keep(self) -> bool:
        return bool(self.products_hub.registry.keep_sample_rows)

    def missions(self, project_id: str) -> List[dict]:
        base = super().missions(project_id) if self._keep() else []   # type: ignore[misc]
        return list(base) + product_missions(self.products_hub)

    def mission_detail(self, project_id: str, mission_id: str) -> dict:
        d = product_mission_detail(self.products_hub, mission_id)
        if d is not None:
            return d
        return super().mission_detail(project_id, mission_id) if self._keep() else {}   # type: ignore[misc]

    def attention(self, project_id: str) -> List[dict]:
        base = super().attention(project_id) if self._keep() else []   # type: ignore[misc]
        return list(base) + product_attention(self.products_hub)

    def activity(self, project_id: str) -> List[dict]:
        base = super().activity(project_id) if self._keep() else []   # type: ignore[misc]
        return list(base) + product_activity(self.products_hub)

    def priorities(self, project_id: str) -> dict:
        out = super().priorities(project_id)   # type: ignore[misc]
        if not self._keep() and isinstance(out, dict) and out.get("basis") == "sample":
            # the base provider's sample fallback would put demo "needs you" items next to real product approvals
            return {"summary": "Nothing needs you right now.", "surfaced": [], "deferred": [],
                    "handled_automatically": 0, "abstained": 0, "basis": "products"}
        return out

    def apps(self, project_id: str) -> List[dict]:
        return extend_apps(self.products_hub, list(super().apps(project_id)))   # type: ignore[misc]

    # -- Today -----------------------------------------------------------------------------------
    def _remember(self, item_id: str, pid: str, rid: str, att: str) -> None:
        m = self.__dict__.setdefault("_products_today", {})
        if len(m) > 500:
            m.clear()
        m[item_id] = (pid, rid, att)

    def today(self, pid: str, session: str = "") -> dict:
        feed = super().today(pid, session)   # type: ignore[misc]
        hub = self.products_hub
        from agentic_os.today import STATES, stable_id   # noqa: PLC0415
        now = feed.get("generated_at") or time.time()
        by_id = {it["id"]: i for i, it in enumerate(feed.get("items", []))}
        for spec in hub.specs():
            if spec.kind != "security" or not hub.status(spec.id)["ready"]:
                continue
            try:
                acts = _actions(hub, spec)
            except ProductError:
                continue
            for a in acts:
                if not _phase(a):
                    continue
                att = f"{spec.id}:{a.get('request_id')}"
                tid = stable_id(_TENANT, pid, f"attention:{att}")
                case = today_case(spec, a, pid, att, now)
                self._remember(tid, spec.id, str(a.get("request_id")), att)
                if tid in by_id:
                    feed["items"][by_id[tid]] = case
                else:
                    feed["items"].append(case)
        if not feed.get("simulated_actions") and any(i.get("kind") == "case" and i.get("domain") == "security" for i in feed["items"]):
            feed["basis"] = "live"             # real product records; the "sample data" banner would be false
        counts = {s: 0 for s in STATES}
        for it in feed["items"]:
            counts[it["state"]] += 1
        feed["counts"] = counts
        n = counts["needs_action"]
        feed["headline"] = (f"{n} thing{'s' if n != 1 else ''} need{'s' if n == 1 else ''} your decision." if n
                            else "Nothing needs you right now.")
        return feed

    def _product_for_item(self, item_id: str):
        ent = self.__dict__.get("_products_today", {}).get(item_id)
        if ent is None:
            return None
        spec = self.products_hub.spec(ent[0])
        return (spec,) + ent[1:] if spec else None

    def today_item(self, pid: str, item_id: str, session: str = "", targets=None, identity_id=None) -> dict:
        ent = self._product_for_item(item_id)
        if ent is None:
            self.today(pid, session)                    # (re)learn the product item ids, then fall through
            ent = self._product_for_item(item_id)
        if ent is None:
            return super().today_item(pid, item_id, session, targets, identity_id)   # type: ignore[misc]
        spec, rid, att = ent
        from agentic_os.today import TodayError   # noqa: PLC0415
        try:
            a = self.products_hub.sentinel(spec.id).action(rid, actor="projects:projection")
        except ProductError as e:
            raise TodayError(503 if e.unavailable else 404, e.code, e.detail or e.code)
        return today_detail(spec, a, pid, att, time.time())

    def today_approve(self, pid: str, item_id: str, session: str = "", **kw) -> dict:
        ent = self._product_for_item(item_id)
        if ent is None:
            self.today(pid, session)
            ent = self._product_for_item(item_id)
        if ent is None:
            return super().today_approve(pid, item_id, session, **kw)   # type: ignore[misc]
        from agentic_os.today import TodayError   # noqa: PLC0415
        spec, rid, att = ent
        hub = self.products_hub
        principal = current_principal(hub.registry.single_owner)
        if not principal:
            raise TodayError(401, "principal_required", "Sign in to Projects so the product can attribute your decision.")
        expected = str(kw.get("expected_digest") or "")
        try:
            a = hub.with_mode(spec.id, hub.sentinel(spec.id).action(rid, actor=f"projects:{principal}"))
            st = str(a.get("state"))
            if st == "AWAITING_APPROVAL":
                if expected != phase_digest(a):
                    raise TodayError(409, "stale_approval", "the action changed since you reviewed it; review it again")
                approvals.sentinel_decision(hub, spec.id, rid, approved=True, digest=str(a.get("digest")),
                                            rationale="", principal=principal)
            elif st == "APPROVED":
                if expected != phase_digest(a):
                    raise TodayError(409, "stale_approval", "the action changed since it was approved; review it again")
                approvals.sentinel_execute(hub, spec.id, rid, digest=str(a.get("digest")), principal=principal)
            else:
                raise TodayError(409, "not_actionable", f"the action is {st.lower()}; nothing to approve")
            a2 = hub.with_mode(spec.id, hub.sentinel(spec.id).action(rid, actor=f"projects:{principal}"))
        except ProductError as e:
            status = e.status or (503 if e.unavailable else 409)
            raise TodayError(status if status in (401, 403, 404, 409, 422, 503) else 409, e.code,
                             e.reason or e.detail or e.code)
        d = today_detail(spec, a2, pid, att, time.time())
        d["replayed"] = False
        return d


def mixin_over(provider: Any, hub: ProductHub) -> Any:
    """Return ``provider`` with the products mixin in front of its class (same object, same state)."""
    if isinstance(provider, ProductProjectionsMixin):
        provider.products_hub = hub
        return provider
    cls = type("Products" + type(provider).__name__, (ProductProjectionsMixin, type(provider)), {})
    try:
        provider.__class__ = cls
    except TypeError as e:   # pragma: no cover - a provider with an incompatible layout
        raise RuntimeError(f"cannot layer the products projections over {type(provider).__name__}: {e}") from None
    provider.products_hub = hub
    return provider


__all__ = ["ProductProjectionsMixin", "mixin_over", "product_apps", "extend_apps", "product_missions",
           "product_attention", "product_activity", "product_mission_detail", "phase_digest", "today_case",
           "today_detail"]

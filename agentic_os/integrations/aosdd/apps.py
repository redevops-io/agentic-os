"""The four write adapters (ERPNext, Lago, Twenty, Chatwoot): find-by-marker, write, undo, deep links.

Every adapter works from the APPROVED plan step (target, derived ids, payload) and the operation id; none of them invents a target.
``find`` looks the application up by the marker that the op id wrote into the record, so after a crash or a lost reply the
runtime adopts the existing record instead of creating a second one (ERPNext and Chatwoot have no native de-duplication).
Observed quirks are handled here, not hidden: Lago metadata is replace-whole-set (guarded: refuse when the invoice carries other
metadata, clear only when our entry is the only one), Twenty keeps a soft-deleted id (the id is derived from the op id, so a new
attempt is a new op id), Chatwoot deletes a message as a tombstone."""
from __future__ import annotations

import datetime
import json
import urllib.parse

from . import opid
from .http import AppError, AppHttp
from .context import LAGO_KEY, q

#: A conversation can hold more messages than one page (Chatwoot returns the newest 20; `?before=<id>` the 20 older than that id). The marker
#: lookup must see ALL of them: follow the pages until an empty one, up to this many pages; anything short of that is "incomplete".
MESSAGE_PAGE_CAP = 100
UI_NOTE_CHATWOOT = "Chatwoot has no per-message URL; the link opens the conversation that contains the note"


def ui_url(app: str, base: str, **k) -> str:
    base = base.rstrip("/")
    if app == "twenty":
        return f"{base}/object/{k.get('object', 'task')}/{k['id']}"
    if app == "chatwoot":
        return f"{base}/app/accounts/{k.get('account', 1)}/conversations/{k['conversation']}"
    if app == "erpnext":
        return f"{base}/desk/{k.get('doctype_slug', 'quotation')}/{q(k['name'])}"
    raise KeyError(app)          # Lago: the API returns the URL (invoice.web_url); it is never built here


def _ok(result: dict, **extra) -> dict:
    return {"status": "ok", "executed": True, **result, **extra}


# ---------------------------------------------------------------------------------------------------------------- ERPNext
class ErpNext:
    app = "erpnext"

    def __init__(self, http: AppHttp, base: str):
        self.h, self.base = http, base

    def find(self, step: dict) -> "dict | None":
        title = step["derived"]["title"]
        r = self.h.check(self.h.get("/api/resource/Quotation?filters=%s&fields=%s" % (q(json.dumps([["title", "=", title]])), q('["name","docstatus"]'))), (200,), op="erp.find")
        rows = r.body.get("data") or []
        if len(rows) > 1:
            raise AppError("erpnext", "conflict", 200, f"{len(rows)} quotations carry the marker {title}; refusing to pick one", op="erp.find")
        if not rows:
            return None
        return self._result(rows[0]["name"], created=False)

    def _result(self, name: str, created: bool, **extra) -> dict:
        return _ok({"native_id": name, "native_ids": {"quotation": name}, "created": created, "provider_ref": name,
                    "ui_url": ui_url("erpnext", self.base, doctype_slug="quotation", name=name), "ui_url_note": ""}, **extra)

    def write(self, step: dict, _inputs: dict) -> dict:
        body = dict(step["payload"])
        body["title"] = step["derived"]["title"]
        r = self.h.check(self.h.request("POST", "/api/resource/Quotation", body), (200, 201), op="erp.write")
        d = r.body["data"]
        return self._result(d["name"], created=True, docstatus=d.get("docstatus"), grand_total=d.get("grand_total"),
                            request={"method": "POST", "path": "/api/resource/Quotation"})

    def undo(self, step: dict, result: dict) -> dict:
        r = self.h.request("DELETE", "/api/resource/Quotation/" + q(result["native_id"]))
        if r.status == 404:
            return _ok({"undone": True, "note": "already absent"})
        self.h.check(r, (200, 202), op="erp.undo")
        return _ok({"undone": True, "request": {"method": "DELETE", "path": "/api/resource/Quotation/" + result["native_id"]}})


# ---------------------------------------------------------------------------------------------------------------- Lago
class Lago:
    app = "lago"

    def __init__(self, http: AppHttp, base: str):
        self.h, self.base = http, base

    def _invoice(self, step: dict) -> dict:
        r = self.h.check(self.h.get("/api/v1/invoices/" + step["target"]["invoice_lago_id"]), (200,), op="lago.read")
        return r.body["invoice"]

    def _result(self, step: dict, inv: dict, entry: dict, created: bool, **extra) -> dict:
        return _ok({"native_id": inv["lago_id"], "native_ids": {"invoice": inv["lago_id"], "metadata": entry["lago_id"]}, "created": created,
                    "metadata_key": entry["key"], "metadata_value": entry["value"], "provider_ref": inv["lago_id"],
                    "ui_url": inv.get("web_url") or "", "ui_url_note": "" if inv.get("web_url") else "the API returned no web_url"}, **extra)

    def find(self, step: dict) -> "dict | None":
        inv = self._invoice(step)
        for m in inv.get("metadata") or []:
            if m["key"] == step["target"]["metadata_key"] and str(m["value"]).startswith(step["op_id"] + " "):
                return self._result(step, inv, m, created=False)
        return None

    def write(self, step: dict, inputs: dict) -> dict:
        inv = self._invoice(step)
        existing = inv.get("metadata") or []
        if existing:   # PUT replaces the whole set and 422s on an unchanged entry: never clobber another writer's metadata
            raise AppError("lago", "conflict", 409, "domain guard: the invoice already carries metadata keys=%s (one active recovery case per invoice)" % [m["key"] for m in existing], op="lago.write")
        value = ("%s %s" % (step["op_id"], inputs.get("quotation", "")))[:100]
        r = self.h.check(self.h.request("PUT", "/api/v1/invoices/" + inv["lago_id"], {"invoice": {"metadata": [{"key": step["target"]["metadata_key"], "value": value}]}}), (200,), op="lago.write")
        inv2 = r.body["invoice"]
        entry = next((m for m in inv2.get("metadata") or [] if m["key"] == step["target"]["metadata_key"]), None)
        if entry is None:
            raise AppError("lago", "server", 200, "the PUT answered 200 but the metadata entry is not in the response", op="lago.write")
        return self._result(step, inv2, entry, created=True, request={"method": "PUT", "path": "/api/v1/invoices/" + inv["lago_id"]})

    def undo(self, step: dict, result: dict) -> dict:
        inv = self._invoice(step)
        md = inv.get("metadata") or []
        ours = [m for m in md if m["key"] == step["target"]["metadata_key"] and str(m["value"]).startswith(step["op_id"] + " ")]
        if not md or not ours:
            return _ok({"undone": True, "note": "our entry was already absent"})
        if len(md) != 1:
            return {"status": "refused", "executed": False, "undone": False, "undo_error": "invoice metadata is not exactly our entry (replace-whole-set would clobber %d other entries)" % (len(md) - 1)}
        self.h.check(self.h.request("PUT", "/api/v1/invoices/" + inv["lago_id"], {"invoice": {"metadata": []}}), (200,), op="lago.undo")
        return _ok({"undone": True, "request": {"method": "PUT", "path": "/api/v1/invoices/" + inv["lago_id"], "body": "metadata: []"}})


# ---------------------------------------------------------------------------------------------------------------- Twenty
class Twenty:
    app = "twenty"

    def __init__(self, http: AppHttp, base: str):
        self.h, self.base = http, base

    def _result(self, step: dict, created: bool, **extra) -> dict:
        tid = step["derived"]["task_id"]
        return _ok({"native_id": tid, "native_ids": {"task": tid, "task_target": step["derived"]["target_id"]}, "created": created, "provider_ref": tid,
                    "ui_url": ui_url("twenty", self.base, object="task", id=tid), "ui_url_note": ""}, **extra)

    def _target_exists(self, step: dict) -> bool:
        d = step["derived"]
        r = self.h.check(self.h.get("/rest/taskTargets?filter=" + q('taskId[eq]:"%s"' % d["task_id"])), (200,), op="twenty.find.target")
        return any(t.get("id") == d["target_id"] for t in (r.body.get("data") or {}).get("taskTargets") or [])

    def find(self, step: dict) -> "dict | None":
        """The task AND its company link: a task without its link is a half-applied write, reported as ``partial`` (``write`` completes it)."""
        r = self.h.get("/rest/tasks/" + step["derived"]["task_id"])
        if r.status == 404:
            return None
        self.h.check(r, (200,), op="twenty.find")
        res = self._result(step, created=False)
        if not self._target_exists(step):
            res["partial"] = True
        return res

    def write(self, step: dict, inputs: dict) -> dict:
        """Idempotent and resumable: each of the two records is created only if it is not already there (their ids derive from the op id)."""
        p, d = step["payload"], step["derived"]
        due = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=p.get("due_in_days", 2))).strftime("%Y-%m-%dT09:00:00.000Z")
        body = {"id": d["task_id"], "title": p["title"], "bodyV2": {"markdown": p["body"].replace("{quotation}", str(inputs.get("quotation", "")))}, "status": p["status"], "dueAt": due}
        existed = self.h.get("/rest/tasks/" + d["task_id"]).status == 200
        if not existed:
            self.h.check(self.h.request("POST", "/rest/tasks", body), (200, 201), op="twenty.write")
        if not self._target_exists(step):
            try:
                self.h.check(self.h.request("POST", "/rest/taskTargets", {"id": d["target_id"], "taskId": d["task_id"], "targetCompanyId": step["target"]["company_id"]}), (200, 201), op="twenty.write.target")
            except AppError as e:
                # the task exists but could not be linked to the company: take it back before reporting the step as failed
                back = self.h.request("DELETE", "/rest/tasks/%s?soft_delete=true" % d["task_id"])
                e.residual = {"soft_deleted_task": d["task_id"], "rolled_back": back.status in (200, 204)}
                raise
        return self._result(step, created=not existed, request={"method": "POST", "path": "/rest/tasks + /rest/taskTargets"})

    def undo(self, step: dict, result: dict) -> dict:
        d = step["derived"]
        for path in ("/rest/taskTargets/%s?soft_delete=true" % d["target_id"], "/rest/tasks/%s?soft_delete=true" % d["task_id"]):
            r = self.h.request("DELETE", path)
            if r.status != 404:
                self.h.check(r, (200, 204), op="twenty.undo")
        return _ok({"undone": True, "request": {"method": "DELETE", "path": "soft_delete taskTarget then task"}})


# ---------------------------------------------------------------------------------------------------------------- Chatwoot
class Chatwoot:
    app = "chatwoot"

    def __init__(self, http: AppHttp, base: str, account_id: int = 1):
        self.h, self.base, self.account = http, base, account_id

    def _acct(self, path: str) -> str:
        return "/api/v1/accounts/%d%s" % (self.account, path)

    def _result(self, step: dict, mid: int, created: bool, **extra) -> dict:
        cid = step["target"]["conversation_id"]
        return _ok({"native_id": mid, "native_ids": {"message": mid, "conversation": cid}, "conversation_id": cid, "created": created, "provider_ref": str(mid),
                    "ui_url": ui_url("chatwoot", self.base, account=self.account, conversation=cid), "ui_url_note": UI_NOTE_CHATWOOT}, **extra)

    def all_messages(self, cid: int) -> list:
        """Every message of the conversation, newest page first, following `before=` until an empty page. FAILS CLOSED (AppError kind
        `incomplete`) when it cannot be completed: a page that is not a list, a page that does not advance, a failed request, or the page cap."""
        base = self._acct("/conversations/%d/messages" % cid)
        out, seen, before = [], set(), None
        for _ in range(MESSAGE_PAGE_CAP):
            try:
                r = self.h.check(self.h.get(base + ("" if before is None else "?before=%d" % before)), (200,), op="chatwoot.find")
            except AppError as e:
                raise AppError("chatwoot", "incomplete", e.status, f"message pagination failed after {len(out)} message(s): {e.kind} {e.status}", op="chatwoot.find") from None
            pl = r.body.get("payload") if isinstance(r.body, dict) else None
            if not isinstance(pl, list):
                raise AppError("chatwoot", "incomplete", 200, "a message page without a payload list: partial read", op="chatwoot.find")
            if not pl:
                return out
            ids = [m.get("id") for m in pl]
            if any(not isinstance(i, int) for i in ids) or not [i for i in ids if i not in seen]:
                raise AppError("chatwoot", "incomplete", 200, "message pagination did not advance", op="chatwoot.find")
            seen.update(ids)
            out.extend(pl)
            before = min(ids)
        raise AppError("chatwoot", "incomplete", 200, f"message pagination hit the {MESSAGE_PAGE_CAP}-page cap: the marker lookup cannot be completed", op="chatwoot.find")

    def _live_notes(self, step: dict) -> list:
        cid, marker = step["target"]["conversation_id"], step["derived"]["marker"]
        return [m for m in self.all_messages(cid) if m.get("private") and marker in (m.get("content") or "") and not (m.get("content_attributes") or {}).get("deleted")]

    def find(self, step: dict) -> "dict | None":
        live = self._live_notes(step)
        if len(live) > 1:
            raise AppError("chatwoot", "conflict", 200, f"{len(live)} live private notes carry the marker; refusing to pick one", op="chatwoot.find")
        return self._result(step, live[0]["id"], created=False) if live else None

    def write(self, step: dict, inputs: dict) -> dict:
        body = {"content": "%s %s" % (step["derived"]["marker"], step["payload"]["body"].replace("{quotation}", str(inputs.get("quotation", "")))), "message_type": "outgoing", "private": True}
        r = self.h.check(self.h.request("POST", self._acct("/conversations/%d/messages" % step["target"]["conversation_id"]), body), (200, 201), op="chatwoot.write")
        return self._result(step, r.body["id"], created=True, request={"method": "POST", "path": self._acct("/conversations/%d/messages" % step["target"]["conversation_id"])})

    def undo(self, step: dict, result: dict) -> dict:
        r = self.h.request("DELETE", self._acct("/conversations/%d/messages/%d" % (step["target"]["conversation_id"], int(result["native_id"]))))
        if r.status == 404:
            return _ok({"undone": True, "note": "already absent"})
        self.h.check(r, (200, 204), op="chatwoot.undo")
        return _ok({"undone": True, "request": {"method": "DELETE", "path": "message"}})


def adapters(clients: "dict[str, AppHttp]", bases: dict, account_id: int = 1) -> dict:
    return {"erpnext": ErpNext(clients["erpnext"], bases["erpnext"]), "lago": Lago(clients["lago"], bases["lago"]),
            "twenty": Twenty(clients["twenty"], bases["twenty"]), "chatwoot": Chatwoot(clients["chatwoot"], bases["chatwoot"], account_id)}

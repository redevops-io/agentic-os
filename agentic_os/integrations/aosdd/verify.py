"""Independent read-back: the only thing that can mark a step VERIFIED.

Nothing here touches the write path: its own ``AppHttp`` instances are built from the READER credentials (Twenty read-only key,
ERPNext read-only user; Chatwoot and Lago OSS have no read-only role, so their single credential is used through a client that
refuses every method but GET), it never imports the adapters' write functions, and it never consults the runtime's recorded
result for anything but the app-native id it must look up. A step is VERIFIED only if every check against the expectation fixed
at plan time passes. Transport failure is UNREADABLE (never VERIFIED, never silently NOT_VERIFIED)."""
from __future__ import annotations

import json
from typing import Any

from .config import AosddConfig
from .context import q
from .http import AppError, AppHttp, Transport, auth_for

VERIFIED, NOT_VERIFIED, UNREADABLE = "VERIFIED", "NOT_VERIFIED", "UNREADABLE"
READER_NOTE = {"twenty": "read-only API key", "erpnext": "read-only user (AOS Read Only)",
               "chatwoot": "integration agent token, GET only (Chatwoot has no read-only role)",
               "lago": "organization API key, GET only (Lago OSS has no scoped keys)"}


class Unreadable(RuntimeError):
    pass


def reader_clients(cfg: AosddConfig, transport: Transport) -> "dict[str, AppHttp]":
    return {a: AppHttp(a, cfg.base_urls[a], auth_for(a, cfg.reader[a]), transport, cfg.redactor, timeout=cfg.http_timeout_s, read_only=True, role="reader") for a in cfg.reader}


class ReadBack:
    def __init__(self, cfg: AosddConfig, transport: Transport):
        self.c = reader_clients(cfg, transport)
        self.account = cfg.account_id

    # ---- helpers ----------------------------------------------------------------------------------------------------
    @staticmethod
    def _chk(out: list, name: str, expected: Any, observed: Any, ok: bool) -> None:
        out.append({"check": name, "expected": expected, "observed": observed, "ok": bool(ok)})

    def _get(self, app: str, path: str, ok=(200,)):
        r = self.c[app].get(path)
        if r.status == 0 or r.status >= 500:
            raise Unreadable(f"{app}: {r.status} on read-back")
        if r.status not in ok:
            return r
        return r

    # ---- present ---------------------------------------------------------------------------------------------------------
    def verify(self, step: dict, result: dict, ctx: "dict | None" = None) -> dict:
        """-> {status, checks, method, reader, requests}. ``ctx`` carries values created by earlier VERIFIED steps (the quotation name)."""
        out: list = []
        try:
            getattr(self, "_v_" + step["app"])(step, result, out, ctx or {})
            status = VERIFIED if out and all(c["ok"] for c in out) else NOT_VERIFIED
        except Unreadable as e:
            status, out = UNREADABLE, out + [{"check": "readable", "expected": True, "observed": str(e), "ok": False}]
        except AppError as e:
            status, out = UNREADABLE, out + [{"check": "readable", "expected": True, "observed": f"{e.kind} {e.status}", "ok": False}]
        return {"status": status, "checks": out, "method": "independent_readback", "reader": READER_NOTE[step["app"]], "step": step["step"]}

    def _v_erpnext(self, step, res, out, ctx):
        ex, name = step["expect"], res["native_id"]
        r = self._get("erpnext", "/api/resource/Quotation?filters=%s&fields=%s" % (q(json.dumps([["title", "=", ex["title"]]])), q('["name","docstatus"]')))
        rows = (r.body or {}).get("data") if r.status == 200 else None
        self._chk(out, "quotations carrying the marker title (count)", 1, None if rows is None else len(rows), rows is not None and len(rows) == 1)
        self._chk(out, "marker quotation name == receipt native id", name, rows[0]["name"] if rows else None, bool(rows) and rows[0]["name"] == name)
        g = self._get("erpnext", "/api/resource/Quotation/" + q(name))
        d = (g.body or {}).get("data", {}) if g.status == 200 else {}
        self._chk(out, "GET quotation by native id", 200, g.status, g.status == 200)
        for k in ("title", "docstatus", "party_name", "grand_total"):
            self._chk(out, "field " + k, ex[k], d.get(k), d.get(k) == ex[k])

    def _v_lago(self, step, res, out, ctx):
        ex, iid = step["expect"], step["target"]["invoice_lago_id"]
        r = self._get("lago", "/api/v1/invoices/" + iid)
        inv = (r.body or {}).get("invoice", {}) if r.status == 200 else {}
        self._chk(out, "GET invoice by native id", 200, r.status, r.status == 200)
        mine = [m for m in inv.get("metadata") or [] if m["key"] == ex["metadata_key"]]
        self._chk(out, "exactly one entry with our key", 1, len(mine), len(mine) == 1)
        want = "%s %s" % (ex["value_prefix"], ctx.get("quotation", ""))
        self._chk(out, "metadata value == '<op id> <verified quotation>'", want, mine[0]["value"] if mine else None, bool(mine) and mine[0]["value"] == want[:100])
        self._chk(out, "metadata lago_id == receipt", res["native_ids"]["metadata"], mine[0].get("lago_id") if mine else None, bool(mine) and mine[0].get("lago_id") == res["native_ids"]["metadata"])
        self._chk(out, "invoice unchanged: payment_status", ex["payment_status"], inv.get("payment_status"), inv.get("payment_status") == ex["payment_status"])
        self._chk(out, "invoice unchanged: payment_overdue", ex["payment_overdue"], inv.get("payment_overdue"), inv.get("payment_overdue") is ex["payment_overdue"])
        self._chk(out, "invoice unchanged: total_due_amount_cents", ex["total_due_amount_cents"], inv.get("total_due_amount_cents"), inv.get("total_due_amount_cents") == ex["total_due_amount_cents"])

    def _v_twenty(self, step, res, out, ctx):
        ex, tid = step["expect"], step["derived"]["task_id"]
        r = self._get("twenty", "/rest/tasks/" + tid)
        d = ((r.body or {}).get("data") or {}).get("task", {}) if r.status == 200 else {}
        self._chk(out, "GET task by native id", 200, r.status, r.status == 200)
        self._chk(out, "task id == receipt native id", res["native_id"], d.get("id"), d.get("id") == res["native_id"] == tid)
        self._chk(out, "title contains the overdue invoice number", ex["title_contains"], d.get("title"), ex["title_contains"] in (d.get("title") or ""))
        self._chk(out, "status", ex["status"], d.get("status"), d.get("status") == ex["status"])
        self._chk(out, "not soft-deleted", None, d.get("deletedAt"), d.get("deletedAt") is None)
        if ctx.get("quotation"):
            self._chk(out, "body names the verified quotation", ctx["quotation"], ((d.get("bodyV2") or {}).get("markdown") or "")[:300], ctx["quotation"] in ((d.get("bodyV2") or {}).get("markdown") or ""))
        t = self._get("twenty", "/rest/taskTargets?filter=" + q('taskId[eq]:"%s"' % tid))
        tl = (((t.body or {}).get("data") or {}).get("taskTargets") or []) if t.status == 200 else []
        self._chk(out, "taskTarget -> expected company", ex["target_company_id"], [x.get("targetCompanyId") for x in tl], any(x.get("targetCompanyId") == ex["target_company_id"] for x in tl))

    def _chatwoot_messages(self, cid: int) -> list:
        """ALL messages (the verifier's own pagination, same fail-closed rule: anything short of an empty last page is UNREADABLE)."""
        from .apps import MESSAGE_PAGE_CAP  # noqa: PLC0415 - a constant only; no adapter or write code is imported
        base, out, seen, before = "/api/v1/accounts/%d/conversations/%d/messages" % (self.account, cid), [], set(), None
        for _ in range(MESSAGE_PAGE_CAP):
            r = self._get("chatwoot", base + ("" if before is None else "?before=%d" % before))
            pl = (r.body or {}).get("payload") if r.status == 200 and isinstance(r.body, dict) else None
            if not isinstance(pl, list):
                raise Unreadable(f"chatwoot: message page unreadable (HTTP {r.status})")
            if not pl:
                return out
            ids = [m.get("id") for m in pl]
            if any(not isinstance(i, int) for i in ids) or not [i for i in ids if i not in seen]:
                raise Unreadable("chatwoot: message pagination did not advance")
            seen.update(ids)
            out.extend(pl)
            before = min(ids)
        raise Unreadable(f"chatwoot: message pagination hit the {MESSAGE_PAGE_CAP}-page cap")

    def _v_chatwoot(self, step, res, out, ctx):
        ex = step["expect"]
        cid = step["target"]["conversation_id"]
        pl = self._chatwoot_messages(cid)
        live = [m for m in pl if ex["marker"] in (m.get("content") or "") and not (m.get("content_attributes") or {}).get("deleted")]
        self._chk(out, "live notes carrying the marker (count)", 1, len(live), len(live) == 1)
        if live:
            self._chk(out, "message id == receipt native id", res["native_id"], live[0]["id"], live[0]["id"] == res["native_id"])
            self._chk(out, "private", True, live[0].get("private"), live[0].get("private") is True)
            self._chk(out, "conversation id", ex["conversation_id"], live[0].get("conversation_id"), live[0].get("conversation_id") == ex["conversation_id"])
            if ctx.get("quotation"):
                self._chk(out, "note names the verified quotation", ctx["quotation"], "...", ctx["quotation"] in (live[0].get("content") or ""))

    # ---- absent (after compensation) -------------------------------------------------------------------------------------------
    def verify_absent(self, step: dict, result: dict) -> dict:
        out: list = []
        residual: list = []
        try:
            getattr(self, "_a_" + step["app"])(step, result, out, residual)
            status = VERIFIED if out and all(c["ok"] for c in out) else NOT_VERIFIED
        except (Unreadable, AppError) as e:
            status, out = UNREADABLE, out + [{"check": "readable", "expected": True, "observed": str(e), "ok": False}]
        return {"status": status, "checks": out, "method": "independent_readback", "reader": READER_NOTE[step["app"]], "step": step["step"], "residual_observed": residual}

    def _a_erpnext(self, step, res, out, residual):
        r = self._get("erpnext", "/api/resource/Quotation?filters=%s&fields=%s" % (q(json.dumps([["title", "=", step["expect"]["title"]]])), q('["name"]')))
        rows = (r.body or {}).get("data") if r.status == 200 else None
        self._chk(out, "quotations carrying the marker title (count)", 0, None if rows is None else len(rows), rows is not None and len(rows) == 0)
        g = self._get("erpnext", "/api/resource/Quotation/" + q(res["native_id"]))
        self._chk(out, "GET quotation by native id", 404, g.status, g.status == 404)

    def _a_lago(self, step, res, out, residual):
        r = self._get("lago", "/api/v1/invoices/" + step["target"]["invoice_lago_id"])
        inv = (r.body or {}).get("invoice", {}) if r.status == 200 else {}
        ours = [m for m in inv.get("metadata") or [] if m["key"] == step["expect"]["metadata_key"] and str(m["value"]).startswith(step["expect"]["value_prefix"] + " ")]
        self._chk(out, "our metadata entry is gone", 0, len(ours), r.status == 200 and not ours)
        self._chk(out, "invoice still unpaid and overdue", {"payment_status": "pending", "payment_overdue": True}, {"payment_status": inv.get("payment_status"), "payment_overdue": inv.get("payment_overdue")}, inv.get("payment_status") == "pending" and inv.get("payment_overdue") is True)
        residual.append({"what": "invoice metadata set was rewritten (cleared); other keys: %s" % [m["key"] for m in inv.get("metadata") or []]})

    def _a_twenty(self, step, res, out, residual):
        tid = step["derived"]["task_id"]
        r = self._get("twenty", "/rest/tasks/" + tid)
        self._chk(out, "GET task (soft-deleted or absent => 404)", 404, r.status, r.status == 404)
        t = self._get("twenty", "/rest/taskTargets?filter=" + q('taskId[eq]:"%s"' % tid))
        tl = (((t.body or {}).get("data") or {}).get("taskTargets") or []) if t.status == 200 else []
        self._chk(out, "no live taskTarget", 0, len(tl), t.status == 200 and not tl)
        residual.append({"what": "soft-deleted task row and taskTarget row remain in Twenty; their ids cannot be reused", "task_id": tid})

    def _a_chatwoot(self, step, res, out, residual):
        cid = step["target"]["conversation_id"]
        pl = self._chatwoot_messages(cid)
        marked = [m for m in pl if step["expect"]["marker"] in (m.get("content") or "")]
        live = [m for m in marked if not (m.get("content_attributes") or {}).get("deleted")]
        self._chk(out, "live notes carrying the marker (count)", 0, len(live), r.status == 200 and not live)
        tomb = [m["id"] for m in pl if (m.get("content_attributes") or {}).get("deleted") and m.get("id") == res.get("native_id")]
        residual.append({"what": "a 'This message was deleted' tombstone remains in the conversation", "message_id": res.get("native_id"), "tombstone_listed": bool(tomb)})

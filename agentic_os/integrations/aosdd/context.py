"""The nine context reads of the governed revenue-recovery mission (SCENARIO.md section 2.1) and the plan they lead to.

Reads are plain GETs with the integration credential. Each read yields the app-native ids and a record hash; ``read_context``
returns the signal (what the plan is built from) and the context pack (what the receipt records)."""
from __future__ import annotations

import datetime
import json
import urllib.parse

from . import opid
from .http import AppError, AppHttp

CAPABILITY = {"S1": "aosdd.erpnext.quotation_draft", "S2": "aosdd.lago.invoice_metadata",
              "S3": "aosdd.twenty.task_create", "S4": "aosdd.chatwoot.private_note"}
APP = {"S1": "erpnext", "S2": "lago", "S3": "twenty", "S4": "chatwoot"}
OUTCOME = {"S1": "quotation", "S2": "invoice_metadata", "S3": "crm_task", "S4": "support_note"}
LAGO_KEY = "aosdd_recovery"      # one key per invoice: Lago's metadata update is replace-whole-set, key <= 20 chars
OFFER_FACTOR = 0.95


class ContextError(RuntimeError):
    pass


def q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def _h(o) -> str:
    return opid.sha256_hex(o)


def read_context(clients: "dict[str, AppHttp]", ref: str, account_id: int = 1) -> tuple:
    """-> (signal, context_pack). Raises AppError on any failed read and ContextError when a record is missing."""
    ctx: list = []
    sig: dict = {"customer_ref": ref}

    def add(app, method, path, native, rec, why):
        ctx.append({"n": len(ctx) + 1, "app": app, "request": f"{method} {path}", "native_ids": native, "record_sha256": _h(rec), "why": why})

    lg = clients["lago"]
    r = lg.check(lg.get("/api/v1/customers/" + q(ref)), (200,), op="read1")
    cust = r.body["customer"]
    add("lago", "GET", "/api/v1/customers/" + ref, {"lago_id": cust["lago_id"], "external_id": ref},
        {k: cust.get(k) for k in ("lago_id", "external_id", "email", "name")}, "billing customer, resolved by external_id == customer ref")
    path = "/api/v1/invoices?external_customer_id=%s&payment_overdue=true&payment_status=pending" % q(ref)
    r = lg.check(lg.get(path), (200,), op="read2")
    if not r.body.get("invoices"):
        raise ContextError("no overdue invoice in Lago")
    inv = r.body["invoices"][0]
    add("lago", "GET", path, {"invoice_lago_id": inv["lago_id"], "number": inv["number"]},
        {k: inv.get(k) for k in ("lago_id", "number", "payment_due_date", "payment_overdue", "payment_status", "total_due_amount_cents", "currency")}, "overdue invoice (signal 1)")
    sig["lago_invoice"] = {k: inv.get(k) for k in ("lago_id", "number", "payment_due_date", "total_due_amount_cents", "currency", "web_url")}
    sig["lago_customer"] = {"lago_id": cust["lago_id"], "email": cust.get("email")}

    er = clients["erpnext"]
    r = er.check(er.get("/api/resource/Customer?filters=%s&fields=%s" % (q(json.dumps([["customer_ref", "=", ref]])), q('["name","customer_ref"]'))), (200,), op="read3")
    if not r.body.get("data"):
        raise ContextError("no ERPNext customer with this customer_ref")
    ec = r.body["data"][0]["name"]
    add("erpnext", "GET", "/api/resource/Customer?filters=customer_ref", {"name": ec}, r.body["data"][0], "ERP customer, resolved by customer_ref")
    p2 = "/api/resource/Sales Invoice?filters=%s&fields=%s" % (q(json.dumps([["customer", "=", ec], ["status", "=", "Overdue"], ["docstatus", "=", 1]])),
                                                              q('["name","status","outstanding_amount","due_date","grand_total","currency"]'))
    r = er.check(er.get(p2), (200,), op="read4")
    if not r.body.get("data"):
        raise ContextError("no overdue Sales Invoice in ERPNext")
    si = r.body["data"][0]
    add("erpnext", "GET", p2, {"sales_invoice": si["name"]}, si, "overdue Sales Invoice (signal 2)")
    sig["erpnext_invoice"], sig["erpnext_customer"] = si, ec
    r = er.check(er.get("/api/resource/Quotation?filters=%s&fields=%s" % (q(json.dumps([["party_name", "=", ec], ["docstatus", "=", 1]])), q('["name","status","grand_total","valid_till"]'))), (200,), op="read5")
    qrows = r.body.get("data") or []
    if qrows:
        add("erpnext", "GET", "/api/resource/Quotation?filters=party_name", {"quotation": qrows[0]["name"]}, qrows[0], "current renewal quotation")
        sig["erpnext_quotation"] = qrows[0]

    cw = clients["chatwoot"]
    acct = "/api/v1/accounts/%d" % account_id
    r = cw.check(cw.get(acct + "/contacts/search?q=" + q(ref)), (200,), op="read6")
    cts = [x for x in r.body.get("payload", []) if x.get("identifier") == ref]
    if not cts:
        raise ContextError("no Chatwoot contact with this identifier")
    ct = cts[0]
    add("chatwoot", "GET", acct + "/contacts/search?q=" + ref, {"contact_id": ct["id"]}, {k: ct.get(k) for k in ("id", "email", "identifier", "name")}, "support contact, resolved by identifier == customer ref")
    r = cw.check(cw.get(acct + "/contacts/%d/conversations" % ct["id"]), (200,), op="read7")
    convs = [x for x in r.body.get("payload", []) if x.get("status") == "open"]
    if not convs:
        raise ContextError("no open Chatwoot conversation")
    conv = convs[0]
    add("chatwoot", "GET", acct + "/contacts/%d/conversations" % ct["id"], {"conversation_id": conv["id"]}, {k: conv.get(k) for k in ("id", "status", "inbox_id")}, "open conversation (signal 3)")
    sig["chatwoot"] = {"contact_id": ct["id"], "conversation_id": conv["id"], "inbox_id": conv.get("inbox_id"), "account_id": account_id}

    tw = clients["twenty"]
    r = tw.check(tw.get("/rest/companies?filter=" + q('customerRef[eq]:"%s"' % ref)), (200,), op="read8")
    cos = r.body["data"]["companies"]
    if not cos:
        raise ContextError("no Twenty company with this customerRef")
    co = cos[0]
    add("twenty", "GET", "/rest/companies?filter=customerRef", {"company_id": co["id"]}, {k: co.get(k) for k in ("id", "name", "customerRef")}, "CRM company, resolved by customerRef")
    r = tw.check(tw.get("/rest/opportunities?filter=" + q('companyId[eq]:"%s"' % co["id"])), (200,), op="read9")
    opps = r.body["data"]["opportunities"]
    if not opps:
        raise ContextError("no Twenty opportunity for the company")
    opp = opps[0]
    add("twenty", "GET", "/rest/opportunities?filter=companyId", {"opportunity_id": opp["id"]}, {k: opp.get(k) for k in ("id", "name", "stage", "closeDate", "amount")}, "renewal opportunity at risk (signal 4)")
    sig["twenty"] = {"company_id": co["id"], "opportunity_id": opp["id"], "stage": opp.get("stage"), "closeDate": opp.get("closeDate"),
                     "amount": opp.get("amount")}
    return sig, ctx


# ---------------------------------------------------------------------------------------------------------- the plan
def _offer(sig: dict) -> float:
    micros = ((sig.get("twenty") or {}).get("amount") or {}).get("amountMicros")
    base = (micros / 1_000_000) if isinstance(micros, (int, float)) else 48000.0
    return round(base * OFFER_FACTOR, 2)


def build_plan(mission_id: str, sig: dict, ctx: list, *, erp_company: str, account_id: int = 1, attempt: int = 1,
               today: "datetime.date | None" = None) -> dict:
    """The deterministic plan: four steps, each with its exact target, derived application ids, and the expectation the
    independent read-back will check. ``plan_sha256`` over the canonical JSON is what a human approves."""
    today = today or datetime.date.today()
    inv, party = sig["lago_invoice"], sig["erpnext_customer"]
    offer = _offer(sig)
    tgt = {
        "S1": {"doctype": "Quotation", "party": party, "company": erp_company},
        "S2": {"invoice_lago_id": inv["lago_id"], "metadata_key": LAGO_KEY},
        "S3": {"company_id": sig["twenty"]["company_id"]},
        "S4": {"account_id": account_id, "conversation_id": sig["chatwoot"]["conversation_id"]},
    }
    ops = {s: opid.op_id(mission_id, s, tgt[s], attempt) for s in tgt}
    steps = [
        {"step": "S1", "app": "erpnext", "capability": CAPABILITY["S1"], "outcome": OUTCOME["S1"], "op_id": ops["S1"], "target": tgt["S1"],
         "derived": {"title": opid.erp_title(ops["S1"])},
         "payload": {"quotation_to": "Customer", "party_name": party, "company": erp_company, "currency": "USD", "order_type": "Sales",
                     "transaction_date": today.isoformat(), "valid_till": (today + datetime.timedelta(days=14)).isoformat(),
                     "terms": "Early-renewal offer (5%% off) prepared by %s. Overdue invoice %s (%s due %s), Lago %s." % (
                         ops["S1"], sig["erpnext_invoice"]["name"], sig["erpnext_invoice"]["outstanding_amount"], sig["erpnext_invoice"]["due_date"], inv["number"]),
                     "items": [{"item_code": "Platform Subscription - Annual", "qty": 1, "rate": offer}]},
         "expect": {"title": opid.erp_title(ops["S1"]), "docstatus": 0, "party_name": party, "grand_total": offer},
         "link": "ERPNext /desk/quotation/<name>"},
        {"step": "S2", "app": "lago", "capability": CAPABILITY["S2"], "outcome": OUTCOME["S2"], "op_id": ops["S2"], "target": tgt["S2"], "derived": {},
         "payload": {"metadata_key": LAGO_KEY, "value": "<op_id> <quotation name from S1>"},
         "expect": {"metadata_key": LAGO_KEY, "value_prefix": ops["S2"], "payment_status": "pending", "payment_overdue": True,
                    "total_due_amount_cents": inv["total_due_amount_cents"]},
         "link": "Lago invoice.web_url from the API"},
        {"step": "S3", "app": "twenty", "capability": CAPABILITY["S3"], "outcome": OUTCOME["S3"], "op_id": ops["S3"], "target": tgt["S3"],
         "derived": {"task_id": opid.twenty_uuid(ops["S3"], "task"), "target_id": opid.twenty_uuid(ops["S3"], "target")},
         "payload": {"title": "Recover overdue invoice - Harborview Freight Co (%s)" % inv["number"], "status": "TODO",
                     "body": "Mission %s. Overdue: Lago %s (%s %s), ERPNext %s. Open support conversation #%s. Renewal offer drafted: {quotation}." % (
                         mission_id, inv["number"], inv["total_due_amount_cents"] / 100, inv["currency"], sig["erpnext_invoice"]["name"], sig["chatwoot"]["conversation_id"]),
                     "due_in_days": 2},
         "expect": {"title_contains": inv["number"], "status": "TODO", "target_company_id": sig["twenty"]["company_id"]},
         "link": "Twenty /object/task/<uuid>"},
        {"step": "S4", "app": "chatwoot", "capability": CAPABILITY["S4"], "outcome": OUTCOME["S4"], "op_id": ops["S4"], "target": tgt["S4"],
         "derived": {"marker": opid.chatwoot_marker(ops["S4"])},
         "payload": {"body": "Internal note: invoice %s is overdue (%s). A renewal offer ({quotation}) was drafted in ERPNext and a follow-up task was created for "
                             "the account owner in Twenty. Do not quote prices to the customer until the task owner confirms." % (inv["number"], inv["payment_due_date"])},
         "expect": {"private": True, "marker": opid.chatwoot_marker(ops["S4"]), "conversation_id": sig["chatwoot"]["conversation_id"]},
         "link": "Chatwoot conversation (no per-message URL exists)"},
    ]
    plan = {"schema": "aosdd.plan/1", "mission_id": mission_id, "attempt": attempt, "customer_ref": sig["customer_ref"],
            "context_sha256": _h([c["record_sha256"] for c in ctx]), "steps": steps}
    plan["plan_sha256"] = plan_sha256(plan)
    return plan


def plan_sha256(plan: dict) -> str:
    return opid.sha256_hex({k: v for k, v in plan.items() if k != "plan_sha256"})


def step_of(plan: dict, step: str) -> dict:
    for s in plan["steps"]:
        if s["step"] == step:
            return s
    raise KeyError(step)


def step_binding(plan: dict) -> list:
    """The exact step set an approval covers: (step, capability, op_id, target digest) in order."""
    return [[s["step"], s["capability"], s["op_id"], opid.target_digest(s["target"])] for s in plan["steps"]]

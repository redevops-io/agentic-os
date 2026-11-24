"""Billing app <-> a real Lago (a throwaway instance; the seeded demo is not touched). LAGO_API_URL and LAGO_API_KEY come from the environment. Creates two customers with one finalised invoice
each; the caller marks them overdue in Lago before phase 2 (time cannot be advanced through the API). Phases: `setup` prints the invoice ids, `run` does the checks. Prints PASS/FAIL and a JSON summary."""
import importlib
import os
import json
import sys

import httpx

core = importlib.import_module("billing.core")
U, H = core.LAGO_API_URL, core._headers()
results, facts = [], {}


def check(name, ok, detail=""):
    results.append({"check": name, "ok": bool(ok), "detail": str(detail)[:400]})
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{str(detail)[:260]}]" if detail and not ok else ""), flush=True)


def api(method, path, **kw):
    with httpx.Client(timeout=30.0) as c:
        return c.request(method, U + path, headers=H, **kw)


def invoice(lid):
    return api("GET", f"/api/v1/invoices/{lid}").json().get("invoice") or {}


if sys.argv[1] == "setup":
    api("POST", "/api/v1/add_ons", json={"add_on": {"name": "Setup fee", "code": "setup_fee", "amount_cents": 12000, "amount_currency": "USD"}})
    out = {}
    for ext in ("acct-north", "acct-south"):
        r = api("POST", "/api/v1/customers", json={"customer": {"external_id": ext, "name": ext.replace("acct-", "").title() + " Ltd", "currency": "USD", "net_payment_term": 0}})
        assert r.status_code == 200, r.text[:300]
        r = api("POST", "/api/v1/invoices", json={"invoice": {"external_customer_id": ext, "currency": "USD", "fees": [{"add_on_code": "setup_fee", "units": 1, "unit_amount_cents": 12000}]}})
        assert r.status_code == 200, r.text[:300]
        inv = r.json()["invoice"]
        out[ext] = {"lago_id": inv["lago_id"], "number": inv["number"], "status": inv["status"], "payment_status": inv["payment_status"], "total": inv["total_amount_cents"]}
    print(json.dumps(out))
    sys.exit(0)

ids = json.loads(sys.argv[2])
north, south = ids["acct-north"], ids["acct-south"]
facts["lago_version"] = os.environ.get("CORE_VERSION", "not stated")   # the version of the instance under test, as the person running this states it

# 1. read
check("the app reports Lago as connected", core.lago_connected())
act = core.fetch_activity(force=True)
dump = json.dumps(act)
check("read: the app shows both real invoices (no sample data)", north["number"] in dump and south["number"] in dump, list(act)[:14])
check("read: Lago reports both invoices as overdue and unpaid", all(invoice(x["lago_id"]).get("payment_overdue") is True and invoice(x["lago_id"]).get("payment_status") == "pending" for x in (north, south)),
      [(invoice(x["lago_id"]).get("payment_overdue"), invoice(x["lago_id"]).get("payment_status")) for x in (north, south)])
facts["overdue_in_app"] = {"north": north["number"] in dump, "kpis": act.get("kpis")}

# 2. scope
out = core.chase_invoices("")
check("retry with no account: refused (scope_required), nothing attempted", out.get("status") == "error" and out.get("error") == "scope_required" and out.get("dunning_attempted") is False, out)

# 3. no approval
out = core.chase_invoices("acct-north")
check("retry for one account without approval: staged (pending_approval, executed False)", out.get("status") == "pending_approval" and out.get("executed") is False and out.get("dunning_attempted") is False, out)
before = {k: invoice(v["lago_id"]) for k, v in ids.items()}

# 4. with approval, scoped to one account
out = core.chase_invoices("acct-north", approval={"approved": True, "by": "integration-check"})
facts["retry_result"] = {k: out.get(k) for k in ("status", "error", "dunning_attempted", "dunning_verified", "settled_count", "failed_count", "results", "skipped", "summary")}
rows = out.get("results") or []
check("retry with approval: exactly the one account's overdue invoice was sent to Lago", [r.get("lago_id") for r in rows] == [north["lago_id"]], rows)
check("retry with approval: the other account's invoice was not touched", south["lago_id"] not in json.dumps(rows) and invoice(south["lago_id"]).get("payment_status") == before["acct-south"].get("payment_status"), rows)
after = invoice(north["lago_id"])
facts["lago_retry_http"] = rows[0].get("retry_http") if rows else None
facts["payment_status_after_retry"] = after.get("payment_status")
honest = (out.get("dunning_verified") is True) == (after.get("payment_status") == "succeeded")
check("retry with approval: the result says 'verified' only if Lago reads back the invoice as paid", honest, (out.get("status"), out.get("dunning_verified"), after.get("payment_status")))
check("retry with approval: an invoice that did not settle is reported as not settled, never as success",
      after.get("payment_status") == "succeeded" or (out.get("status") in ("error", "partial") and out.get("dunning_verified") is False and out.get("failed_count", 0) >= 1), out)
out2 = core.chase_invoices("acct-north", invoice_ids=[south["lago_id"]], approval={"approved": True})
check("retry naming another account's invoice: nothing is sent", (out2.get("results") or []) == [] and south["lago_id"] in (out2.get("skipped") or []), out2)

# 5. refund: staged, explicit amount, currency of the invoice
out = core.stage_refund({})
check("refund without an invoice: refused", out.get("status") == "error" and out.get("refund_staged") is False, out)
out = core.stage_refund({"invoice": north["number"], "amount_cents": 5000})
check("refund without approval: staged only", out.get("status") == "pending_approval" and "refund_executed" not in out, out)
notes_before = len(api("GET", "/api/v1/credit_notes").json().get("credit_notes") or [])
out = core.stage_refund({"invoice": north["number"], "_approval": {"approved": True}})
check("refund with approval but no amount: not executed (an amount is required)", out.get("refund_executed") is False and "amount" in str(out.get("error")), out)
out = core.stage_refund({"invoice": north["number"], "amount_cents": 5000, "currency": "EUR", "_approval": {"approved": True}})
check("refund in another currency than the invoice's: refused", out.get("refund_executed") is False and "currency" in str(out.get("error")), out)
out = core.stage_refund({"invoice": north["number"], "amount_cents": 99999999, "_approval": {"approved": True}})
check("refund above the refundable total: refused", out.get("refund_executed") is False and "exceeds" in str(out.get("error")), out)
check("after four refused refunds Lago holds no new credit note", len(api("GET", "/api/v1/credit_notes").json().get("credit_notes") or []) == notes_before)
# 6. an approved, well-formed refund. What happens next is Lago's: the open-source edition answers 403 feature_unavailable to a credit note (measured below), a licensed one issues it.
#    Either way the app must report what Lago did and nothing else. The invoice is first marked paid in Lago by hand, as an operator would record a bank transfer.
r = api("PUT", f"/api/v1/invoices/{south['lago_id']}", json={"invoice": {"payment_status": "succeeded"}})
facts["mark_paid_http"] = r.status_code
probe = api("POST", "/api/v1/credit_notes", json={"credit_note": {"invoice_id": south["lago_id"], "reason": "other", "credit_amount_cents": 0, "refund_amount_cents": 0, "items": []}})
facts["lago_credit_note_probe"] = {"http": probe.status_code, "code": (probe.json() or {}).get("code") if probe.headers.get("content-type", "").startswith("application/json") else None}
edition_refuses = probe.status_code == 403 and facts["lago_credit_note_probe"]["code"] == "feature_unavailable"
facts["lago_edition_issues_credit_notes"] = not edition_refuses
notes_before = len(api("GET", "/api/v1/credit_notes").json().get("credit_notes") or [])
out = core.stage_refund({"invoice": south["number"], "amount_cents": 5000, "currency": "USD", "_approval": {"approved": True}})
facts["refund_on_paid_invoice"] = {k: out.get(k) for k in ("status", "refund_executed", "error", "credit_note_id", "refunded_cents", "currency")}
notes = api("GET", "/api/v1/credit_notes").json().get("credit_notes") or []
if edition_refuses:
    check("approved refund on open-source Lago: Lago refuses the credit note (403 feature_unavailable) and the app reports it as NOT executed, with Lago's reason",
          out.get("status") == "error" and out.get("refund_executed") is False and "feature_unavailable" in str(out.get("error")) and "NOT executed" in str(out.get("summary")), out)
    check("approved refund on open-source Lago: no credit note exists afterwards", len(notes) == notes_before, len(notes))
else:
    new_notes = [n for n in notes if n.get("lago_id") == out.get("credit_note_id")]
    check("approved refund on a licensed Lago: executed, and Lago holds the credit note for exactly 5000 cents in USD",
          out.get("refund_executed") is True and len(new_notes) == 1 and new_notes[0].get("refund_amount_cents") == 5000 and new_notes[0].get("currency") == "USD", out)

ok = all(r["ok"] for r in results)
print(json.dumps({"target": "lago", "facts": facts, "passed": sum(r["ok"] for r in results), "total": len(results), "results": results}, indent=1, default=str))
sys.exit(0 if ok else 1)

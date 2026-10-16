"""agentic-billing core — the pure Lago REST client + agentic actions.

No web framework, no context-runtime: just httpx against a real Lago core. This is the
layer the FastAPI app renders from AND the Mission Runtime operator invokes, so the
capability handlers can be tested against a fake Lago without booting the whole console.

`chase_overdue(blurb=...)` takes an optional narration callback — the LLM blurb lives in
`app.py` (context-runtime), keeping this module dependency-light and deterministic.
"""
from __future__ import annotations

import os
import re
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

import httpx
from urllib.parse import quote

from agentic_os.mission.operator_sdk import pending_approval

# ── config (env; seed.py writes agents/billing/.env) ─────────────────────────
# Idempotent .env load so this module is self-sufficient when imported by the operator
# (i.e. without the FastAPI app having run its own loader first).
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

LAGO_API_URL = os.environ.get("LAGO_API_URL", "http://localhost:3000").rstrip("/")
LAGO_API_KEY = os.environ.get("LAGO_API_KEY", "")
LAGO_FRONT_URL = os.environ.get("LAGO_FRONT_URL", "http://localhost:80").rstrip("/")

TENANT = "Meridian Wealth Management"
SUBTITLE = "Checkout to reconciliation on a real Lago core — with a human in the loop when money moves."


# ── Lago REST client ─────────────────────────────────────────────────────────
def _headers() -> dict:
    return {"Authorization": f"Bearer {LAGO_API_KEY}", "Content-Type": "application/json"}


def lago_connected() -> bool:
    """True iff Lago's health endpoint returns 200."""
    try:
        r = httpx.get(f"{LAGO_API_URL}/health", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False


def _get_all(path: str, key: str, params: dict | None = None) -> list[dict]:
    """GET a paginated Lago collection (e.g. /api/v1/invoices) and return all rows."""
    out: list[dict] = []
    page = 1
    params = dict(params or {})
    with httpx.Client(timeout=10.0) as client:
        while True:
            params.update({"page": page, "per_page": 100})
            r = client.get(f"{LAGO_API_URL}{path}", headers=_headers(), params=params)
            r.raise_for_status()
            body = r.json()
            rows = body.get(key, [])
            out.extend(rows)
            meta = body.get("meta", {})
            if not rows or page >= int(meta.get("total_pages", page)):
                break
            page += 1
    return out


# ── live data + KPIs (cached briefly) ────────────────────────────────────────
_CACHE: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 15.0  # seconds — keep the dashboard snappy without hammering Lago


def _money(cents: int) -> str:
    return "${:,.0f}".format((cents or 0) / 100.0)


def _invoice_state(inv: dict) -> str:
    """Collapse Lago's status/payment_status/overdue into one display state."""
    status = inv.get("status")
    pay = inv.get("payment_status")
    if inv.get("payment_overdue"):
        return "OVERDUE"
    if status == "draft":
        return "DRAFT"
    if status == "finalized" and pay == "succeeded":
        return "PAID"
    if status == "finalized" and pay == "pending":
        return "SENT"
    if pay == "failed":
        return "FAILED"
    return (status or "").upper() or "UNKNOWN"


def _days_overdue(inv: dict) -> int | None:
    due = inv.get("payment_due_date")
    if not due:
        return None
    try:
        from datetime import date

        y, m, d = (int(x) for x in due[:10].split("-"))
        delta = (date.today() - date(y, m, d)).days
        return delta if delta > 0 else 0
    except Exception:
        return None


def fetch_activity(force: bool = False) -> dict:
    """Pull REAL Lago data and compute the billing KPIs the dashboard renders."""
    now = time.time()
    if not force and _CACHE["data"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["data"]

    connected = lago_connected()
    invoices: list[dict] = []
    customers: list[dict] = []
    error = None
    if connected and LAGO_API_KEY:
        try:
            invoices = _get_all("/api/v1/invoices", "invoices")
            customers = _get_all("/api/v1/customers", "customers")
        except Exception as e:  # network / auth hiccup — surface, don't crash the page
            error = str(e)

    # KPIs straight from the live invoices.
    collected = sum(
        i.get("total_amount_cents", 0)
        for i in invoices
        if i.get("status") == "finalized" and i.get("payment_status") == "succeeded"
    )
    outstanding = sum(
        i.get("total_amount_cents", 0)
        for i in invoices
        if i.get("status") == "finalized" and i.get("payment_status") != "succeeded"
    )
    finalized = [i for i in invoices if i.get("status") == "finalized"]
    paid_ct = sum(1 for i in finalized if i.get("payment_status") == "succeeded")
    success_rate = round(100 * paid_ct / len(finalized)) if finalized else 0
    overdue = [i for i in invoices if i.get("payment_overdue")]

    # Recent invoices for the table (newest issuing_date first).
    recent = sorted(
        invoices, key=lambda i: i.get("issuing_date") or "", reverse=True
    )[:8]
    recent_rows = [
        {
            "number": i.get("number", "—"),
            "customer": (i.get("customer") or {}).get("name", "—"),
            "amount_cents": i.get("total_amount_cents", 0),
            "amount": _money(i.get("total_amount_cents", 0)),
            "state": _invoice_state(i),
            "days_overdue": _days_overdue(i) if i.get("payment_overdue") else None,
            "lago_id": i.get("lago_id"),
        }
        for i in recent
    ]

    overdue_rows = [
        {
            "number": i.get("number"),
            "customer": (i.get("customer") or {}).get("name", "—"),
            "amount": _money(i.get("total_amount_cents", 0)),
            "amount_cents": i.get("total_amount_cents", 0),
            "days_overdue": _days_overdue(i),
            "lago_id": i.get("lago_id"),
        }
        for i in overdue
    ]

    data = {
        "tenant": TENANT,
        "core": "lago",
        "connected": connected,
        "error": error,
        "front_url": LAGO_FRONT_URL,
        "kpis": [
            {"label": "Collected MTD", "value": _money(collected), "note": "finalized + paid"},
            {"label": "Outstanding", "value": _money(outstanding),
             "note": f"{len(finalized) - paid_ct} open invoice(s)"},
            {"label": "Payment success", "value": f"{success_rate}%", "note": "of finalized"},
            {"label": "Active customers", "value": str(len(customers)), "note": "in Lago"},
        ],
        "recent": recent_rows,
        "overdue": overdue_rows,
        "counts": {"invoices": len(invoices), "customers": len(customers), "overdue": len(overdue)},
    }
    _CACHE.update(ts=now, data=data)
    return data


# ── agentic actions (deterministic Lago API work) ────────────────────────────
def chase_overdue(blurb: Callable[[str], str | None] | None = None, *, approval=None) -> dict:
    """For each overdue invoice, trigger Lago's payment retry (the dunning action).

    APPROVAL-GATED IN CORE: without ``approval`` (the runtime-delivered ``_approval`` marker, or the
    route's server-side operator marker) this only STAGES a preview -- nothing is sent to Lago -- so
    every caller (operator, HTTP route, console tool) is covered by the one gate.

    POST /api/v1/invoices/{lago_id}/retry_payment is the real, idempotent Lago call.
    `blurb` is an optional narration callback (the LLM one-liner lives in app.py); the
    action itself is fully deterministic and works with blurb=None.
    """
    data = fetch_activity(force=True)
    overdue = data.get("overdue", [])
    if not approval:
        return pending_approval(
            "chase_overdue", dunning_attempted=False, overdue_count=len(overdue),
            requires="human approval",
            summary=f"Dunning on {len(overdue)} overdue invoice(s) is staged and awaiting human approval; "
                    "nothing was sent to Lago.")
    actions = []
    with httpx.Client(timeout=10.0) as client:
        for inv in overdue:
            lago_id = inv.get("lago_id")
            result = {"invoice": inv["number"], "customer": inv["customer"],
                      "amount": inv["amount"], "days_overdue": inv["days_overdue"]}
            try:
                resp = client.post(
                    f"{LAGO_API_URL}/api/v1/invoices/{lago_id}/retry_payment",
                    headers=_headers(),
                )
                result["lago_status"] = resp.status_code
                result["action"] = (
                    "retry_payment triggered (dunning reminder sent)"
                    if resp.status_code in (200, 202)
                    else f"retry_payment returned {resp.status_code}"
                )
            except Exception as e:
                result["action"] = f"error: {e}"
            actions.append(result)

    detail = "; ".join(f"{i['customer']} {i['amount']} ({i['days_overdue']}d)" for i in overdue)
    reasoning = blurb(
        "You are a billing collections agent for a wealth-management firm. In ONE sentence, "
        f"summarize sending dunning reminders / retrying payment on these {len(overdue)} overdue "
        f"invoices: {detail}. Be concrete and professional. Final answer only, no preamble."
    ) if (blurb and overdue) else None
    out = {
        "status": "done",
        "action": "chase_overdue",
        "overdue_count": len(overdue),
        "results": actions,
        "summary": f"Retried payment / sent dunning reminder on {len(actions)} overdue invoice(s) via Lago.",
    }
    if reasoning:
        out["reasoning"] = reasoning
    return out


# ── scoped dunning with a real read-back (closed loop: Act + Verify) ─────────
VERIFY_ATTEMPTS = int(os.environ.get("BILLING_VERIFY_ATTEMPTS", "3"))
VERIFY_INTERVAL_S = float(os.environ.get("BILLING_VERIFY_INTERVAL_S", "2.0"))
_sleep = time.sleep           # seam: tests inject a no-op


def invoice_account(inv: dict) -> str:
    """The account an invoice belongs to: the Lago customer's external_id, else its name, else its id."""
    c = inv.get("customer") or {}
    return str(c.get("external_id") or c.get("name") or c.get("lago_id") or "")


def invoice_in_account(inv: dict, account_id: str) -> bool:
    c = inv.get("customer") or {}
    return bool(account_id) and account_id in {str(c.get(k)) for k in ("external_id", "name", "lago_id")
                                               if c.get(k)}


def fetch_invoice(lago_id: str) -> dict | None:
    """GET /api/v1/invoices/{lago_id} — the read-back. None when Lago cannot be read (never guesses)."""
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f"{LAGO_API_URL}/api/v1/invoices/{lago_id}", headers=_headers())
        if r.status_code >= 400:
            return None
        return (r.json() or {}).get("invoice") or None
    except Exception:  # noqa: BLE001
        return None


def chase_invoices(account_id: str, invoice_ids=None, *, approval=None, all_accounts: bool = False) -> dict:
    """Dunning scoped to ONE account: retry payment only on that account's currently-overdue invoices (and,
    when ``invoice_ids`` is given, only those). Then READ EACH INVOICE BACK from Lago and report, per
    invoice, ``retried`` / ``payment_status_after`` / ``settled``. The result declares ``dunning_verified``
    (True only when every chased invoice settled and at least one was chased), ``settled_count`` and
    ``failed_count``; a partial result is ``status='partial'`` -- never success.

    Scope is REQUIRED: ``account_id`` (optionally narrowed by ``invoice_ids``). An unscoped call is refused
    with ``scope_required`` unless ``all_accounts=True`` (every account's overdue invoices -- still read back
    and still declaring the same outputs). APPROVAL-GATED IN CORE: without ``approval`` it stages only."""
    base = {"action": "dunning", "account_id": account_id, "dunning_attempted": False,
            "dunning_verified": False, "settled_count": 0, "failed_count": 0, "results": [], "skipped": []}
    if not account_id and not all_accounts:
        return {**base, "status": "error", "error": "scope_required",
                "reason": "billing.dunning needs account_id (and optional invoice_ids); "
                          "pass all_accounts=true with approval to chase every account"}
    if not approval:
        return {**pending_approval("billing.dunning", dunning_attempted=False, dunning_verified=False,
                                   account_id=account_id, all_accounts=bool(all_accounts and not account_id),
                                   requires="human approval"),
                "settled_count": 0, "failed_count": 0}
    if not (lago_connected() and LAGO_API_KEY):
        return {**base, "status": "error", "error": "lago not reachable"}
    try:
        invoices = _get_all("/api/v1/invoices", "invoices")
    except Exception as e:  # noqa: BLE001
        return {**base, "status": "error", "error": f"lago read failed: {e}"}
    want = None if invoice_ids is None else {str(i) for i in invoice_ids}
    chosen, skipped = [], []
    for inv in invoices:
        if account_id and not invoice_in_account(inv, account_id):
            continue
        lid = str(inv.get("lago_id"))
        if want is not None and lid not in want:
            continue
        (chosen if inv.get("payment_overdue") else skipped).append(inv)
    base["skipped"] = [str(i.get("lago_id")) for i in skipped]
    if want is not None:
        base["skipped"] += sorted(want - {str(i.get("lago_id")) for i in chosen + skipped})
    results = []
    with httpx.Client(timeout=10.0) as client:
        for inv in chosen:
            lid = inv.get("lago_id")
            row = {"invoice": inv.get("number"), "lago_id": lid, "retried": False,
                   "payment_status_after": None, "settled": False}
            try:
                resp = client.post(f"{LAGO_API_URL}/api/v1/invoices/{lid}/retry_payment", headers=_headers())
                row["retried"] = resp.status_code in (200, 202)
                row["retry_http"] = resp.status_code
            except Exception as e:  # noqa: BLE001
                row["error"] = str(e)
            results.append(row)
    for _ in range(max(1, VERIFY_ATTEMPTS)):          # read-back (bounded wait: Lago settles asynchronously)
        for row in results:
            if row["retried"] and not row["settled"]:
                after = fetch_invoice(row["lago_id"])
                row["payment_status_after"] = (after or {}).get("payment_status")
                row["settled"] = row["payment_status_after"] == "succeeded"
        if all(r["settled"] or not r["retried"] for r in results):
            break
        _sleep(VERIFY_INTERVAL_S)
    settled = sum(1 for r in results if r["settled"])
    failed = len(results) - settled
    verified = settled > 0 and failed == 0
    status = "done" if verified else ("partial" if settled > 0 else "error")
    out = {**base, "status": status, "results": results, "dunning_attempted": any(r["retried"] for r in results),
           "dunning_verified": verified, "settled_count": settled, "failed_count": failed,
           "summary": f"Dunning for {account_id or 'ALL accounts'}: {settled} of {len(results)} invoice(s) settled after retry."}
    if status != "done":
        out["error"] = ("no overdue invoice in scope" if not results else
                        f"{failed} of {len(results)} chased invoice(s) not settled on read-back")
    return out


_CUSTOMER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._@-]{0,127}$")
_PLAN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def valid_id(value) -> bool:
    """A Lago identifier safe to place in a URL path: no separators, no dot-dot, bounded length."""
    return isinstance(value, str) and bool(_ID_RE.match(value)) and ".." not in value


def parse_amount_cents(value) -> int:
    """Parse a requested refund amount to integer cents. Strings are DOLLARS ("$10", "10.00", "1,000.50");
    an int is CENTS (1000 == $10.00). Floats/bools/None/garbage and non-positive values raise ValueError."""
    if isinstance(value, bool) or value is None or isinstance(value, float):
        raise ValueError(f"invalid refund amount {value!r}")
    try:
        return _parse_amount_cents(value)
    except ArithmeticError:                    # Overflow / InvalidOperation from absurd exponents
        raise ValueError(f"invalid refund amount {value!r}") from None


def _parse_amount_cents(value) -> int:
    if isinstance(value, int):
        cents = value
    elif isinstance(value, str):
        txt = value.strip().replace(",", "")
        if txt.startswith("$"):
            txt = txt[1:].strip()
        try:
            d = Decimal(txt)
        except InvalidOperation:
            raise ValueError(f"invalid refund amount {value!r}") from None
        if not d.is_finite():
            raise ValueError(f"invalid refund amount {value!r}")
        if abs(d) > Decimal("1e12"):
            raise ValueError(f"invalid refund amount {value!r}")
        cents = int((d * 100).to_integral_value(rounding=ROUND_HALF_UP))
    else:
        raise ValueError(f"invalid refund amount {value!r}")
    if cents <= 0:
        raise ValueError(f"refund amount must be positive, got {value!r}")
    return cents


_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def resolve_refund_amount(body: dict):
    """Requested refund as integer cents, or None when absent. ``amount_cents`` (an explicit positive int) is the
    unambiguous form; the legacy ``amount`` keeps its meaning (string = dollars, int = cents). When both are
    given they must agree. Raises ValueError for anything invalid."""
    raw_c = body.get("amount_cents")
    cents = None
    if raw_c is not None:
        if isinstance(raw_c, bool) or not isinstance(raw_c, int) or raw_c <= 0:
            raise ValueError(f"amount_cents must be a positive integer, got {raw_c!r}")
        cents = raw_c
    if body.get("amount") is not None:
        legacy = parse_amount_cents(body.get("amount"))
        if cents is not None and legacy != cents:
            raise ValueError("amount and amount_cents disagree")
        cents = legacy
    return cents


def _execute_refund(invoice_number: str, amount=None, full_refund: bool = False, currency=None) -> dict:
    """Issue a real Lago refund credit note for the invoice — the money-back action. Refunds exactly the
    requested ``amount`` (capped at the invoice's refundable total; the actual cents are reported), or the
    whole refundable total only when ``full_refund`` is explicitly true. With neither, it errors. Errors are
    surfaced, never silently swallowed. Only reached on a governed approval."""
    want = None
    if not full_refund:
        if amount is None:
            return {"executed": False, "error": "refund amount required (or pass full_refund: true)"}
        try:
            want = parse_amount_cents(amount)
        except ValueError as e:
            return {"executed": False, "error": str(e)}
    if not (lago_connected() and LAGO_API_KEY):
        return {"executed": False, "error": "lago not reachable"}
    inv = next((i for i in _get_all("/api/v1/invoices", "invoices")
                if i.get("number") == invoice_number or i.get("lago_id") == invoice_number), None)
    if not inv:
        return {"executed": False, "error": f"invoice {invoice_number!r} not found"}
    lago_id = inv.get("lago_id")
    inv_cur = str(inv.get("currency") or "").upper()
    if currency is not None:
        if not isinstance(currency, str) or not _CURRENCY_RE.match(currency.strip().upper()):
            return {"executed": False, "error": f"invalid currency {currency!r}"}
        if inv_cur and currency.strip().upper() != inv_cur:
            return {"executed": False, "error": f"currency {currency!r} does not match the invoice currency {inv_cur}"}
    with httpx.Client(timeout=10.0) as client:
        detail = client.get(f"{LAGO_API_URL}/api/v1/invoices/{lago_id}", headers=_headers())
        fees = ((detail.json() or {}).get("invoice", {}) or {}).get("fees", []) if detail.status_code < 400 else []
        avail = [{"fee_id": f.get("lago_id"), "amount_cents": int(f.get("amount_cents") or 0)}
                 for f in fees if f.get("lago_id") and int(f.get("amount_cents") or 0) > 0]
        refundable = sum(it["amount_cents"] for it in avail)
        if not avail or refundable <= 0:
            return {"executed": False, "error": "invoice has no refundable fees"}
        if want is not None and want > refundable:
            return {"executed": False, "requested_cents": want, "refundable_cents": refundable,
                    "error": f"requested refund exceeds the refundable total ({refundable} cents)"}
        target = refundable if want is None else want
        items, left = [], target
        for it in avail:                      # allocate the refund across fees until the target is met
            if left <= 0:
                break
            take = min(it["amount_cents"], left)
            items.append({"fee_id": it["fee_id"], "amount_cents": take})
            left -= take
        payload = {"credit_note": {"invoice_id": lago_id, "reason": "other",
                                   "credit_amount_cents": 0, "refund_amount_cents": target, "items": items}}
        r = client.post(f"{LAGO_API_URL}/api/v1/credit_notes", headers=_headers(), json=payload)
        if r.status_code >= 400:
            return {"executed": False, "error": f"lago {r.status_code}: {r.text[:200]}"}
        cn = (r.json() or {}).get("credit_note", {})
        return {"executed": True, "credit_note_id": cn.get("lago_id"), "refunded_cents": target,
                "currency": inv_cur or None,
                "requested_cents": want}


def stage_refund(body: dict) -> dict:
    """Refunds move money OUT. On the ungoverned/pre-approval path they are only STAGED for a human. On a
    governed approval (the resumed Mission node carries ``_approval``) the refund is EXECUTED — a real Lago
    refund credit note — because a human has cleared THIS action (N3: approval executes)."""
    target = body.get("invoice")
    if not isinstance(target, str) or not target.strip():
        return {"status": "error", "action": "refund", "refund_staged": False, "refund_executed": False,
                "error": "invoice is required for a refund (no default invoice is chosen)",
                "summary": "Refund not staged: an invoice number is required."}
    target = target.strip()
    try:
        want_cents = resolve_refund_amount(body)
    except ValueError as e:
        return {"status": "error", "action": "refund", "refund_staged": False, "refund_executed": False,
                "error": str(e), "summary": f"Refund not staged: {e}."}
    amount = (f"{want_cents} cents" if want_cents is not None else
              "the full invoice" if body.get("full_refund") is True
              else "an UNSPECIFIED amount (an amount is required to execute)")
    if not body.get("_approval"):
        return {
            "status": "pending_approval",
            "action": "refund",
            "requires": "human approval",
            "refund_staged": True,
            "summary": f"Refund of {amount} on invoice {target} is staged and awaiting human approval. "
                       "Refunds are never auto-executed by the agent.",
        }
    ex = _execute_refund(target, want_cents, full_refund=body.get("full_refund") is True,
                         currency=body.get("currency"))
    ok = bool(ex.get("executed"))
    return {
        "status": "done" if ok else "error",
        "action": "refund",
        "refund_staged": True,
        "refund_executed": ok,
        **{k: v for k, v in ex.items() if k != "executed"},
        "summary": (f"Refund on invoice {target} EXECUTED after approval "
                    f"(Lago credit note {ex.get('credit_note_id')})." if ok
                    else f"Refund on invoice {target} approved but NOT executed: {ex.get('error')}."),
    }


# ── onboarding actions (create a paid subscription + its compensating undo) ───
DEFAULT_PLAN_CODE = "starter_monthly"


def _subscription_external_id(customer: str, plan: str) -> str:
    """Deterministic, idempotency-safe external_id derived from customer + plan.

    Lago treats external_id as the subscription's stable identifier, so deriving it
    (rather than minting a random one) makes create_subscription idempotent: re-running
    the onboarding mission upserts the same subscription instead of duplicating it.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", f"{customer}-{plan}".lower()).strip("-")
    return f"sub-{slug}"


def create_subscription(body: dict) -> dict:
    """Onboard a customer onto a paid plan via Lago's subscriptions API.

    POST /api/v1/subscriptions is the real, idempotent Lago call (keyed on external_id).
    Side-effecting; its compensating action is `cancel_subscription` (saga undo).
    """
    customer, plan = body.get("customer"), body.get("plan_code")
    if not (isinstance(customer, str) and _CUSTOMER_RE.match(customer)
            and isinstance(plan, str) and _PLAN_RE.match(plan)):
        return {"status": "error", "action": "create_subscription", "subscription": False, "created": False,
                "error": "a customer id and a plan code (plain strings) are required; no defaults are assumed"}
    if not body.get("_approval"):             # gate in CORE: every caller is covered
        return pending_approval("billing.create_subscription", customer=customer, plan_code=plan,
                                subscription=False)
    external_id = _subscription_external_id(customer, plan)
    lago_id = None
    status_code = 0
    with httpx.Client(timeout=10.0) as client:
        try:                                  # did this subscription already exist BEFORE this call?
            pre = client.get(f"{LAGO_API_URL}/api/v1/subscriptions/{quote(external_id, safe='')}",
                             headers=_headers())
            pre_existing = pre.status_code == 200 and bool((pre.json() or {}).get("subscription"))
        except Exception:  # noqa: BLE001 — cannot tell: treat as pre-existing so an undo never cancels it
            pre_existing = True
        if pre_existing:
            return {"status": "done", "action": "create_subscription", "subscription": True, "created": False,
                    "subscription_id": external_id, "plan": plan, "plan_code": plan, "customer": customer,
                    "note": "subscription already existed; nothing was created"}
        resp = client.post(
            f"{LAGO_API_URL}/api/v1/subscriptions",
            headers=_headers(),
            json={"subscription": {
                "external_customer_id": customer,
                "plan_code": plan,
                "external_id": external_id,
            }},
        )
        status_code = resp.status_code
        try:
            lago_id = (resp.json().get("subscription") or {}).get("lago_id")
        except Exception:  # noqa: BLE001 — non-JSON / error body: fall back to external_id
            lago_id = None
    if not (200 <= status_code < 300):          # never report "done" for a rejected Lago call
        return {
            "status": "error",
            "action": "create_subscription",
            "error": f"Lago returned HTTP {status_code}",
            "lago_status": status_code,
            "subscription": False,
            "plan": plan,
        }
    return {
        "status": "done",
        "action": "create_subscription",
        "subscription": True,
        "created": True,
        "subscription_id": external_id or lago_id,
        "customer": customer,
        "plan": plan,
        "plan_code": plan,
        "lago_status": status_code,
    }


def cancel_subscription(body: dict) -> dict:
    """Compensating action (saga undo) for create_subscription — terminate the subscription.

    DELETE /api/v1/subscriptions/{external_id} is Lago's terminate call.
    """
    fwd = body.get("result") if isinstance(body.get("result"), dict) else {}   # saga undo nests the forward result
    customer = body.get("customer") or fwd.get("customer")
    plan = body.get("plan_code") or fwd.get("plan_code") or fwd.get("plan")
    external_id = fwd.get("subscription_id") or body.get("subscription_id")
    if not external_id and customer and (body.get("plan_code") or fwd.get("plan_code") or fwd.get("plan")):
        external_id = _subscription_external_id(customer, plan)
    if not body.get("_approval"):             # gate in CORE: every caller is covered
        return pending_approval("billing.cancel_subscription", customer=customer, subscription_cancelled=False)
    if "result" in body and isinstance(body.get("result"), dict) and fwd.get("created") is not True:
        return {"status": "error", "action": "cancel_subscription", "cancelled": False,
                "subscription_cancelled": False,
                "error": "the forward call did not create this subscription; nothing was cancelled"}
    if not valid_id(external_id):             # never guess a target: refuse when the id is missing/invalid
        return {"status": "error", "action": "cancel_subscription", "cancelled": False,
                "subscription_cancelled": False,
                "error": "no valid subscription id supplied; nothing was cancelled"}
    with httpx.Client(timeout=10.0) as client:
        resp = client.delete(f"{LAGO_API_URL}/api/v1/subscriptions/{quote(external_id, safe='')}",
                             headers=_headers())
        status_code = resp.status_code
    if not (200 <= status_code < 300):          # never report "done" for a rejected Lago call
        return {
            "status": "error",
            "action": "cancel_subscription",
            "error": f"Lago returned HTTP {status_code}",
            "lago_status": status_code,
            "cancelled": False,
            "subscription_cancelled": False,
        }
    return {
        "status": "done",
        "action": "cancel_subscription",
        "cancelled": True,
        "subscription_cancelled": True,
        "subscription_id": external_id,
        "lago_status": status_code,
    }

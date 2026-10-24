"""Lago (billing) reconciliation probes: subscriptions, cancellations and refund credit notes.

What identifies each effect at Lago, from its API documentation:
  create_subscription  `external_id`, derived deterministically from customer and plan (so creation is an upsert): `GET /subscriptions/{external_id}`. By default that endpoint
                       returns only ACTIVE subscriptions; `pending` is queried explicitly. A record is positive evidence that the subscription exists.
  cancel_subscription  the same `external_id`, queried with `status=terminated` (several terminated or canceled subscriptions can share an external_id, so the newest termination is
                       used). `active` after the call is not proof the cancel never happened (it may not be visible yet): INCONCLUSIVE.
  refund               `POST /credit_notes` has NO idempotency key and returns nothing that ties a credit note to our request. A probe can only find a refund credit note on the invoice
                       (`GET /credit_notes?invoice_number=...&types[]=refund`) created since the call was sent, with the requested amount. That is consistent with our request, not identified
                       by it: exactly one such credit note is CONFIRMED_EXECUTED; more than one is INCONCLUSIVE (it may be a duplicate: a person must look); none is INCONCLUSIVE.
  dunning              `retry_payment` leaves no identifiable record: UNSUPPORTED (reconcile at the payment provider).
No Lago adapter is qualified to prove an effect ABSENT: none of these lookups is documented as strongly consistent, and Lago documents no idempotency for these calls. A negative is therefore
never returned; an unresolved case is retried read-only with backoff and then escalated to a person.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional
from urllib.parse import quote

from ..probes import ProbeContext, ProbeOutcome, ProbeResult, Qualification, from_http_status

SKEW_S = 120.0                                   # a created_at slightly before our dispatch time (clock skew) still counts as "since the call"
Http = Callable[[str, dict], "tuple[int, Any, dict]"]   # (path, params) -> (status, parsed JSON or None, response headers)

QUALIFICATION = Qualification("lago", eventually_consistent=True, visibility_window_s=None, authoritative_negative=False)
NO_LOOKUP = Qualification("lago-no-record", supports_lookup=False)


def subscription_external_id(customer: str, plan: str) -> str:
    """The same derivation `apps/billing/core.py` uses to make subscription creation an upsert (pinned against it by a test)."""
    slug = re.sub(r"[^a-z0-9]+", "-", f"{customer}-{plan}".lower()).strip("-")
    return f"sub-{slug}"


def _epoch(ts: Any) -> Optional[float]:
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
        except ValueError:
            return None
    return None


def _cents(amount: Any) -> Optional[int]:
    """The refund amount in cents, read the way `apps/billing` reads it (pinned against it by a test): a string is DOLLARS ("$10", "1,000.50"), an int is CENTS;
    anything else (a float, a bool, garbage, a non-positive value) is not used to match."""
    if isinstance(amount, bool) or amount is None or isinstance(amount, float):
        return None
    if isinstance(amount, int):
        return amount if amount > 0 else None
    if isinstance(amount, str):
        txt = amount.strip().replace(",", "")
        if txt.startswith("$"):
            txt = txt[1:].strip()
        try:
            d = Decimal(txt) * 100
        except (InvalidOperation, ValueError, ArithmeticError):
            return None
        if not d.is_finite() or d != d.to_integral_value() or d <= 0:
            return None
        return int(d)
    return None


def default_http(base_url: str, api_key: str, timeout: float = 10.0) -> Http:
    """A read-only GET over httpx (imported lazily so the module imports without it)."""
    def http(path: str, params: dict) -> tuple:
        import httpx   # noqa: PLC0415
        r = httpx.get(base_url.rstrip("/") + path, params=params, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
        try:
            body = r.json()
        except ValueError:
            body = None
        return r.status_code, body, dict(r.headers)
    return http


class LagoProbes:
    def __init__(self, http: Http) -> None:
        self.http = http

    # ── plumbing ──
    def _get(self, path: str, params: Optional[dict] = None):
        """(status, body, headers) or a TRANSIENT_ERROR ProbeResult when the lookup itself failed."""
        try:
            return self.http(path, params or {})
        except Exception as e:  # noqa: BLE001 - "I could not look" is never "it did not happen"
            return ProbeResult(ProbeOutcome.TRANSIENT_ERROR, detail=f"the lookup failed: {type(e).__name__}: {str(e)[:120]}")

    @staticmethod
    def _bad_status(status: int, headers: dict) -> Optional[ProbeResult]:
        if 200 <= status < 300:
            return None
        if status == 404:
            return ProbeResult(ProbeOutcome.INCONCLUSIVE, detail="not found: a missing record is not proof the action never happened")
        h = {k.lower(): v for k, v in (headers or {}).items()}
        return from_http_status(status, retry_after=h.get("retry-after"))

    @staticmethod
    def _evidence(reference: str, now: float) -> dict:
        return {"kind": "provider_record", "reference": reference[:480], "observed_at": str(now)}

    # ── subscriptions ──
    def create_subscription(self, ctx: ProbeContext) -> ProbeResult:
        ref = ctx.effect_ref or {}
        customer, plan = ref.get("customer"), ref.get("plan_code")
        if not (isinstance(customer, str) and isinstance(plan, str) and customer and plan):
            return ProbeResult(ProbeOutcome.UNSUPPORTED, detail="the reservation does not say which customer and plan: the effect cannot be looked up")
        ext = subscription_external_id(customer, plan)
        for status in ("active", "pending"):
            got = self._get(f"/api/v1/subscriptions/{quote(ext, safe='')}", {"status": status})
            if isinstance(got, ProbeResult):
                return got
            code, body, headers = got
            if code == 404:
                continue
            bad = self._bad_status(code, headers)
            if bad is not None:
                return bad
            sub = (body or {}).get("subscription") or {}
            if sub.get("external_id") == ext and sub.get("lago_id"):
                created = _epoch(sub.get("created_at"))
                pre = created is not None and created < ctx.dispatched_at - SKEW_S
                return ProbeResult(ProbeOutcome.CONFIRMED_EXECUTED,
                                   evidence=self._evidence(f"lago subscription {sub['lago_id']} external_id={ext} status={sub.get('status')}", ctx.now),
                                   provider_ref=str(sub["lago_id"]), observed_at=ctx.now,
                                   result={"status": "ok", "action": "create_subscription", "subscription": True, "external_id": ext,
                                           "lago_id": sub["lago_id"], "subscription_status": sub.get("status"), "reconciled": True},
                                   detail="the subscription exists" + (" (it was created before this call: an upsert found it)" if pre else ""))
        return ProbeResult(ProbeOutcome.INCONCLUSIVE, detail="no active or pending subscription with that external_id (not proof that none was created)")

    def cancel_subscription(self, ctx: ProbeContext) -> ProbeResult:
        ref = ctx.effect_ref or {}
        customer, plan = ref.get("customer"), ref.get("plan_code")
        if not (isinstance(customer, str) and isinstance(plan, str) and customer and plan):
            return ProbeResult(ProbeOutcome.UNSUPPORTED, detail="the reservation does not say which customer and plan: the effect cannot be looked up")
        ext = subscription_external_id(customer, plan)
        got = self._get(f"/api/v1/subscriptions/{quote(ext, safe='')}", {"status": "terminated"})
        if isinstance(got, ProbeResult):
            return got
        code, body, headers = got
        bad = self._bad_status(code, headers)
        if bad is not None:
            return bad
        sub = (body or {}).get("subscription") or {}
        if sub.get("external_id") == ext and sub.get("status") == "terminated":
            return ProbeResult(ProbeOutcome.CONFIRMED_EXECUTED,
                               evidence=self._evidence(f"lago subscription {sub.get('lago_id')} external_id={ext} status=terminated "
                                                       f"terminated_at={sub.get('terminated_at')}", ctx.now),
                               provider_ref=str(sub.get("lago_id") or ""), observed_at=ctx.now,
                               result={"status": "ok", "action": "cancel_subscription", "cancelled": True, "external_id": ext, "reconciled": True},
                               detail="the subscription is terminated")
        return ProbeResult(ProbeOutcome.INCONCLUSIVE, detail="no terminated subscription with that external_id yet (not proof that the cancel never happened)")

    # ── refunds ──
    def refund(self, ctx: ProbeContext) -> ProbeResult:
        ref = ctx.effect_ref or {}
        number = ref.get("invoice_number")
        if not (isinstance(number, str) and number):
            return ProbeResult(ProbeOutcome.UNSUPPORTED, detail="the reservation does not say which invoice: the refund cannot be looked up")
        want = None if ref.get("full_refund") in (True, "true", "True") else _cents(ref.get("amount"))
        got = self._get("/api/v1/credit_notes", {"invoice_number": number, "types[]": "refund", "per_page": 100})
        if isinstance(got, ProbeResult):
            return got
        code, body, headers = got
        bad = self._bad_status(code, headers)
        if bad is not None:
            return bad
        since = ctx.dispatched_at - SKEW_S
        notes = [n for n in ((body or {}).get("credit_notes") or []) if (_epoch(n.get("created_at")) or 0) >= since]
        if want is not None:
            notes = [n for n in notes if int(n.get("refund_amount_cents") or -1) == want]
        if len(notes) == 1:
            n = notes[0]
            return ProbeResult(ProbeOutcome.CONFIRMED_EXECUTED,
                               evidence=self._evidence(f"lago credit note {n.get('number')} ({n.get('lago_id')}) on invoice {number}: refund "
                                                       f"{n.get('refund_amount_cents')} cents, refund_status={n.get('refund_status')}", ctx.now),
                               provider_ref=str(n.get("lago_id") or ""), observed_at=ctx.now,
                               result={"executed": True, "credit_note_id": n.get("lago_id"), "refunded_cents": n.get("refund_amount_cents"),
                                       "reconciled": True},
                               detail="exactly one refund credit note on the invoice since the call was sent (consistent with the request, not identified by it)")
        if len(notes) > 1:
            return ProbeResult(ProbeOutcome.INCONCLUSIVE,
                               detail=f"{len(notes)} refund credit notes on the invoice since the call: one may be a duplicate; a person must look")
        return ProbeResult(ProbeOutcome.INCONCLUSIVE, detail="no matching refund credit note yet (not proof that no refund was issued)")

    def retry_payment(self, ctx: ProbeContext) -> ProbeResult:
        return ProbeResult(ProbeOutcome.UNSUPPORTED, detail="a payment retry leaves no identifiable record at Lago: reconcile at the payment provider")


def register_lago(registry, http: Http, *, prefix: str = "billing.") -> LagoProbes:
    """Register the Lago probes for the billing capabilities (with the input fields that identify each effect) on a `ProbeRegistry`."""
    p = LagoProbes(http)
    registry.register(f"{prefix}create_subscription", p.create_subscription, QUALIFICATION, ref_keys=("customer", "plan_code"))
    registry.register(f"{prefix}cancel_subscription", p.cancel_subscription, QUALIFICATION, ref_keys=("customer", "plan_code"))
    registry.register(f"{prefix}refund", p.refund, QUALIFICATION, ref_keys=("invoice_number", "amount", "full_refund", "currency"))
    registry.register(f"{prefix}dunning", p.retry_payment, NO_LOOKUP)
    return p

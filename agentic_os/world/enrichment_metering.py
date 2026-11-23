"""Metering + provenance for the contact-enrichment providers (Hunter / Apollo / Clearbit / PDL).

``world.enrichment`` makes real, credit-spending provider calls but, until this module, nothing recorded
that a call happened, what it cost, or where a returned address came from. ``MeteredProvider`` wraps any of
those providers without changing them:

* **Metering** — every billable call is appended to an ``EnrichmentLedger`` (SQLite, append-only: UPDATE and
  DELETE are rejected by triggers). A call whose price is not configured is booked as *unknown*, never as
  zero, so a total is always "known spend + N calls of unknown cost".
* **Budget** — with a budget set, a call is refused *before* the provider is contacted when the remaining
  budget cannot be shown to cover it. An unknown price cannot be shown to fit, so it is refused too.
* **Provenance** — each result carries a ``provenance`` record: provider, operation, retrieval time, a digest
  of the request and of the response, the ledger entry, and the cost basis. No credential and no raw
  response is stored; the ledger keeps the company domain in clear and everything else as a digest.

Prices are deployment configuration (``ENRICHMENT_PRICE_MICROS``, a JSON object of
``"<provider>.<operation>": <micro-USD>``). There are no built-in prices: a provider's list price depends on
the plan the deployment bought, and inventing one would turn the ledger into a guess.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .enrichment import EnrichmentError

# Provider operations that reach the provider's API (and may consume credits).
BILLABLE_OPS = ("find_email", "verify_email", "search_people", "unlock_person", "list_contacts")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS enrichment_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              TEXT    NOT NULL,
    tenant          TEXT    NOT NULL,
    provider        TEXT    NOT NULL,
    operation       TEXT    NOT NULL,
    subject         TEXT    NOT NULL,
    request_digest  TEXT    NOT NULL,
    response_digest TEXT    NOT NULL,
    outcome         TEXT    NOT NULL,
    cost_micros     INTEGER,
    cost_basis      TEXT    NOT NULL,
    detail          TEXT    NOT NULL DEFAULT ''
);
CREATE TRIGGER IF NOT EXISTS enrichment_calls_no_update BEFORE UPDATE ON enrichment_calls
BEGIN SELECT RAISE(ABORT, 'enrichment ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS enrichment_calls_no_delete BEFORE DELETE ON enrichment_calls
BEGIN SELECT RAISE(ABORT, 'enrichment ledger is append-only'); END;
"""

# cost_basis values
LIST_PRICE = "list_price"                       # the call completed; booked at the configured price
LIST_PRICE_ON_ERROR = "list_price_assumed_on_error"  # the call failed after it was sent; booked as if billed
UNKNOWN = "unknown"                             # no configured price; cost_micros is NULL
NOT_CALLED = "not_called"                       # refused before the provider was contacted; cost is 0


class BudgetExceeded(EnrichmentError):
    """The call was refused before reaching the provider. An ``EnrichmentError``, so the existing
    ``resolve_*`` helpers degrade to "no email" with this as the reason instead of raising."""


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def prices_from_env() -> Dict[str, int]:
    raw = os.environ.get("ENRICHMENT_PRICE_MICROS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise EnrichmentError(f"ENRICHMENT_PRICE_MICROS is not valid JSON: {e}") from None
    out: Dict[str, int] = {}
    for k, v in (data or {}).items():
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise EnrichmentError(f"ENRICHMENT_PRICE_MICROS[{k!r}] must be a non-negative integer (micro-USD)")
        out[str(k)] = v
    return out


class EnrichmentLedger:
    """Append-only record of enrichment provider calls. One SQLite file (or ``:memory:``)."""

    def __init__(self, path: str = ":memory:") -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)

    def record(self, *, tenant: str, provider: str, operation: str, subject: str, request_digest: str,
               response_digest: str, outcome: str, cost_micros: Optional[int], cost_basis: str,
               detail: str = "", at: Optional[str] = None) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO enrichment_calls (at, tenant, provider, operation, subject, request_digest,"
                " response_digest, outcome, cost_micros, cost_basis, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (at or _now(), tenant, provider, operation, subject, request_digest, response_digest, outcome,
                 cost_micros, cost_basis, detail[:200]))
            return int(cur.lastrowid)

    def entries(self, tenant: Optional[str] = None) -> List[Dict[str, Any]]:
        q, args = "SELECT * FROM enrichment_calls", ()
        if tenant is not None:
            q, args = q + " WHERE tenant = ?", (tenant,)
        with self._lock:
            return [dict(r) for r in self._db.execute(q + " ORDER BY id", args).fetchall()]

    def get(self, entry_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            r = self._db.execute("SELECT * FROM enrichment_calls WHERE id = ?", (entry_id,)).fetchone()
        return dict(r) if r else None

    def summary(self, tenant: str) -> Dict[str, Any]:
        """Known spend plus the number of calls whose cost is unknown — the two are never merged."""
        with self._lock:
            r = self._db.execute(
                "SELECT COALESCE(SUM(cost_micros), 0) AS known, COUNT(*) AS calls,"
                " SUM(CASE WHEN cost_basis = ? THEN 1 ELSE 0 END) AS unknown,"
                " SUM(CASE WHEN cost_basis = ? THEN 1 ELSE 0 END) AS refused"
                " FROM enrichment_calls WHERE tenant = ?", (UNKNOWN, NOT_CALLED, tenant)).fetchone()
        return {"tenant": tenant, "known_spend_micros": int(r["known"] or 0), "calls": int(r["calls"] or 0),
                "unknown_cost_calls": int(r["unknown"] or 0), "refused_calls": int(r["refused"] or 0)}


def _subject(kwargs: Dict[str, Any], args: tuple) -> str:
    """The clear-text subject kept in the ledger: a company domain when there is one, never a person."""
    if kwargs.get("domain"):
        return str(kwargs["domain"]).lower()
    email = kwargs.get("email") or (args[0] if args and isinstance(args[0], str) and "@" in args[0] else "")
    if email:
        return str(email).rsplit("@", 1)[-1].lower()
    return ""


def _outcome(op: str, result: Any) -> str:
    if isinstance(result, list):
        return "match" if result else "no_match"
    if isinstance(result, dict):
        if op == "verify_email":
            return "match" if result.get("status") not in (None, "unknown") else "no_match"
        return "match" if result.get("email") else "no_match"
    return "no_match"


class MeteredProvider:
    """A provider with every billable call metered, budget-checked and stamped with provenance.

    Capability probing is preserved: ``hasattr(metered, "search_people")`` is true exactly when the wrapped
    provider has it, so ``resolve_contact`` and ``source_apollo_list`` behave as before.
    """

    def __init__(self, provider: Any, ledger: EnrichmentLedger, *, tenant: str,
                 prices: Optional[Dict[str, int]] = None, budget_micros: Optional[int] = None,
                 clock: Callable[[], str] = _now) -> None:
        if not tenant:
            raise EnrichmentError("metered enrichment needs a tenant")
        self._inner = provider
        self._ledger = ledger
        self._tenant = tenant
        self._prices = prices_from_env() if prices is None else dict(prices)
        self._budget = budget_micros
        self._clock = clock
        self.name = provider.name

    def configured(self) -> bool:
        return self._inner.configured()

    @property
    def ledger(self) -> EnrichmentLedger:
        return self._ledger

    def price_of(self, op: str) -> Optional[int]:
        return self._prices.get(f"{self.name}.{op}")

    def __getattr__(self, attr: str) -> Any:
        if attr in BILLABLE_OPS:
            fn = getattr(self._inner, attr)        # AttributeError here keeps hasattr() honest
            return lambda *a, **kw: self._call(attr, fn, a, kw)
        return getattr(self._inner, attr)

    def _call(self, op: str, fn: Callable[..., Any], args: tuple, kwargs: Dict[str, Any]) -> Any:
        req = digest({"provider": self.name, "operation": op, "args": list(args), "kwargs": kwargs})
        subject, at, price = _subject(kwargs, args), self._clock(), self.price_of(op)
        base = dict(tenant=self._tenant, provider=self.name, operation=op, subject=subject,
                    request_digest=req, at=at)
        if self._budget is not None:
            spent = self._ledger.summary(self._tenant)["known_spend_micros"]
            reason = ""
            if price is None:
                reason = f"no configured price for {self.name}.{op}; cannot show it fits the budget"
            elif spent + price > self._budget:
                reason = f"budget {self._budget} micro-USD: {spent} spent, {self.name}.{op} costs {price}"
            if reason:
                self._ledger.record(**base, response_digest="", outcome="refused_budget", cost_micros=0,
                                    cost_basis=NOT_CALLED, detail=reason)
                raise BudgetExceeded(reason)
        try:
            result = fn(*args, **kwargs)
        except EnrichmentError as e:
            self._ledger.record(**base, response_digest="", outcome="error", cost_micros=price,
                                cost_basis=LIST_PRICE_ON_ERROR if price is not None else UNKNOWN, detail=str(e))
            raise
        resp = digest(result)
        basis = LIST_PRICE if price is not None else UNKNOWN
        entry = self._ledger.record(**base, response_digest=resp, outcome=_outcome(op, result),
                                    cost_micros=price, cost_basis=basis)
        prov = {"provider": self.name, "operation": op, "retrieved_at": at, "request_digest": req,
                "response_digest": resp, "ledger_entry": entry, "cost_micros": price, "cost_basis": basis,
                "tenant": self._tenant}
        if isinstance(result, dict):
            return {**result, "provenance": prov}
        if isinstance(result, list):
            return [{**r, "provenance": prov} if isinstance(r, dict) else r for r in result]
        return result


def metered_provider(*, tenant: str, ledger: EnrichmentLedger, name: Optional[str] = None,
                     prices: Optional[Dict[str, int]] = None,
                     budget_micros: Optional[int] = None) -> Optional[MeteredProvider]:
    """The configured provider (same selection as ``get_provider``), metered. ``None`` when none is
    configured — callers keep the lead at NO_EMAIL exactly as before."""
    from .enrichment import _select_provider  # noqa: PLC0415
    p = _select_provider(name)
    return MeteredProvider(p, ledger, tenant=tenant, prices=prices, budget_micros=budget_micros) if p else None


_LEDGERS: Dict[str, EnrichmentLedger] = {}
_LEDGERS_LOCK = threading.Lock()


def ledger_from_env() -> Optional[EnrichmentLedger]:
    """``ENRICHMENT_LEDGER_PATH`` names the ledger file (one shared handle per path). Unset means metering
    is not deployed."""
    path = os.environ.get("ENRICHMENT_LEDGER_PATH", "").strip()
    if not path:
        return None
    with _LEDGERS_LOCK:
        if path not in _LEDGERS:
            _LEDGERS[path] = EnrichmentLedger(path)
        return _LEDGERS[path]


def metered_from_env(provider: Any) -> Optional[MeteredProvider]:
    """Wrap ``provider`` when metering is deployed. ``ENRICHMENT_TENANT`` is required then, and
    ``ENRICHMENT_BUDGET_MICROS`` (optional) caps known spend. A half-configured deployment fails closed
    with an ``EnrichmentError`` rather than silently spending unmetered."""
    ledger = ledger_from_env()
    if ledger is None:
        return None
    tenant = os.environ.get("ENRICHMENT_TENANT", "").strip()
    if not tenant:
        raise EnrichmentError("ENRICHMENT_LEDGER_PATH is set but ENRICHMENT_TENANT is not")
    raw = os.environ.get("ENRICHMENT_BUDGET_MICROS", "").strip()
    budget: Optional[int] = None
    if raw:
        if not raw.isdigit():
            raise EnrichmentError("ENRICHMENT_BUDGET_MICROS must be a non-negative integer (micro-USD)")
        budget = int(raw)
    return MeteredProvider(provider, ledger, tenant=tenant, budget_micros=budget)

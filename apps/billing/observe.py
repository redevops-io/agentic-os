"""OBSERVE — build real ``BillingSignals`` from Lago and persist them as bi-temporal Observations.

Reads through the existing ``core`` client seams (``core._get_all`` / ``core.fetch_invoice``; tests swap
``core.httpx`` for a fake Lago transport), computes per-account overdue count / amount / age, payment
history and the last dunning attempt, and ingests ONE ``Observation`` per account per read through the
kernel's single ``ObservationIngestor`` path into a store this app owns (JSONL file, or in-memory).

Time semantics (agentic_os.observation): ``known_at`` is the moment this process read Lago (an exact
timestamp we took => ``KnownAtQuality.OBSERVED``); ``valid_at`` is the newest ``updated_at`` Lago reports
for the account's invoices (never later than the read), else the read time. ``ingested_at`` is the persist
time. Lago exposes no 'paid_at', so a payment is dated by when we SAW it.

Documented fallbacks (listed in ``BillingSignals.fallbacks`` and lowering decision confidence):
  - ``payment_due_date`` missing on an overdue invoice  -> its age is unknown (counted as 0 days)
  - ``payment_overdue`` flag absent                      -> derived from finalized + unpaid + past due date
  - ``payment_requests`` endpoint unavailable            -> ``last_dunning_at`` unknown (None)
  - overcharge detection (standing limit, not a per-read fallback): not derivable from Lago invoices -> False
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from agentic_os.observation import (EvidenceChange, KnownAtQuality, Observation, ObservationIngestor,
                                    StateDelta)

from . import core
from .producers import BillingSignals

APP = "billing"   # Observation.source: kernel derive_outcome copies it into OutcomeEvent.source_app, which is
                  # half of the learner's (source_app, action_kind) key -- it MUST equal the candidate's source_app.
SOURCE_STATE = "billing.lago.account_state"      # ingestor mapper keys (not the Observation.source)
SOURCE_OUTCOME = "billing.lago.dunning_outcome"


# ── persistence (the app owns the store; Postgres is optional via agentic_os.observation_store) ──────────
def _obs_to_dict(o: Observation) -> dict:
    return {"observation_id": o.observation_id, "source": o.source, "kind": o.kind, "subject": o.subject,
            "valid_at": o.valid_at, "known_at": o.known_at, "ingested_at": o.ingested_at,
            "known_at_quality": o.known_at_quality.value, "payload": dict(o.payload),
            "evidence_refs": list(o.evidence_refs)}


def _obs_from_dict(d: dict) -> Observation:
    return Observation(
        observation_id=d["observation_id"], source=d["source"], kind=d["kind"], subject=d["subject"],
        valid_at=d["valid_at"], known_at=d["known_at"], ingested_at=d.get("ingested_at", 0.0),
        known_at_quality=KnownAtQuality(d.get("known_at_quality", "unknown")),
        payload=d.get("payload") or {}, evidence_refs=tuple(d.get("evidence_refs") or ()))


@dataclass
class FileObservationStore:
    """Append-only JSONL Observation store (idempotent on observation_id; corrupt tail lines skipped)."""
    path: str

    def all(self) -> List[Observation]:
        if not os.path.exists(self.path):
            return []
        out: List[Observation] = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(_obs_from_dict(json.loads(line)))
                except (ValueError, KeyError):
                    continue
        return out

    def get(self, observation_id: str) -> Optional[Observation]:
        return next((o for o in self.all() if o.observation_id == observation_id), None)

    def append(self, obs: Observation) -> None:
        if self.get(obs.observation_id) is not None:
            return
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_obs_to_dict(obs), default=str) + "\n")

    def count(self) -> int:
        return len(self.all())


# ── helpers ──────────────────────────────────────────────────────────────────────────────────────────
def _ts(v) -> Optional[float]:
    """Parse a Lago ISO date/datetime to epoch seconds (UTC); None if absent/unparseable."""
    if not v:
        return None
    try:
        s = str(v).replace("Z", "+00:00")
        d = datetime.fromisoformat(s) if len(s) > 10 else datetime.fromisoformat(s + "T00:00:00+00:00")
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except ValueError:
        return None


def _is_overdue(inv: dict, now: float, fb: set) -> bool:
    if "payment_overdue" in inv:
        return bool(inv.get("payment_overdue"))
    fb.add("payment_overdue flag absent: derived from due date")
    due = _ts(inv.get("payment_due_date"))
    return (inv.get("status") == "finalized" and inv.get("payment_status") != "succeeded"
            and due is not None and due < now)


def _oid(prefix: str, *parts: Any) -> str:
    h = hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:12]
    return f"{prefix}-{h}"


def fetch_payment_requests() -> Optional[List[dict]]:
    """Lago's dunning trail (payment requests). None when the endpoint is unavailable (a documented fallback)."""
    try:
        return core._get_all("/api/v1/payment_requests", "payment_requests")
    except Exception:  # noqa: BLE001
        return None


def build_signals(invoices: List[dict], payment_requests: Optional[List[dict]], *, now: float
                  ) -> Dict[str, BillingSignals]:
    """Per-account BillingSignals from raw Lago invoices (pure; no I/O). Accounts with no invoices are absent."""
    by_acct: Dict[str, List[dict]] = {}
    for inv in invoices:
        acct = core.invoice_account(inv)
        if acct:
            by_acct.setdefault(acct, []).append(inv)
    last_req: Dict[str, float] = {}
    for pr in payment_requests or []:
        t = _ts(pr.get("created_at"))
        c = pr.get("customer") or {}
        for key in (c.get("external_id"), c.get("name"), c.get("lago_id")):
            if key and t is not None:
                last_req[str(key)] = max(last_req.get(str(key), 0.0), t)
    out: Dict[str, BillingSignals] = {}
    for acct, rows in by_acct.items():
        fb: set = set()
        overdue = [i for i in rows if _is_overdue(i, now, fb)]
        ages = []
        for i in overdue:
            due = _ts(i.get("payment_due_date"))
            if due is None:
                fb.add("payment_due_date missing: invoice age unknown (0 days)")
                ages.append(0.0)
            else:
                ages.append(max(0.0, (now - due) / 86400.0))
        finalized = [i for i in rows if i.get("status") == "finalized"]
        if payment_requests is None:
            fb.add("payment_requests unavailable: last_dunning_at unknown")
        cust_keys = set()
        for i in rows:
            c = i.get("customer") or {}
            cust_keys.update(str(c[k]) for k in ("external_id", "name", "lago_id") if c.get(k))
        last = max((last_req[k] for k in cust_keys if k in last_req), default=None)
        out[acct] = BillingSignals(
            account=acct, overdue_invoices=len(overdue), days_overdue=max(ages, default=0.0),
            overdue_amount_cents=sum(int(i.get("total_amount_cents") or 0) for i in overdue),
            overdue_invoice_ids=tuple(str(i.get("lago_id")) for i in overdue),
            paid_invoices=sum(1 for i in finalized if i.get("payment_status") == "succeeded"),
            failed_invoices=sum(1 for i in finalized if i.get("payment_status") == "failed"),
            last_dunning_at=last, currency=str((rows[0].get("currency")) or ""),
            observed_at=now, fallbacks=tuple(sorted(fb)))
    return out


# ── ingestion ────────────────────────────────────────────────────────────────────────────────────────
def _state_mapper(raw: dict):
    sig: BillingSignals = raw["signals"]
    read_at = raw["read_at"]
    upd = [t for t in (_ts(i.get("updated_at")) for i in raw["invoices"]) if t is not None and t <= read_at]
    valid_at = max(upd, default=read_at)
    payload = {"overdue_invoices": sig.overdue_invoices, "days_overdue": sig.days_overdue,
               "overdue_amount_cents": sig.overdue_amount_cents,
               "overdue_invoice_ids": list(sig.overdue_invoice_ids), "paid_invoices": sig.paid_invoices,
               "failed_invoices": sig.failed_invoices, "last_dunning_at": sig.last_dunning_at,
               "fallbacks": list(sig.fallbacks)}
    oid = _oid("obs-state", sig.account, read_at, payload)
    obs = Observation(
        observation_id=oid, source=APP, kind="billing.account_state", subject=sig.account,
        valid_at=valid_at, known_at=read_at, ingested_at=raw.get("ingested_at", read_at),
        payload=payload, evidence_refs=tuple(f"invoice:{i}" for i in sig.overdue_invoice_ids),
        known_at_quality=KnownAtQuality.OBSERVED)
    deltas = [StateDelta(entity=sig.account, field="overdue_invoices", old=None, new=sig.overdue_invoices,
                         valid_at=valid_at, known_at=read_at, source=SOURCE_STATE)]
    changes = ([EvidenceChange(entity=sig.account, summary=f"{sig.overdue_invoices} overdue invoice(s)",
                               valid_at=valid_at, known_at=read_at, refs=obs.evidence_refs,
                               source=SOURCE_STATE)] if sig.overdue_invoices else [])
    return obs, deltas, changes


def _outcome_mapper(raw: dict):
    read_at = raw["read_at"]
    payload = {k: raw[k] for k in ("outcome", "settled_fraction", "chased_amount_cents",
                                   "settled_amount_cents", "invoices", "intervention_id")}
    oid = _oid("obs-outcome", raw["account"], raw["intervention_id"], read_at)
    obs = Observation(
        observation_id=oid, source=APP, kind="billing.dunning_outcome", subject=raw["account"],
        valid_at=read_at, known_at=read_at, ingested_at=raw.get("ingested_at", read_at), payload=payload,
        evidence_refs=tuple(f"invoice:{i['lago_id']}" for i in raw["invoices"]),
        known_at_quality=KnownAtQuality.OBSERVED)
    return obs, [], []


def build_ingestor(store) -> ObservationIngestor:
    """The app's ingestor: its first sink persists every Observation to ``store`` (``.append(Observation)``)."""
    ing = ObservationIngestor()
    ing.register_mapper(SOURCE_STATE, _state_mapper).register_mapper(SOURCE_OUTCOME, _outcome_mapper)
    ing.add_sink(lambda obs, deltas, changes: store.append(obs))
    return ing


def observe_accounts(ingestor: ObservationIngestor, *, now: Optional[float] = None,
                     accounts: Optional[List[str]] = None) -> Dict[str, BillingSignals]:
    """Read Lago NOW and ingest one Observation per account. Returns real signals carrying their
    observation ids. Raises RuntimeError when Lago cannot be read (the loop must not decide on no data)."""
    t = time.time() if now is None else now
    if not (core.lago_connected() and core.LAGO_API_KEY):
        raise RuntimeError("lago not reachable: cannot observe billing state")
    invoices = core._get_all("/api/v1/invoices", "invoices")
    signals = build_signals(invoices, fetch_payment_requests(), now=t)
    out: Dict[str, BillingSignals] = {}
    for acct, sig in signals.items():
        if accounts is not None and acct not in accounts:
            continue
        rows = [i for i in invoices if core.invoice_in_account(i, acct)]
        obs, _, _ = ingestor.ingest(SOURCE_STATE, {"signals": sig, "invoices": rows, "read_at": t,
                                                   "ingested_at": time.time()})
        from dataclasses import replace
        out[acct] = replace(sig, observation_ids=(obs.observation_id,))
    return out


def observe_dunning_outcome(ingestor: ObservationIngestor, *, account: str, intervention_id: str,
                            invoice_ids: List[str], now: float) -> Optional[Observation]:
    """Re-read the invoices a dunning run chased and ingest ONE outcome Observation. None if any invoice
    cannot be read back (no outcome is better than a guessed one)."""
    rows, chased, settled = [], 0, 0
    for lid in invoice_ids:
        inv = core.fetch_invoice(lid)
        if inv is None:
            return None
        amt = int(inv.get("total_amount_cents") or 0)
        ok = inv.get("payment_status") == "succeeded"
        chased += amt
        settled += amt if ok else 0
        rows.append({"lago_id": lid, "payment_status": inv.get("payment_status"), "amount_cents": amt})
    frac = (settled / chased) if chased else 0.0
    obs, _, _ = ingestor.ingest(SOURCE_OUTCOME, {
        "account": account, "intervention_id": intervention_id, "read_at": now, "ingested_at": time.time(),
        "outcome": "payment_collected" if settled > 0 else "no_payment", "settled_fraction": frac,
        "chased_amount_cents": chased, "settled_amount_cents": settled, "invoices": rows})
    return obs


__all__ = ["FileObservationStore", "build_ingestor", "build_signals", "observe_accounts",
           "observe_dunning_outcome", "SOURCE_STATE", "SOURCE_OUTCOME", "fetch_payment_requests"]

"""The one ledger correction governed writes perform: a received payment that the bank's OWN statement shows for another
amount is cancelled and entered again, as ERPNext's amendment of it.

A difference between the books and the bank is NOT enough. It is proposed and performed only when all of this holds, at
the proposal and again at the moment of writing:

* the deployment turned it on (``AGENTIC_OS_PAYMENT_CORRECTIONS=1``) and holds the bank's statement exports where only
  the operator puts them (``AGENTIC_OS_BANK_STATEMENTS_DIR/<tenant>/*.csv``); an agent cannot supply evidence;
* ``economics.causes.explain_difference`` finds the difference corroborated: the statement shows another amount under the
  reference and no fee, currency, partial-settlement, allocation, reversal or timing cause is supported or unknown;
* no other statement line could be the missing part: none names the payment, none is for exactly the difference, no bank
  charge no larger than it is dated within ``SETTLEMENT_DAYS`` of the deposit, and the statement runs ``SETTLEMENT_DAYS`` past the payment and the deposit, so
  a second instalment has had time to arrive;
* the books are the simple case: one submitted received payment under the reference, one deposit, allocated in full to
  one sales invoice, one currency at rate 1, no deductions or taxes.

The approver sees that evidence, its digest is part of what they approve, and they must attest to having checked it.

Nothing is deleted. ERPNext keeps the cancelled voucher and its ledger entries; the replacement names it
(``amended_from``) and both carry a comment naming the approval. A journal (``corrections.sqlite``, never erased) records
the voucher as it was, the evidence, who approved, and each step. When the cancellation happens and the replacement does
not, the outcome is "unknown, check now": nothing is retried by itself. A new proposal for the same payment then offers
only to FINISH it, needs a new approval, and looks for an existing amendment before creating one.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from agentic_os.economics import bank, causes, reconcile
from agentic_os.economics.contracts import Extraction, money
from agentic_os.governed_writes.apps import NotApplied, OutcomeUnknown

ENV_ENABLED = "AGENTIC_OS_PAYMENT_CORRECTIONS"
ENV_STATEMENTS = "AGENTIC_OS_BANK_STATEMENTS_DIR"
#: how far past the payment and the deposit a statement must reach before "no more is coming" can be said
SETTLEMENT_DAYS = bank.LATE_DAYS
MAX_FILES, MAX_FILE_BYTES = 60, 5_000_000
#: journal states in which the books may be half way between the two vouchers
UNFINISHED = ("planned", "cancel_sent", "cancelled", "create_sent", "draft", "submitted")
#: what the amendment copies from the cancelled entry; every amount is set from the approved figures, never copied
COPIED = ("payment_type", "party_type", "party", "company", "posting_date", "paid_from", "paid_to", "paid_from_account_currency",
          "paid_to_account_currency", "mode_of_payment", "reference_no", "reference_date", "source_exchange_rate", "target_exchange_rate")
ATTEST = ("I checked the bank statement lines shown. No bank fee, currency conversion, further instalment, allocation or timing "
          "difference explains this, including anything the customer or the bank said about it. The payment as recorded is wrong.")
ATTEST_FINISH = ("I checked in ERPNext that the original payment is cancelled and that no one has recorded its money again by "
                 "other means.")
_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def enabled() -> bool:
    return os.environ.get(ENV_ENABLED, "").strip() == "1" and bool(os.environ.get(ENV_STATEMENTS, "").strip())


class Reader:
    """The strict reader ``economics`` expects, over the writer's own connection: unreadable raises, it never reads empty."""

    def __init__(self, writer: Any) -> None:
        self.w = writer

    def read_list(self, doctype: str, fields: List[str], filters: Any = None, limit: int = 0) -> List[dict]:
        return self.w.read_list(doctype, fields, filters, limit)


# ── the bank's own statement ─────────────────────────────────────────────────────
def load_statements(tenant: str) -> Dict[str, str]:
    """``{file name: text}`` of the statement exports the operator keeps for this tenant ("" = ``default``)."""
    root = os.environ.get(ENV_STATEMENTS, "").strip()
    name = tenant or "default"
    if not root or not _TENANT.match(name):
        raise NotApplied(f"no bank statement is configured for this tenant ({ENV_STATEMENTS})")
    folder = Path(root) / name
    files = sorted(p for p in folder.glob("*.csv") if p.is_file()) if folder.is_dir() else []
    if not files:
        raise NotApplied(f"no bank statement export is there for this tenant: a bank difference alone changes nothing ({ENV_STATEMENTS})")
    if len(files) > MAX_FILES or any(p.stat().st_size > MAX_FILE_BYTES for p in files):
        raise NotApplied("the bank statement folder holds more than can be read completely")
    try:
        return {p.name: p.read_text(encoding="utf-8-sig") for p in files}
    except (OSError, UnicodeError) as e:
        raise NotApplied(f"a bank statement export could not be read ({type(e).__name__})") from None


# ── may this payment be corrected at all ─────────────────────────────────────────
@dataclass
class Assessment:
    reasons: List[str]                       # why not ("" list = eligible)
    evidence: Dict[str, Any]
    document: Optional[Dict[str, Any]] = None

    @property
    def ok(self) -> bool:
        return not self.reasons


def refusal_for(doc: Mapping[str, Any], posted: Decimal) -> str:
    """Why this payment entry is not one this action corrects ("" = it is)."""
    refs = [r for r in (doc.get("references") or [])]
    rates = [doc.get(k) for k in ("source_exchange_rate", "target_exchange_rate")]
    currencies = {doc.get(k) for k in ("paid_from_account_currency", "paid_to_account_currency") if doc.get(k)}
    one = len(refs) == 1 and refs[0].get("reference_doctype") == "Sales Invoice"
    reasons = (("it is not a submitted entry", doc.get("docstatus") != 1),
               ("it is not a received payment", doc.get("payment_type") != "Receive"),
               ("it is not allocated to exactly one sales invoice", not one),
               ("it is not allocated in full", one and money(refs[0].get("allocated_amount")) != money(doc.get("paid_amount"))),
               ("it holds money no invoice was allocated", money(doc.get("unallocated_amount") or 0) != 0),
               ("it involves a currency conversion", len(currencies) > 1 or any(r not in (None, "") and float(r) != 1.0 for r in rates)),
               ("its amounts differ between the two accounts (another currency, or charges)",
                money(doc.get("paid_amount")) != money(doc.get("received_amount"))),
               ("it carries deductions or taxes", bool(doc.get("deductions")) or bool(doc.get("taxes"))),
               ("its amount is not the one in the proposal", money(doc.get("paid_amount")) != posted))
    return "; ".join(why for why, bad in reasons if bad)


def _days(a: str, b: str) -> int:
    return abs((_dt.date.fromisoformat(a) - _dt.date.fromisoformat(b)).days)


def other_lines_that_could_explain(lines: List[Any], deposit: Any, posting: Any, difference: Decimal) -> List[str]:
    """Statement lines, other than the deposit itself, that might be the missing part although nothing ties them by reference."""
    gap, ref = abs(difference), posting.reference
    since = (_dt.date.fromisoformat(posting.date) - _dt.timedelta(days=SETTLEMENT_DAYS)).isoformat()
    out = []
    for l in lines:
        if l.id == deposit.id:
            continue
        text = re.sub(r"\s+", "", f"{l.reference} {l.description}").upper()
        if ref and ref in text:
            out.append(f"line {l.id} also names this payment")
        elif l.date >= since and abs(l.amount) == gap:
            out.append(f"line {l.id} is for exactly the difference")
        elif l.amount < 0 and abs(l.amount) <= gap and _days(l.date, deposit.date) <= SETTLEMENT_DAYS and bank.FEE.search(l.description):
            out.append(f"line {l.id} is a bank charge no larger than the difference")
    return out[:10]


def assess(writer: Any, tenant: str, reference: str, payment: str, posted: Decimal, banked: Decimal) -> Assessment:
    """Reads the books and the statement now. Raises ``NotApplied`` when either cannot be read completely."""
    client = Reader(writer)
    found = reconcile.find(client)
    if found.gaps:
        raise NotApplied("the books could not be read completely: " + "; ".join(str(g) for g in found.gaps)[:300])
    mine = [d for d in found.events if d.reference == reference]
    if not mine:
        return Assessment(["the books and the application's bank lines do not disagree under this reference"], {})
    d = mine[0]
    if d.ambiguous or len(mine) > 1:
        return Assessment([f"which record is wrong cannot be told: {d.ambiguous or 'several discrepancies share the reference'}"], {})
    if (d.payment, d.posted, d.bank) != (payment, posted, banked):
        return Assessment(["the figures are not the ones in the proposal: read the discrepancy again"], {})
    if banked <= 0:
        return Assessment(["the bank shows no positive amount"], {})
    statements = load_statements(tenant)
    findings, ex = bank.reconcile_statement(client, statements)
    if ex.gaps:
        raise NotApplied("the bank statement could not be read completely: " + "; ".join(str(g) for g in ex.gaps)[:300])
    lines = [l for name, text in statements.items() for l in bank.parse_statement(text, name, Extraction())]
    doc = writer.document("Payment Entry", payment)
    posting = bank.posting_from_entry(doc)
    explained = causes.explain_difference(posting.reference, posted - banked, posting, findings)
    ours = [f for f in findings if f.reference == posting.reference and f.posting == payment]
    evidence: Dict[str, Any] = {
        "statement_sha256": {n: hashlib.sha256(t.encode("utf-8")).hexdigest() for n, t in sorted(statements.items())},
        "classification": explained.to_json()}
    reasons: List[str] = []
    why = refusal_for(doc, posted)
    if why:
        reasons.append(why)
    if [f.kind for f in ours] != ["amount_mismatch"] or len(ours[0].lines) != 1 or ours[0].banked != banked:
        said = ", ".join(sorted(f.kind for f in ours)) or "nothing under this reference"
        reasons.append(f"the bank statement does not independently show one deposit of {banked} for this payment (it shows: {said})")
    if not explained.corroborated:
        open_causes = [f"{c.kind} is {c.status}" for c in explained.causes if c.status in ("supported", "unknown")]
        reasons.append("the difference is not corroborated" + (f": {', '.join(open_causes)}" if open_causes else ""))
    deposit = next((l for l in lines if ours and ours[0].lines and l.id == ours[0].lines[0]), None)
    if deposit is not None and posting.date:
        reasons += other_lines_that_could_explain(lines, deposit, posting, posted - banked)
        needed = (_dt.date.fromisoformat(max(deposit.date, posting.date)) + _dt.timedelta(days=SETTLEMENT_DAYS)).isoformat()
        first, last = min(l.date for l in lines), max(l.date for l in lines)
        evidence["deposit"] = {"line": deposit.id, "date": deposit.date, "amount": str(deposit.amount), "currency": deposit.currency,
                               "description": deposit.description[:200]}
        evidence["statement_covers"] = [first, last]
        if last < needed:
            reasons.append(f"the statement ends {last}; it must reach {needed} before a later instalment or fee can be ruled out")
        if first > posting.date:
            reasons.append(f"the statement starts {first}, after the payment was recorded ({posting.date})")
    elif deposit is not None:
        reasons.append("the payment has no posting date")
    return Assessment(reasons, evidence, doc)


# ── what happened, kept ──────────────────────────────────────────────────────────
class Journal:
    """One row per (tenant, payment). Never erased: it is the record of an approved change to the accounts."""

    def __init__(self, state_dir: "str | Path", clock: Callable[[], float] = time.time) -> None:
        db = Path(state_dir) / "corrections.sqlite"
        if not db.exists():
            os.close(os.open(db, os.O_CREAT | os.O_WRONLY, 0o600))
        self.clock, self._lock = clock, threading.RLock()
        self._db = sqlite3.connect(str(db), check_same_thread=False, isolation_level=None, timeout=30)
        self._db.execute("PRAGMA synchronous=FULL")             # a step is on disk before the next call to ERPNext
        self._db.execute("CREATE TABLE IF NOT EXISTS corrections(tenant TEXT NOT NULL, payment TEXT NOT NULL, data TEXT NOT NULL,"
                         " PRIMARY KEY(tenant, payment))")

    def get(self, tenant: str, payment: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT data FROM corrections WHERE tenant=? AND payment=?", (tenant, payment)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, tenant: str, payment: str, entry: Dict[str, Any], state: str, note: str = "") -> Dict[str, Any]:
        entry = {**entry, "state": state}
        entry["history"] = [*entry.get("history", []), {"at": self.clock(), "state": state, **({"note": note[:300]} if note else {})}]
        with self._lock:
            self._db.execute("INSERT INTO corrections(tenant,payment,data) VALUES(?,?,?) ON CONFLICT(tenant,payment) DO UPDATE SET data=excluded.data",
                             (tenant, payment, json.dumps(entry, default=str)))
        return entry


@dataclass(frozen=True)
class Context:
    tenant: str
    journal: Journal
    approver: str = ""
    evidence: Optional[Mapping[str, Any]] = None
    #: optional: how many messages mention this payment's invoice or amounts (shown to the approver, never deciding)
    mentions: Optional[Callable[[str, str], List[str]]] = None


def _figures(i: Mapping[str, Any]):
    return str(i["reference"]), str(i["payment_entry"]), money(i["posted_amount"]), money(i["bank_amount"])


def _half_done(writer: Any, ctx: Context, payment: str) -> Optional[Dict[str, Any]]:
    """The journal's entry when a correction of this payment started AND the original is no longer submitted."""
    entry = ctx.journal.get(ctx.tenant, payment)
    if entry and entry["state"] in UNFINISHED and writer.document("Payment Entry", payment).get("docstatus") == 2:
        return entry
    return None


def prepare(ctx: Context, writer: Any, i: Mapping[str, Any]) -> Dict[str, Any]:
    """Runs when an agent proposes: refuses (``NotApplied``) what could never be approved, and returns what the approver
    will be shown and attest to. It writes nothing."""
    if not enabled():
        raise NotApplied(f"payment corrections are not enabled on this deployment ({ENV_ENABLED} and {ENV_STATEMENTS})")
    reference, payment, posted, banked = _figures(i)
    entry = ctx.journal.get(ctx.tenant, payment)
    if entry and entry["state"] == "done":
        raise NotApplied(f"this payment was already corrected: see {entry.get('replacement')}")
    half = _half_done(writer, ctx, payment)
    if half is not None:
        if (half["reference"], money(half["posted"]), money(half["bank"])) != (reference, posted, banked):
            raise NotApplied("a correction of this payment is half done with other figures: a person has to finish it in ERPNext")
        first = {k: half["evidence"][k] for k in ("statement_sha256", "classification", "deposit", "statement_covers") if k in half["evidence"]}
        return {**first, "mode": "finish", "attestation": ATTEST_FINISH,
                "summary": f"FINISH correcting {payment}: it is already CANCELLED; record the payment of {banked} that replaces it",
                "original_approval": {"request_id": half["request_id"], "approver": half["approver"]},
                "show": [["Where it stopped", half["history"][-1].get("note") or half["state"]],
                         ["First approved by", f"{half['approver']} (request {half['request_id']})"]]}
    found = assess(writer, ctx.tenant, reference, payment, posted, banked)
    if not found.ok:
        raise NotApplied("this difference does not justify cancelling and re-entering the payment: " + "; ".join(found.reasons))
    ev = found.evidence
    line = found.document["references"][0]
    dep = ev["deposit"]
    related = ctx.mentions(ctx.tenant, reference) if ctx.mentions else []
    show = [["Bank statement shows", f"{dep['amount']} {dep['currency']} on {dep['date']} ({dep['line']}) {dep['description']}"],
            ["Statement covers", " to ".join(ev["statement_covers"])],
            ["Ruled out from the statement and the payment", ", ".join(c["kind"].replace("_", " ") for c in ev["classification"]["causes"]
                                                                     if c["status"] == "excluded")],
            ["Effect on the invoice", f"{line['reference_name']}: {min(banked, money(line['allocated_amount']))} stays allocated to it; "
                                      f"the rest of {posted} becomes outstanding again"]]
    if related:
        show.append(["Messages that mention it (read them)", ", ".join(related[:10])])
    return {"mode": "correct", "attestation": ATTEST, "show": show, **ev}


# ── writing ──────────────────────────────────────────────────────────────────────
def _amendments(client: Any, original: str) -> List[dict]:
    rows = client.read_list("Payment Entry", ["name", "docstatus", "amended_from", "reference_no"], None, 0)
    return [r for r in rows if str(r.get("amended_from") or "") == original and int(r.get("docstatus") or 0) != 2]


def _live_under(client: Any, reference: str) -> List[str]:
    rows = client.read_list("Payment Entry", ["name", "docstatus", "reference_no"], None, 0)
    return sorted(str(r["name"]) for r in rows if int(r.get("docstatus") or 0) != 2 and str(r.get("reference_no") or "") == reference)


def _matches_plan(doc: Mapping[str, Any], plan: Mapping[str, Any]) -> bool:
    refs = doc.get("references") or []
    want = plan["references"][0]
    return (doc.get("amended_from") == plan["amended_from"] and doc.get("party") == plan.get("party")
            and doc.get("reference_no") == plan.get("reference_no") and money(doc.get("paid_amount")) == money(plan["paid_amount"])
            and money(doc.get("received_amount")) == money(plan["received_amount"]) and len(refs) == 1
            and refs[0].get("reference_name") == want["reference_name"]
            and money(refs[0].get("allocated_amount")) == money(want["allocated_amount"])
            and not doc.get("deductions") and not doc.get("taxes"))


def verify_ledger(writer: Any, entry: Mapping[str, Any], replacement: str) -> List[str]:
    """What is wrong after the correction, read from the application's own vouchers and general ledger ([] = consistent)."""
    original, banked = entry["payment"], money(entry["bank"])
    before, plan = entry["snapshot"], entry["plan"]
    problems: List[str] = []
    old, new = writer.document("Payment Entry", original), writer.document("Payment Entry", replacement)
    if old.get("docstatus") != 2 or money(old.get("paid_amount")) != money(before.get("paid_amount")):
        problems.append("the original is not kept as the cancelled voucher it was")
    if new.get("docstatus") != 1 or not _matches_plan(new, plan):
        problems.append("the replacement is not the submitted amendment that was approved")
    rows = writer.read_list("GL Entry", ["voucher_no", "account", "debit", "credit", "is_cancelled"],
                            [["voucher_no", "in", [original, replacement]]], 0)
    live = [r for r in rows if not int(r.get("is_cancelled") or 0)]
    net: Dict[str, Decimal] = {}
    for r in live:
        if r.get("voucher_no") == original:
            net[r["account"]] = net.get(r["account"], Decimal("0")) + money(r.get("debit")) - money(r.get("credit"))
    if any(v != 0 for v in net.values()):
        problems.append("the cancelled voucher still moves the ledger")
    mine = [r for r in live if r.get("voucher_no") == replacement]
    debit, credit = sum((money(r.get("debit")) for r in mine), Decimal("0")), sum((money(r.get("credit")) for r in mine), Decimal("0"))
    if not mine or debit != credit or debit != banked:
        problems.append(f"the replacement's ledger entries do not balance at {banked} (debit {debit}, credit {credit})")
    invoice = plan["references"][0]["reference_name"]
    expected = money(entry["outstanding_before"]) + money(before["references"][0]["allocated_amount"]) - money(plan["references"][0]["allocated_amount"])
    now = money(writer.document("Sales Invoice", invoice).get("outstanding_amount"))
    if now != expected:
        problems.append(f"{invoice} shows {now} outstanding, expected {expected}")
    done = next((c for c in reconcile.corrections(Reader(writer)).events if c.reference == entry["reference"]), None)
    if not (done and done.reconciled and done.replacement_payment == replacement):
        problems.append("the books do not yet read as reconciled with the bank line")
    return problems


def _lineage(writer: Any, entry: Mapping[str, Any], replacement: str, footer: str) -> List[str]:
    """A comment on each voucher naming the other, the approval and the evidence. Best effort: its failure is said."""
    digest = hashlib.sha256(json.dumps(entry["evidence"], sort_keys=True).encode()).hexdigest()
    texts = ((entry["payment"], f"Cancelled and entered again as {replacement} for {entry['bank']}, the amount on the bank statement."),
             (replacement, f"Replaces {entry['payment']} ({entry['posted']}), which is kept as cancelled."))
    failed = []
    for name, text in texts:
        try:
            writer.add_lineage_note("Payment Entry", name, f"{text} Evidence sha256 {digest}.", footer)
        except (NotApplied, OutcomeUnknown):
            failed.append(name)
    return failed


def _replace(writer: Any, ctx: Context, entry: Dict[str, Any], footer: str):
    """From a cancelled original to a verified replacement, creating at most one amendment however often it is run."""
    from agentic_os.governed_writes.actions import Outcome  # noqa: PLC0415
    original, plan, journal = entry["payment"], entry["plan"], ctx.journal

    def stop(why: str):
        journal.save(ctx.tenant, original, entry, entry["state"], why)
        raise OutcomeUnknown(f"CHECK NOW: Payment Entry {original} is cancelled and {why}. Look at Payment Entry in ERPNext. "
                             "Proposing the same correction again offers only to finish it, after a new approval")

    try:
        client = Reader(writer)
        made = _amendments(client, original)
        others = [n for n in _live_under(client, entry["reference"]) if n not in {m["name"] for m in made}]
    except (NotApplied, OutcomeUnknown) as e:
        stop(f"what was recorded since could not be read ({e})")
    if others:
        stop(f"{', '.join(others)} was recorded under the same reference by other means, so no replacement is entered")
    if len(made) > 1:
        stop("more than one amendment of it exists")
    try:
        if made:
            name = str(made[0]["name"])
            doc = writer.document("Payment Entry", name)
            if not _matches_plan(doc, plan):
                raise NotApplied(f"the amendment {name} found there differs from what was approved")
            draft = doc.get("docstatus") == 0
        else:
            entry = journal.save(ctx.tenant, original, entry, "create_sent")
            name, draft = writer.create_draft("Payment Entry", plan), True
        entry = journal.save(ctx.tenant, original, {**entry, "replacement": name}, "draft" if draft else "submitted")
        if draft:
            writer.submit("Payment Entry", name)
            entry = journal.save(ctx.tenant, original, entry, "submitted")
    except (NotApplied, OutcomeUnknown) as e:
        stop(f"its replacement for {entry['bank']} was not confirmed ({e})")
    try:
        problems = verify_ledger(writer, entry, name)
    except Exception as e:  # noqa: BLE001
        problems = [f"it could not be read back ({type(e).__name__})"]
    if problems:
        journal.save(ctx.tenant, original, entry, "submitted", "; ".join(problems))
        return Outcome(True, False, name, f"{name} was entered, but the ledger check failed: {'; '.join(problems)}. Look at both vouchers")
    uncommented = _lineage(writer, entry, name, footer)
    journal.save(ctx.tenant, original, entry, "done", f"lineage comment missing on {', '.join(uncommented)}" if uncommented else "")
    invoice = plan["references"][0]["reference_name"]
    return Outcome(True, True, name, f"{original} is kept as cancelled; {name} records {entry['bank']}, its amendment. Ledger and {invoice} "
                   "read back consistent" + (f". The note on {', '.join(uncommented)} could not be added" if uncommented else ""))


def perform(writer: Any, i: Mapping[str, Any], footer: str, rid: str, ctx: Context):
    from agentic_os.governed_writes.actions import Outcome  # noqa: PLC0415
    if not enabled():
        raise NotApplied(f"payment corrections are not enabled on this deployment ({ENV_ENABLED} and {ENV_STATEMENTS})")
    reference, payment, posted, banked = _figures(i)
    approved = dict(ctx.evidence or {})
    entry = ctx.journal.get(ctx.tenant, payment)
    if entry and entry["state"] == "done":
        if (entry["reference"], money(entry["bank"])) != (reference, banked):
            raise NotApplied("this payment was already corrected with other figures")
        return Outcome(False, True, entry["replacement"], "already corrected; nothing was changed")
    half = _half_done(writer, ctx, payment)
    if approved.get("mode") == "finish":
        if half is None:
            raise NotApplied("the original payment is not cancelled any more as it was when this was approved: propose it again")
        half = {**half, "finish_approval": {"request_id": rid, "approver": ctx.approver}}
        return _replace(writer, ctx, ctx.journal.save(ctx.tenant, payment, half, half["state"], f"finishing, approved by {ctx.approver}"), footer)
    if half is not None:
        raise NotApplied("a correction of this payment is half done: propose it again to approve finishing it")
    found = assess(writer, ctx.tenant, reference, payment, posted, banked)
    if not found.ok:
        raise NotApplied("this difference no longer justifies cancelling the payment: " + "; ".join(found.reasons))
    if approved.get("mode") != "correct" or found.evidence["statement_sha256"] != approved.get("statement_sha256"):
        raise NotApplied("the bank statement changed since it was approved: propose it again from the current evidence")
    doc = found.document
    line = doc["references"][0]
    plan: Dict[str, Any] = {k: doc[k] for k in COPIED if doc.get(k) not in (None, "")}
    plan.update(amended_from=payment, paid_amount=float(banked), received_amount=float(banked),
                remarks=f"Entered again for the amount on the bank statement. {footer}"[:1000],
                references=[{"reference_doctype": line["reference_doctype"], "reference_name": line["reference_name"],
                             "allocated_amount": float(min(banked, money(line["allocated_amount"])))}])
    invoice = writer.document("Sales Invoice", line["reference_name"])
    entry = ctx.journal.save(ctx.tenant, payment, {
        "payment": payment, "reference": reference, "posted": str(posted), "bank": str(banked), "request_id": rid,
        "approver": ctx.approver, "attestation": approved.get("attestation", ""), "evidence": approved, "snapshot": doc, "plan": plan,
        "outstanding_before": str(money(invoice.get("outstanding_amount"))),
        "history": (entry or {}).get("history", [])}, "planned")
    entry = ctx.journal.save(ctx.tenant, payment, entry, "cancel_sent")
    try:
        writer.cancel("Payment Entry", payment)                          # from here on the books may have changed
    except NotApplied as e:
        ctx.journal.save(ctx.tenant, payment, entry, "abandoned", f"the cancellation was refused: {e}")
        raise
    except OutcomeUnknown as e:
        ctx.journal.save(ctx.tenant, payment, entry, "cancel_sent", f"the cancellation was not confirmed: {e}")
        raise OutcomeUnknown(f"CHECK NOW: the cancellation of Payment Entry {payment} was not confirmed ({e}); no replacement was "
                             "entered. Look at it in ERPNext before anything else") from None
    return _replace(writer, ctx, ctx.journal.save(ctx.tenant, payment, entry, "cancelled"), footer)


__all__ = ["ENV_ENABLED", "ENV_STATEMENTS", "SETTLEMENT_DAYS", "ATTEST", "ATTEST_FINISH", "Context", "Journal", "Reader", "enabled",
           "load_statements", "assess", "prepare", "perform", "refusal_for", "verify_ledger"]

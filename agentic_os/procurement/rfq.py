"""Request-for-quotation workflow over the procurement catalog.

    draft -> (approval) send -> record the suppliers' quotes -> compare -> (approval) award

* A draft goes only to suppliers in the tenant's own supplier records that have a contact address there.
  An address found anywhere else is not a contact the tenant gave us.
* Each supplier's message contains the tenant's requirement and nothing about any other supplier or price.
* ``rfq.send`` and ``rfq.award`` change the outside world (a message leaves; a commitment is made), so both
  are approval-gated through ``approval_gate.gated``. Without a runtime-delivered approval they stage a
  preview and do nothing.
* A recorded quote becomes catalog data of kind ``supplier_quote``, tied to the quote document by digest.
* An award produces a purchase-order draft. It is not written to an ERP here.

Delivery is a seam: ``OutboxSender`` writes the message to a directory (nothing leaves the machine) and
``SmtpSender`` hands it to an SMTP server. Which one is used is deployment configuration.
"""
from __future__ import annotations

import hashlib
import json
import os
import smtplib
import time
import uuid
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlparse

from agentic_os.approval_gate import gated
from agentic_os.mission.operator_sdk import Operator, capability

from .catalog import (
    INTERNAL_SOURCING, SUPPLIER_QUOTE, SYNTHETIC, VERIFIED, Catalog, CatalogError, CatalogSource, today, to_minor,
)

DRAFT, SENT, QUOTED, AWARDED = "draft", "sent", "quoted", "awarded"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rfqs (
    rfq_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, state TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rfq_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, rfq_id TEXT NOT NULL, at TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class OutboxSender:
    """Writes each message as an .eml file. Nothing is transmitted."""
    name = "outbox"

    def __init__(self, directory: str) -> None:
        self.dir = Path(directory)

    def send(self, msg: EmailMessage) -> dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{msg['X-RFQ-Id']}-{msg['X-Supplier-Ref']}.eml"
        path.write_bytes(bytes(msg))
        return {"transmitted": False, "staged_file": str(path)}


class SmtpSender:
    """Hands each message to an SMTP server (``smtp://host:port`` or ``smtps://user:pass@host:port``)."""
    name = "smtp"

    def __init__(self, url: str, timeout: float = 20.0) -> None:
        u = urlparse(url)
        if u.scheme not in ("smtp", "smtps") or not u.hostname:
            raise CatalogError("PROCUREMENT_SMTP_URL must be smtp://host:port or smtps://user:password@host:port")
        self._u, self._timeout = u, timeout

    def send(self, msg: EmailMessage) -> dict:
        u = self._u
        cls = smtplib.SMTP_SSL if u.scheme == "smtps" else smtplib.SMTP
        with cls(u.hostname, u.port or (465 if u.scheme == "smtps" else 25), timeout=self._timeout) as s:
            if u.username:
                s.login(u.username, u.password or "")
            refused = s.send_message(msg)
        return {"transmitted": True, "server": f"{u.hostname}:{u.port or ''}", "refused": sorted(refused)}


def sender_from_env():
    """``PROCUREMENT_SMTP_URL`` selects real delivery; otherwise ``PROCUREMENT_RFQ_OUTBOX`` (a directory).
    With neither set there is no sender and ``rfq.send`` reports that instead of pretending."""
    if os.environ.get("PROCUREMENT_SMTP_URL"):
        return SmtpSender(os.environ["PROCUREMENT_SMTP_URL"])
    if os.environ.get("PROCUREMENT_RFQ_OUTBOX"):
        return OutboxSender(os.environ["PROCUREMENT_RFQ_OUTBOX"])
    return None


class RfqBook:
    """RFQs for one catalog. Every method is scoped to the tenant passed in."""

    def __init__(self, catalog: Catalog, *, from_address: str = "", sender=None) -> None:
        self.catalog, self.from_address, self.sender = catalog, from_address, sender
        self._db, self._lock = catalog._db, catalog._lock
        self._db.executescript(_SCHEMA)

    # ── storage ─────────────────────────────────────────────────────────────────────────────────────
    def _save(self, rfq: dict, event: str, detail: Any = "") -> dict:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO rfqs VALUES (?,?,?,?,?)",
                             (rfq["rfq_id"], rfq["tenant"], rfq["state"], json.dumps(rfq), rfq["created_at"]))
            self._db.execute("INSERT INTO rfq_events (rfq_id, at, event, detail) VALUES (?,?,?,?)",
                             (rfq["rfq_id"], _now(), event, json.dumps(detail, default=str)[:2000]))
        return rfq

    def get(self, tenant: str, rfq_id: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT body FROM rfqs WHERE rfq_id = ? AND tenant = ?", (rfq_id, tenant)).fetchone()
        if not row:
            raise CatalogError("no such RFQ for this tenant")
        return json.loads(row["body"])

    def events(self, tenant: str, rfq_id: str) -> List[dict]:
        self.get(tenant, rfq_id)
        with self._lock:
            rows = self._db.execute("SELECT at, event, detail FROM rfq_events WHERE rfq_id = ? ORDER BY id", (rfq_id,)).fetchall()
        return [{"at": r["at"], "event": r["event"], "detail": json.loads(r["detail"])} for r in rows]

    # ── draft ───────────────────────────────────────────────────────────────────────────────────────
    def draft(self, tenant: str, *, lines: Sequence[Mapping[str, Any]], supplier_refs: Sequence[str],
              respond_by: str, need_by: str = "", incoterm: str = "", deliver_to: str = "", note: str = "") -> dict:
        if not lines:
            raise CatalogError("an RFQ needs at least one line")
        clean = []
        for n, ln in enumerate(lines, 1):
            desc, qty = str(ln.get("description", "") or "").strip(), ln.get("quantity")
            if not desc or not isinstance(qty, (int, float)) or isinstance(qty, bool) or qty <= 0:
                raise CatalogError(f"line {n} needs a description and a positive quantity")
            clean.append({"line": n, "description": desc, "quantity": qty, "unit": str(ln.get("unit") or "each"),
                          "identifiers": {k: str(v) for k, v in (ln.get("identifiers") or {}).items() if v}})
        if not respond_by or respond_by < today():
            raise CatalogError("respond_by must be a date that has not passed (YYYY-MM-DD)")
        recipients, seen = [], set()
        for ref in supplier_refs:
            if ref in seen:
                continue
            seen.add(ref)
            sup = self.catalog.supplier(tenant, ref)
            if sup is None:
                raise CatalogError(f"supplier {ref!r} is not in this tenant's supplier records")
            if not sup.get("email"):
                raise CatalogError(f"supplier {ref!r} has no contact address in this tenant's records")
            recipients.append({"supplier_ref": ref, "name": sup["name"], "email": sup["email"],
                               "identity": (sup.get("identity") or {}).get("status", "not_checked")})
        if not recipients:
            raise CatalogError("an RFQ needs at least one supplier")
        rfq = {"rfq_id": "rfq-" + uuid.uuid4().hex[:12], "tenant": tenant, "state": DRAFT, "lines": clean,
               "recipients": recipients, "respond_by": respond_by, "need_by": need_by, "incoterm": incoterm.upper(),
               "deliver_to": deliver_to, "note": note, "created_at": _now(), "sent": [], "quotes": {}, "award": None}
        return self._save(rfq, "drafted", {"suppliers": [r["supplier_ref"] for r in recipients], "lines": len(clean)})

    def message_for(self, rfq: dict, recipient: dict) -> EmailMessage:
        """The message one supplier receives: this buyer's requirement and nothing about other suppliers."""
        msg = EmailMessage()
        msg["Subject"] = f"Request for quotation {rfq['rfq_id']}"
        msg["From"] = self.from_address or "procurement@localhost"
        msg["To"] = recipient["email"]
        msg["X-RFQ-Id"], msg["X-Supplier-Ref"] = rfq["rfq_id"], recipient["supplier_ref"]
        rows = []
        for ln in rfq["lines"]:
            ids = ", ".join(f"{k.upper()} {v}" for k, v in ln["identifiers"].items())
            rows.append(f"  {ln['line']}. {ln['description']}" + (f" ({ids})" if ids else "") + f" - {ln['quantity']:g} {ln['unit']}")
        terms = [f"Please reply by {rfq['respond_by']}."]
        if rfq["need_by"]:
            terms.append(f"Delivery needed by {rfq['need_by']}.")
        if rfq["incoterm"]:
            terms.append(f"Incoterm: {rfq['incoterm']}" + (f", {rfq['deliver_to']}" if rfq["deliver_to"] else "") + ".")
        msg.set_content(
            f"Dear {recipient['name']},\n\nWe would like a quotation for:\n\n" + "\n".join(rows) + "\n\n" + " ".join(terms) +
            "\nFor each line please state unit price and currency, minimum order quantity, lead time and how long the "
            "price is valid.\n" + (f"\n{rfq['note']}\n" if rfq["note"] else "") + f"\nReference: {rfq['rfq_id']}\n")
        return msg

    # ── send (approval-gated at the operator) ───────────────────────────────────────────────────────
    def send(self, tenant: str, rfq_id: str) -> dict:
        rfq = self.get(tenant, rfq_id)
        if rfq["state"] != DRAFT:
            raise CatalogError(f"this RFQ is already {rfq['state']}")
        if rfq["respond_by"] < today():
            raise CatalogError("the reply date has passed; draft a new RFQ")
        if self.sender is None:
            raise CatalogError("no delivery is configured (PROCUREMENT_SMTP_URL or PROCUREMENT_RFQ_OUTBOX)")
        results = []
        for rec in rfq["recipients"]:
            sup = self.catalog.supplier(tenant, rec["supplier_ref"])           # re-read: the record may have changed
            if not sup or sup.get("email") != rec["email"]:
                results.append({"supplier_ref": rec["supplier_ref"], "error": "supplier contact changed since the draft"})
                continue
            msg = self.message_for(rfq, rec)
            try:
                out = self.sender.send(msg)
            except Exception as e:  # noqa: BLE001
                results.append({"supplier_ref": rec["supplier_ref"], "error": f"{type(e).__name__}: {e}"[:200]})
                continue
            results.append({"supplier_ref": rec["supplier_ref"], "to": rec["email"],
                            "message_sha256": hashlib.sha256(bytes(msg)).hexdigest(), **out})
        ok = [r for r in results if "error" not in r]
        rfq["sent"] = results
        if ok:
            rfq["state"] = SENT
        self._save(rfq, "sent" if ok else "send_failed", {"via": self.sender.name, "results": results})
        return {"rfq_id": rfq_id, "state": rfq["state"], "via": self.sender.name, "results": results,
                "transmitted": bool(ok) and all(r.get("transmitted") for r in ok)}

    # ── quotes ──────────────────────────────────────────────────────────────────────────────────────
    def record_quote(self, tenant: str, rfq_id: str, supplier_ref: str, *, lines: Sequence[Mapping[str, Any]],
                     document: bytes, document_name: str, received_from: str, valid_until: str) -> dict:
        """Record a supplier's reply. The quote document's digest ties the prices to what was received."""
        rfq = self.get(tenant, rfq_id)
        if rfq["state"] not in (SENT, QUOTED):
            raise CatalogError("quotes are recorded against an RFQ that has been sent")
        if supplier_ref not in {r["supplier_ref"] for r in rfq["recipients"]}:
            raise CatalogError("that supplier was not asked on this RFQ")
        if not document or not document_name:
            raise CatalogError("a quote needs the document it was read from")
        if not valid_until or valid_until < today():
            raise CatalogError("a quote needs a validity date that has not passed")
        sup = self.catalog.supplier(tenant, supplier_ref) or {}
        doc_ref = f"sha256:{hashlib.sha256(document).hexdigest()}:{document_name}"
        source_id = f"{rfq_id}:{supplier_ref}"
        self.catalog.register_source(CatalogSource(
            source_id=source_id, tenant=tenant, kind=SUPPLIER_QUOTE, name=f"Quote from {sup.get('name', supplier_ref)} on {rfq_id}",
            permitted_uses=(INTERNAL_SOURCING,), authorized_by=received_from, authorization_ref=doc_ref, authorized_at=today(),
            expires_at=valid_until))
        by_line = {ln["line"]: ln for ln in rfq["lines"]}
        rows = []
        for q in lines:
            ln = by_line.get(q.get("line"))
            if ln is None:
                raise CatalogError(f"the RFQ has no line {q.get('line')!r}")
            rows.append({"supplier_ref": supplier_ref, "supplier_name": sup.get("name", ""), "sku": f"{rfq_id}-L{ln['line']}",
                         "title": ln["description"], "currency": q.get("currency", ""), "unit_price": q.get("unit_price", ""),
                         "moq": q.get("moq", 1), "unit": ln["unit"], "lead_time_days": q.get("lead_time_days", ""),
                         "gtin": ln["identifiers"].get("gtin", ""), "mpn": ln["identifiers"].get("mpn", ""),
                         "valid_until": valid_until, "document_ref": doc_ref, "incoterm": rfq["incoterm"]})
        res = self.catalog.ingest(tenant, source_id, rows)
        if res["rejected"]:
            raise CatalogError("quote not recorded: " + "; ".join(f"line {r['row']}: {r['reason']}" for r in res["rejects"]))
        rfq["quotes"][supplier_ref] = {"document_ref": doc_ref, "received_from": received_from, "valid_until": valid_until,
                                       "lines": [{"line": q["line"], "currency": str(q.get("currency", "")).upper(),
                                                  "unit_price_minor": to_minor(q["unit_price"], str(q.get("currency", ""))),
                                                  "moq": float(q.get("moq", 1)), "lead_time_days": q.get("lead_time_days")}
                                                 for q in lines], "recorded_at": _now()}
        rfq["state"] = QUOTED
        self._save(rfq, "quote_recorded", {"supplier_ref": supplier_ref, "document_ref": doc_ref})
        return {"rfq_id": rfq_id, "supplier_ref": supplier_ref, "document_ref": doc_ref, "lines": len(rows)}

    def compare(self, tenant: str, rfq_id: str) -> dict:
        """Total per supplier over the lines it quoted in full. A supplier missing a line, quoting below the
        needed quantity's MOQ, or in another currency than the rest is listed with the reason, not ranked."""
        rfq = self.get(tenant, rfq_id)
        need = {ln["line"]: ln["quantity"] for ln in rfq["lines"]}
        ranked, set_aside = [], []
        for ref, q in rfq["quotes"].items():
            got = {ln["line"]: ln for ln in q["lines"]}
            reason = ""
            if q["valid_until"] < today():
                reason = f"quote validity ended {q['valid_until']}"
            elif set(got) != set(need):
                reason = "does not quote every line: missing " + ", ".join(str(n) for n in sorted(set(need) - set(got)))
            elif len({ln["currency"] for ln in q["lines"]}) != 1:
                reason = "quotes lines in more than one currency"
            elif any(need[n] < got[n]["moq"] for n in need):
                reason = "a line is below the quoted minimum order quantity"
            if reason:
                set_aside.append({"supplier_ref": ref, "reason": reason})
                continue
            leads = [got[n]["lead_time_days"] for n in need]
            ranked.append({"supplier_ref": ref, "currency": q["lines"][0]["currency"],
                           "total_minor": sum(int(round(got[n]["unit_price_minor"] * need[n])) for n in need),
                           "longest_lead_time_days": None if any(v is None for v in leads) else max(leads),
                           "valid_until": q["valid_until"], "document_ref": q["document_ref"]})
        currencies = sorted({r["currency"] for r in ranked})
        ranked.sort(key=lambda r: (r["currency"], r["total_minor"]))
        if not ranked:
            rec = {"action": "none", "reason": "no complete, current quote"}
        elif len(currencies) > 1:
            rec = {"action": "review", "reason": "quotes are in different currencies (" + ", ".join(currencies) +
                   "); no exchange rate is assumed"}
        else:
            rec = {"action": "award", "supplier_ref": ranked[0]["supplier_ref"],
                   "reason": "lowest total among complete, current quotes"}
        waiting = sorted({r["supplier_ref"] for r in rfq["recipients"]} - set(rfq["quotes"]))
        return {"rfq_id": rfq_id, "ranked": ranked, "set_aside": set_aside, "no_reply": waiting, "recommendation": rec}

    # ── award (approval-gated at the operator) ──────────────────────────────────────────────────────
    def award(self, tenant: str, rfq_id: str, supplier_ref: str) -> dict:
        rfq = self.get(tenant, rfq_id)
        if rfq["state"] != QUOTED:
            raise CatalogError("an award needs at least one recorded quote and no earlier award")
        cmp_ = self.compare(tenant, rfq_id)
        row = next((r for r in cmp_["ranked"] if r["supplier_ref"] == supplier_ref), None)
        if row is None:
            raise CatalogError("that supplier has no complete, current quote on this RFQ")
        q = rfq["quotes"][supplier_ref]
        got = {ln["line"]: ln for ln in q["lines"]}
        po = {"kind": "supply.purchase_order", "status": "draft", "supplier_ref": supplier_ref, "rfq_id": rfq_id,
              "currency": row["currency"], "total_minor": row["total_minor"], "quote_document_ref": q["document_ref"],
              "lines": [{"line": ln["line"], "part": ln["description"], "quantity": ln["quantity"], "unit": ln["unit"],
                         "unit_price_minor": got[ln["line"]]["unit_price_minor"]} for ln in rfq["lines"]],
              "not_lowest": cmp_["recommendation"].get("supplier_ref") not in (None, supplier_ref)}
        rfq["state"], rfq["award"] = AWARDED, po
        self._save(rfq, "awarded", {"supplier_ref": supplier_ref, "total_minor": row["total_minor"]})
        return {"rfq_id": rfq_id, "state": AWARDED, "purchase_order_draft": po}


def verify_supplier_identity(catalog: Catalog, tenant: str, supplier_ref: str, provider) -> dict:
    """Check a supplier's legal name against a register (a ``COMPANY_IDENTITY`` provider such as GLEIF) and
    store the outcome on the supplier record. ``verified`` needs exactly one register entry with that legal
    name; otherwise the candidates are kept for a person to choose from."""
    import re  # noqa: PLC0415
    from runtime_contracts.protocol import AcquisitionFailure, Capability, EvidenceRequest  # noqa: PLC0415

    sup = catalog.supplier(tenant, supplier_ref)
    if sup is None:
        raise CatalogError("no such supplier for this tenant")
    norm = lambda s: " ".join(re.sub(r"[^\w\s]", " ", (s or "").lower()).split())  # noqa: E731
    res = provider.acquire(EvidenceRequest(decision_case_id=f"supplier:{supplier_ref}", capability=Capability.COMPANY_IDENTITY,
                                           subject_refs=(sup["name"],), purpose="supplier onboarding", tenant=tenant))
    if not res.ok:
        status = "no_match" if res.failure == AcquisitionFailure.NO_MATCH else "unavailable"
        identity: Dict[str, Any] = {"status": status, "provider": provider.provider_id, "detail": res.detail[:160]}
    else:
        art = res.artifacts[0]
        records = [dict(o) for o in art.observations]
        exact = [r for r in records if norm(r.get("legal_name", "")) == norm(sup["name"])]
        identity = {"status": "verified" if len(exact) == 1 else "ambiguous", "provider": provider.provider_id,
                    "retrieved_at": art.retrieved_at, "license_scope": art.license_scope,
                    "response_digest": art.raw_response_digest,
                    "record": exact[0] if len(exact) == 1 else None, "candidates": records if len(exact) != 1 else []}
    identity["checked_at"] = _now()
    ext = dict(sup.get("external_ids") or {})
    if identity["status"] == "verified" and identity["record"].get("lei"):
        ext["lei"] = identity["record"]["lei"]
    catalog.upsert_supplier(tenant, supplier_ref, name=sup["name"], email=sup.get("email", ""), country=sup.get("country", ""),
                            external_ids=ext, identity=identity)
    return identity


def build_procurement_operator(catalog: Catalog, book: RfqBook, *, tenant: str) -> Operator:
    """The procurement capabilities for ONE tenant. The tenant is fixed here, never read from the input, so
    a caller cannot ask for another tenant's catalog by naming it."""
    if not tenant:
        raise CatalogError("the procurement operator is built for a tenant")

    def safe(fn, marker: str):
        def handler(inp: dict) -> dict:
            try:
                return {"status": "done", marker: True, **fn(inp)}
            except CatalogError as e:
                return {"status": "refused", marker: False, "error": str(e)}
        return handler

    search = safe(lambda i: {"items": catalog.search(tenant, text=str(i.get("text", "")), identifiers=i.get("identifiers"),
                                                     include_synthetic=i.get("include_synthetic") is True)}, "catalog_searched")
    compare = safe(lambda i: catalog.compare(tenant, quantity=i.get("quantity", 0), text=str(i.get("text", "")),
                                             identifiers=i.get("identifiers"), need_by_days=i.get("need_by_days")), "offers_compared")
    draft = safe(lambda i: {"rfq": book.draft(tenant, lines=i.get("lines") or [], supplier_refs=i.get("supplier_refs") or [],
                                              respond_by=str(i.get("respond_by", "")), need_by=str(i.get("need_by", "")),
                                              incoterm=str(i.get("incoterm", "")), deliver_to=str(i.get("deliver_to", "")),
                                              note=str(i.get("note", "")))}, "rfq_drafted")
    send = safe(lambda i: book.send(tenant, str(i.get("rfq_id", ""))), "rfq_sent")
    quotes = safe(lambda i: book.compare(tenant, str(i.get("rfq_id", ""))), "quotes_compared")
    award = safe(lambda i: book.award(tenant, str(i.get("rfq_id", "")), str(i.get("supplier_ref", ""))), "rfq_awarded")
    read = dict(side_effecting=False, approval_required=False, permissions=["procurement:read"], latency_ms=300)
    write = dict(side_effecting=True, approval_required=True, permissions=["procurement:write"], latency_ms=1500,
                 concurrency_mode="exclusive", concurrency_key="RFQ:{rfq_id}")
    return Operator("procurement", [
        capability("procurement.catalog.search", search, provides=["catalog_searched"], **read,
                   outputs={"catalog_searched": "catalog items this tenant may use, each classed verified, unconfirmed or synthetic"}),
        capability("procurement.offers.compare", compare, provides=["offers_compared"], **read,
                   outputs={"offers_compared": "offers for one requirement with a buy / request-quote recommendation"}),
        capability("procurement.rfq.draft", draft, provides=["rfq_drafted"], side_effecting=False, approval_required=False,
                   permissions=["procurement:write"], latency_ms=300,
                   outputs={"rfq_drafted": "an RFQ stored as a draft; nothing is sent"}),
        capability("procurement.rfq.send", gated("procurement.rfq.send", send, rfq_sent=False), provides=["rfq_sent"], **write,
                   irreversible="a message sent to a supplier cannot be recalled",
                   outputs={"rfq_sent": "the RFQ delivered to each supplier's recorded contact"}),
        capability("procurement.rfq.compare", quotes, provides=["quotes_compared"], **read,
                   outputs={"quotes_compared": "recorded quotes ranked, with the reason any was set aside"}),
        capability("procurement.rfq.award", gated("procurement.rfq.award", award, rfq_awarded=False), provides=["rfq_awarded"],
                   **write, irreversible="an award is a commitment to a supplier; reversing it is a new decision",
                   outputs={"rfq_awarded": "the RFQ awarded and a purchase-order draft produced"}),
    ])

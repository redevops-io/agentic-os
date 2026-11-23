"""Procurement catalog — what a buyer may source from, built from data the buyer is entitled to use.

The catalog holds three kinds of data and never blends them:

* **customer records** — the tenant's own supplier master and price lists (exported from its ERP, a
  spreadsheet, a signed price agreement). The tenant owns them.
* **authorized feeds** — a third party's data used under a recorded authorization: who granted it, the
  document that says so, the exact words, what uses it permits and when it lapses. A feed with no such
  record is refused at registration, and a lapsed one stops being searchable.
* **supplier quotes** — answers to an RFQ this tenant sent (see ``rfq``).

A fourth kind, **synthetic**, exists for fixtures and demos. Synthetic rows are labelled, left out of search
unless asked for, and cannot be put on an RFQ.

Nothing here depends on a right to redistribute someone else's catalog. Data is visible to the tenant that
owns it. Sharing a source with another tenant is refused unless that source's authorization lists
``redistribution`` among its permitted uses.

Every item is classed for the reader as ``verified`` (a current supplier quote, or a customer record backed
by a document and still in its validity window), ``unconfirmed`` (a list price, an expired or undated
record) or ``synthetic``. A comparison only recommends buying from a ``verified`` price; for anything else
it recommends asking for a quote.

Money is an integer in the currency's minor units. Prices in different currencies are never compared: no
exchange rate is assumed.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from agentic_os.market.resolution import _gtin_norm

CUSTOMER_RECORDS, AUTHORIZED_FEED, SUPPLIER_QUOTE, SYNTHETIC = (
    "customer_records", "authorized_feed", "supplier_quote", "synthetic")
SOURCE_KINDS = (CUSTOMER_RECORDS, AUTHORIZED_FEED, SUPPLIER_QUOTE, SYNTHETIC)

INTERNAL_SOURCING, REDISTRIBUTION = "internal_sourcing", "redistribution"
USES = (INTERNAL_SOURCING, REDISTRIBUTION)

VERIFIED, UNCONFIRMED = "verified", "unconfirmed"

_ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP", "ISK", "HUF"}
_THREE_DECIMAL = {"BHD", "KWD", "OMR", "JOD", "TND"}


class CatalogError(ValueError):
    """A refusal with a reason the caller can show: a missing authorization, an unknown tenant, bad data."""


def today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def to_minor(amount: Any, currency: str) -> int:
    """``"12.50", "USD"`` -> 1250. Refuses a negative amount or more decimals than the currency has."""
    cur = (currency or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", cur):
        raise CatalogError(f"currency {currency!r} is not a three-letter code")
    exp = 0 if cur in _ZERO_DECIMAL else 3 if cur in _THREE_DECIMAL else 2
    try:
        d = Decimal(str(amount).strip())
    except (InvalidOperation, ValueError):
        raise CatalogError(f"price {amount!r} is not a number") from None
    scaled = d * (10 ** exp)
    if d < 0 or scaled != scaled.to_integral_value():
        raise CatalogError(f"price {amount!r} is negative or has more decimals than {cur} allows")
    return int(scaled)


@dataclass(frozen=True)
class CatalogSource:
    """Where a set of catalog rows came from and what the tenant may do with them."""
    source_id: str
    tenant: str
    kind: str
    name: str
    permitted_uses: Tuple[str, ...] = (INTERNAL_SOURCING,)
    authorized_by: str = ""            # the person or organization that granted or recorded the right
    authorization_ref: str = ""        # the agreement, licence page or document that says so
    authorization_quote: str = ""      # the words in it that grant the use
    authorized_at: str = ""            # ISO date
    expires_at: str = ""               # ISO date; "" = no stated end
    licence: str = ""
    attribution: str = ""              # text the licence requires wherever the data is shown

    def lapsed(self, on: Optional[str] = None) -> bool:
        return bool(self.expires_at) and self.expires_at < (on or today())


def _check_source(src: CatalogSource) -> None:
    if not src.source_id or not src.tenant or not src.name:
        raise CatalogError("a catalog source needs an id, a tenant and a name")
    if src.kind not in SOURCE_KINDS:
        raise CatalogError(f"unknown source kind {src.kind!r}")
    if not set(src.permitted_uses) <= set(USES):
        raise CatalogError(f"unknown permitted use in {src.permitted_uses!r}")
    if src.kind == AUTHORIZED_FEED:
        missing = [f for f in ("authorized_by", "authorization_ref", "authorization_quote", "authorized_at")
                   if not getattr(src, f).strip()]
        if missing:
            raise CatalogError("an authorized feed needs a recorded authorization; missing: " + ", ".join(missing))
        if INTERNAL_SOURCING not in src.permitted_uses:
            raise CatalogError("the feed's authorization does not permit internal sourcing")
        if src.lapsed():
            raise CatalogError(f"the feed's authorization lapsed on {src.expires_at}")
    elif src.kind in (CUSTOMER_RECORDS, SUPPLIER_QUOTE):
        if not src.authorized_by.strip():
            raise CatalogError("record who supplied these records (authorized_by)")
        if REDISTRIBUTION in src.permitted_uses and not src.authorization_ref.strip():
            raise CatalogError("redistribution needs the document that permits it (authorization_ref)")
    elif REDISTRIBUTION in src.permitted_uses:
        raise CatalogError("synthetic data is not redistributed as catalog data")


@dataclass(frozen=True)
class CatalogItem:
    supplier_ref: str
    sku: str
    title: str
    currency: str = ""
    price_tiers: Tuple[Tuple[float, int], ...] = ()     # (minimum quantity, unit price in minor units), ascending
    moq: float = 1.0
    unit: str = "each"
    pack_qty: float = 1.0
    lead_time_days: Optional[int] = None
    identifiers: Mapping[str, str] = field(default_factory=dict)   # gtin | mpn | model
    brand: str = ""
    category: str = ""
    incoterm: str = ""
    country_of_origin: str = ""
    valid_from: str = ""
    valid_until: str = ""
    document_ref: str = ""             # the price agreement, quote or export this row was read from
    supplier_name: str = ""

    def unit_price_at(self, quantity: float) -> Optional[int]:
        price = None
        for min_qty, minor in self.price_tiers:
            if quantity >= min_qty:
                price = minor
        return price


def _id_keys(identifiers: Mapping[str, str]) -> set:
    """Comparable identifier keys: every GTIN-family code in one namespace (a UPC equals its zero-padded EAN),
    part numbers ignoring case and punctuation."""
    out = set()
    for k, v in identifiers.items():
        k, v = k.lower(), str(v or "")
        if k in ("gtin", "upc", "ean") and _gtin_norm(v):
            out.add(("gtin", _gtin_norm(v)))
        elif k in ("mpn", "model") and re.sub(r"[^a-z0-9]", "", v.lower()):
            out.add((k, re.sub(r"[^a-z0-9]", "", v.lower())))
    return out


def classify(item: CatalogItem, source: CatalogSource, on: Optional[str] = None) -> Tuple[str, str]:
    """(class, reason). ``verified`` only for a price this buyer can rely on today."""
    on = on or today()
    if source.kind == SYNTHETIC:
        return SYNTHETIC, "synthetic fixture"
    if not item.price_tiers:
        return UNCONFIRMED, "no price on record"
    if item.valid_until and item.valid_until < on:
        return UNCONFIRMED, f"price validity ended {item.valid_until}"
    if source.kind == AUTHORIZED_FEED:
        return UNCONFIRMED, "feed list price, not an offer to this buyer"
    if not item.valid_until:
        return UNCONFIRMED, "no validity date on record"
    if not item.document_ref:
        return UNCONFIRMED, "no agreement or quote document on record"
    return VERIFIED, ("supplier quote" if source.kind == SUPPLIER_QUOTE else "customer price record") + \
        f" valid until {item.valid_until}"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, registered_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shares (
    source_id TEXT NOT NULL, to_tenant TEXT NOT NULL, shared_at TEXT NOT NULL, PRIMARY KEY (source_id, to_tenant));
CREATE TABLE IF NOT EXISTS suppliers (
    tenant TEXT NOT NULL, supplier_ref TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (tenant, supplier_ref));
CREATE TABLE IF NOT EXISTS items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, supplier_ref TEXT NOT NULL, sku TEXT NOT NULL,
    body TEXT NOT NULL, row_digest TEXT NOT NULL, ingested_at TEXT NOT NULL, superseded INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS items_by_source ON items (source_id, supplier_ref, sku);
CREATE TABLE IF NOT EXISTS ingests (
    ingest_id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, at TEXT NOT NULL, content_digest TEXT NOT NULL,
    accepted INTEGER NOT NULL, rejected INTEGER NOT NULL, rejects TEXT NOT NULL);
"""

_ITEM_COLUMNS = ("supplier_ref", "sku", "title", "currency", "unit_price", "moq", "unit", "pack_qty", "lead_time_days",
                 "gtin", "mpn", "brand", "category", "incoterm", "country_of_origin", "valid_from", "valid_until",
                 "document_ref", "supplier_name", "price_tiers")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def item_from_row(row: Mapping[str, Any]) -> CatalogItem:
    """One row of a price list -> a CatalogItem, or a ``CatalogError`` naming what is wrong with it."""
    g = lambda k: str(row.get(k, "") or "").strip()  # noqa: E731
    if not g("supplier_ref") or not g("sku") or not g("title"):
        raise CatalogError("supplier_ref, sku and title are required")
    tiers: List[Tuple[float, int]] = []
    currency = g("currency").upper()
    try:
        moq = float(g("moq") or 1)
        pack = float(g("pack_qty") or 1)
        lead = int(g("lead_time_days")) if g("lead_time_days") else None
    except ValueError:
        raise CatalogError("moq, pack_qty and lead_time_days must be numbers") from None
    if moq <= 0 or pack <= 0 or (lead is not None and lead < 0):
        raise CatalogError("moq and pack_qty must be positive and lead_time_days not negative")
    if g("price_tiers"):                                   # "1:12.50;100:11.00"
        for part in g("price_tiers").split(";"):
            q, _, p = part.partition(":")
            try:
                tiers.append((float(q), to_minor(p, currency)))
            except ValueError:
                raise CatalogError(f"price tier {part!r} is not quantity:price") from None
    elif g("unit_price"):
        tiers.append((moq, to_minor(g("unit_price"), currency)))
    tiers.sort()
    if tiers and tiers[0][0] > moq:
        raise CatalogError("the lowest price tier starts above the minimum order quantity")
    for k in ("valid_from", "valid_until"):
        if g(k) and not _DATE.fullmatch(g(k)):
            raise CatalogError(f"{k} must be a YYYY-MM-DD date")
    ids = {k: g(k) for k in ("gtin", "mpn") if g(k)}
    if "gtin" in ids and not re.fullmatch(r"\d{8,14}", ids["gtin"]):
        raise CatalogError("gtin must be 8 to 14 digits")
    return CatalogItem(
        supplier_ref=g("supplier_ref"), sku=g("sku"), title=g("title"), currency=currency, price_tiers=tuple(tiers),
        moq=moq, unit=g("unit") or "each", pack_qty=pack, lead_time_days=lead, identifiers=ids, brand=g("brand"),
        category=g("category"), incoterm=g("incoterm").upper(), country_of_origin=g("country_of_origin").upper(),
        valid_from=g("valid_from"), valid_until=g("valid_until"), document_ref=g("document_ref"),
        supplier_name=g("supplier_name"))


def _item_from_body(body: str) -> CatalogItem:
    d = json.loads(body)
    d["price_tiers"] = tuple((float(q), int(p)) for q, p in d.get("price_tiers") or ())
    return CatalogItem(**d)


class Catalog:
    """Tenant-scoped catalog store (SQLite; ``:memory:`` by default)."""

    def __init__(self, path: str = ":memory:") -> None:
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)

    # ── sources and permissions ─────────────────────────────────────────────────────────────────────
    def register_source(self, src: CatalogSource) -> CatalogSource:
        _check_source(src)
        with self._lock:
            row = self._db.execute("SELECT tenant FROM sources WHERE source_id = ?", (src.source_id,)).fetchone()
            if row and row["tenant"] != src.tenant:
                raise CatalogError("that source id belongs to another tenant")
            self._db.execute("INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?)",
                             (src.source_id, src.tenant, src.kind, json.dumps(asdict(src)), _now()))
        return src

    def source(self, source_id: str) -> Optional[CatalogSource]:
        with self._lock:
            row = self._db.execute("SELECT body FROM sources WHERE source_id = ?", (source_id,)).fetchone()
        if not row:
            return None
        d = json.loads(row["body"])
        d["permitted_uses"] = tuple(d["permitted_uses"])
        return CatalogSource(**d)

    def _owned(self, tenant: str, source_id: str) -> CatalogSource:
        src = self.source(source_id)
        if src is None or src.tenant != tenant:
            raise CatalogError("no such catalog source for this tenant")       # same answer either way
        return src

    def share(self, tenant: str, source_id: str, to_tenant: str) -> None:
        """Let another tenant search this source. Only a source whose authorization permits redistribution."""
        src = self._owned(tenant, source_id)
        if REDISTRIBUTION not in src.permitted_uses:
            raise CatalogError("this source's authorization does not permit redistribution")
        if src.lapsed():
            raise CatalogError(f"this source's authorization lapsed on {src.expires_at}")
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO shares VALUES (?,?,?)", (source_id, to_tenant, _now()))

    def visible_sources(self, tenant: str, on: Optional[str] = None) -> List[CatalogSource]:
        """Sources this tenant may search today: its own, plus ones shared with it. Lapsed feeds drop out."""
        with self._lock:
            rows = self._db.execute(
                "SELECT source_id FROM sources WHERE tenant = ? UNION SELECT source_id FROM shares WHERE to_tenant = ?",
                (tenant, tenant)).fetchall()
        out = []
        for r in rows:
            src = self.source(r["source_id"])
            if src and not src.lapsed(on) and (src.tenant == tenant or REDISTRIBUTION in src.permitted_uses):
                out.append(src)
        return out

    # ── suppliers (always the tenant's own records) ─────────────────────────────────────────────────
    def upsert_supplier(self, tenant: str, supplier_ref: str, *, name: str, email: str = "", country: str = "",
                        external_ids: Optional[Mapping[str, str]] = None, identity: Optional[dict] = None) -> dict:
        if not tenant or not supplier_ref or not name:
            raise CatalogError("a supplier needs a tenant, a reference and a name")
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise CatalogError(f"supplier contact {email!r} is not an email address")
        prior = self.supplier(tenant, supplier_ref) or {}
        body = {"supplier_ref": supplier_ref, "name": name, "email": email, "country": country.upper(),
                "external_ids": dict(external_ids or {}), "identity": identity or prior.get("identity") or
                {"status": "not_checked"}}
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO suppliers VALUES (?,?,?)", (tenant, supplier_ref, json.dumps(body)))
        return body

    def supplier(self, tenant: str, supplier_ref: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute("SELECT body FROM suppliers WHERE tenant = ? AND supplier_ref = ?",
                                   (tenant, supplier_ref)).fetchone()
        return json.loads(row["body"]) if row else None

    def suppliers(self, tenant: str) -> List[dict]:
        with self._lock:
            rows = self._db.execute("SELECT body FROM suppliers WHERE tenant = ? ORDER BY supplier_ref", (tenant,)).fetchall()
        return [json.loads(r["body"]) for r in rows]

    # ── ingestion ───────────────────────────────────────────────────────────────────────────────────
    def ingest(self, tenant: str, source_id: str, rows: Iterable[Mapping[str, Any]], *, content_digest: str = "") -> dict:
        """Add rows to a source the tenant owns. A row that fails validation is rejected with its reason and
        the rest are kept. A newer row for the same supplier and SKU supersedes the older one; the older
        row stays in the store."""
        src = self._owned(tenant, source_id)
        if src.lapsed():
            raise CatalogError(f"this source's authorization lapsed on {src.expires_at}")
        rows = list(rows)
        accepted, rejects = 0, []
        with self._lock:
            for n, row in enumerate(rows, 1):
                try:
                    item = item_from_row(row)
                except CatalogError as e:
                    rejects.append({"row": n, "reason": str(e)})
                    continue
                body = asdict(item)
                self._db.execute("UPDATE items SET superseded = 1 WHERE source_id = ? AND supplier_ref = ? AND sku = ?",
                                 (source_id, item.supplier_ref, item.sku))
                self._db.execute("INSERT INTO items (source_id, supplier_ref, sku, body, row_digest, ingested_at)"
                                 " VALUES (?,?,?,?,?,?)", (source_id, item.supplier_ref, item.sku,
                                                           json.dumps(body, default=list), _digest(body), _now()))
                accepted += 1
            self._db.execute("INSERT INTO ingests (source_id, at, content_digest, accepted, rejected, rejects)"
                             " VALUES (?,?,?,?,?,?)", (source_id, _now(), content_digest or _digest(rows), accepted,
                                                       len(rejects), json.dumps(rejects)))
        return {"source_id": source_id, "accepted": accepted, "rejected": len(rejects), "rejects": rejects}

    def ingest_csv(self, tenant: str, source_id: str, text: str) -> dict:
        reader = csv.DictReader(io.StringIO(text))
        unknown = [c for c in (reader.fieldnames or []) if c not in _ITEM_COLUMNS]
        if unknown:
            raise CatalogError("unknown column(s): " + ", ".join(unknown))
        return self.ingest(tenant, source_id, list(reader), content_digest=hashlib.sha256(text.encode()).hexdigest())

    # ── search and comparison ───────────────────────────────────────────────────────────────────────
    def _items(self, tenant: str, on: Optional[str], include_synthetic: bool) -> List[dict]:
        out = []
        for src in self.visible_sources(tenant, on):
            if src.kind == SYNTHETIC and not include_synthetic:
                continue
            with self._lock:
                rows = self._db.execute("SELECT * FROM items WHERE source_id = ? AND superseded = 0", (src.source_id,)).fetchall()
            for r in rows:
                item = _item_from_body(r["body"])
                cls, why = classify(item, src, on)
                out.append({"item": item, "source": src, "class": cls, "class_reason": why,
                            "row_digest": r["row_digest"], "ingested_at": r["ingested_at"]})
        return out

    def search(self, tenant: str, *, text: str = "", identifiers: Optional[Mapping[str, str]] = None,
               include_synthetic: bool = False, on: Optional[str] = None) -> List[dict]:
        """Items this tenant may see that match an identifier (GTIN/MPN, like for like) or every word of ``text``."""
        words = [w for w in re.sub(r"[^a-z0-9 ]", " ", text.lower()).split() if w]
        want = _id_keys(identifiers or {})
        hits = []
        for rec in self._items(tenant, on, include_synthetic):
            item: CatalogItem = rec["item"]
            match = "identifier" if want & _id_keys(item.identifiers) else ""
            if not match and words:
                hay = " ".join((item.title, item.brand, item.sku, item.category, " ".join(item.identifiers.values()))).lower()
                if all(w in hay for w in words):
                    match = "text"
            if match:
                hits.append({**self._public(rec), "matched_on": match})
        return sorted(hits, key=lambda h: (h["class"] != VERIFIED, h["supplier_ref"], h["sku"]))

    def _public(self, rec: dict) -> dict:
        item, src = rec["item"], rec["source"]
        return {"supplier_ref": item.supplier_ref, "supplier_name": item.supplier_name, "sku": item.sku,
                "title": item.title, "brand": item.brand, "identifiers": dict(item.identifiers), "unit": item.unit,
                "pack_qty": item.pack_qty, "moq": item.moq, "currency": item.currency,
                "price_tiers": [list(t) for t in item.price_tiers], "lead_time_days": item.lead_time_days,
                "incoterm": item.incoterm, "valid_until": item.valid_until, "document_ref": item.document_ref,
                "class": rec["class"], "class_reason": rec["class_reason"],
                "source": {"source_id": src.source_id, "kind": src.kind, "name": src.name, "licence": src.licence,
                           "attribution": src.attribution, "owner": src.tenant},
                "row_digest": rec["row_digest"], "ingested_at": rec["ingested_at"]}

    def compare(self, tenant: str, *, quantity: float, text: str = "", identifiers: Optional[Mapping[str, str]] = None,
                need_by_days: Optional[int] = None, include_synthetic: bool = False, on: Optional[str] = None) -> dict:
        """Offers for one requirement at one quantity, with a recommendation the evidence supports.

        Offers are compared within a currency only. An offer below its MOQ, or one that cannot arrive in
        time, is listed with the reason and left out of the ranking. ``buy`` is recommended only from a
        verified price; when the best price on record is unconfirmed the recommendation is ``request_quote``.
        A text match is a candidate, not a like-for-like product, and is marked so."""
        if quantity <= 0:
            raise CatalogError("quantity must be positive")
        offers, excluded = [], []
        for hit in self.search(tenant, text=text, identifiers=identifiers, include_synthetic=include_synthetic, on=on):
            tiers = [(q, p) for q, p in hit["price_tiers"]]
            unit_price = None
            for q, p in tiers:
                if quantity >= q:
                    unit_price = p
            reason = ""
            if hit["class"] == SYNTHETIC:
                reason = "synthetic fixture"
            elif unit_price is None and not tiers:
                reason = "no price on record"
            elif quantity < hit["moq"]:
                reason = f"below the minimum order quantity of {hit['moq']:g}"
            elif need_by_days is not None and hit["lead_time_days"] is None:
                reason = "no lead time on record"
            elif need_by_days is not None and hit["lead_time_days"] > need_by_days:
                reason = f"lead time {hit['lead_time_days']} days exceeds {need_by_days}"
            if reason:
                excluded.append({**hit, "excluded_reason": reason})
                continue
            offers.append({**hit, "unit_price_minor": unit_price, "total_minor": int(round(unit_price * quantity)),
                           "like_for_like": hit["matched_on"] == "identifier"})
        by_currency: Dict[str, List[dict]] = {}
        for o in offers:
            by_currency.setdefault(o["currency"], []).append(o)
        for group in by_currency.values():
            group.sort(key=lambda o: (o["total_minor"], o["lead_time_days"] if o["lead_time_days"] is not None else 10 ** 6))
        rec: Dict[str, Any] = {"action": "no_offer", "reason": "no usable offer on record"}
        if len(by_currency) > 1:
            rec = {"action": "review", "reason": "offers are in different currencies (" + ", ".join(sorted(by_currency)) +
                   "); no exchange rate is assumed"}
        elif by_currency:
            ranked = next(iter(by_currency.values()))
            best = ranked[0]
            best_verified = next((o for o in ranked if o["class"] == VERIFIED and o["like_for_like"]), None)
            if best["class"] == VERIFIED and best["like_for_like"]:
                rec = {"action": "buy", "supplier_ref": best["supplier_ref"], "sku": best["sku"],
                       "reason": f"lowest verified like-for-like price: {best['class_reason']}"}
            else:
                why = best["class_reason"] if best["class"] != VERIFIED else "matched on text, not on an identifier"
                rec = {"action": "request_quote", "supplier_refs": sorted({o["supplier_ref"] for o in ranked}),
                       "reason": f"the lowest price on record is not one to buy on ({why})"}
                if best_verified:
                    rec["verified_alternative"] = {"supplier_ref": best_verified["supplier_ref"], "sku": best_verified["sku"],
                                                   "total_minor": best_verified["total_minor"]}
        return {"quantity": quantity, "offers": by_currency, "excluded": excluded, "recommendation": rec,
                "attributions": sorted({o["source"]["attribution"] for g in by_currency.values() for o in g
                                        if o["source"]["attribution"]})}

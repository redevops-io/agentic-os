"""Reads ERPNext and projects its documents into :class:`EconomicEvent`. Read-only; nothing is written.

The reads go through ``read_list`` (raises) when the client has it, so an unreachable or refused doctype becomes a recorded gap, never
an empty list that would make a customer look profitable. The client needs only ``read_list`` or ``get_list``.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence

from .contracts import Approval, Basis, EconomicEvent, Extraction, Kind, money

# the activity types this reads, and what each means. Any other activity type is counted as a gap, never guessed.
ACTIVITY_KINDS = {"Support": Kind.SUPPORT_COST, "Rework": Kind.REWORK_COST, "Cancellation Handling": Kind.CANCELLATION_HANDLING}
_APPROVAL = re.compile(r"aos-approval\s+id=(?P<id>\S+)\s+approver=(?P<who>\S+)(?:\s+action=(?P<action>\S+))?")

SI_FIELDS = ["name", "customer", "posting_date", "docstatus", "is_return", "return_against", "amended_from", "net_total", "currency"]
SI_ITEM_FIELDS = ["parent", "item_code", "qty", "price_list_rate", "rate", "net_amount", "incoming_rate"]
PI_FIELDS = ["name", "supplier", "posting_date", "docstatus", "is_return", "return_against", "net_total", "currency"]
TS_DETAIL_FIELDS = ["parent", "activity_type", "from_time", "hours", "base_costing_amount", "project"]


def _read(client: Any, ex: Extraction, doctype: str, fields: Sequence[str], filters: Optional[list] = None, *, quiet: bool = False) -> Optional[List[dict]]:
    fn = getattr(client, "read_list", None) or client.get_list
    try:
        rows = list(fn(doctype, list(fields), filters, 0))
    except Exception as e:  # noqa: BLE001
        if quiet:
            return None
        ex.gap(f"erpnext:{doctype}", f"could not be read ({type(e).__name__}: {str(e)[:120]})", "every event derived from it is missing")
        return None
    # ERPNext leaves out a requested field it will not give, without an error. A column that is absent from the answer is a gap, not a zero.
    missing = [f for f in fields if rows and f not in rows[0]]
    if missing:
        if quiet:
            return None
        ex.gap(f"erpnext:{doctype}", f"the application did not return field(s) {missing}", "amounts that need them are missing, not zero")
    return rows


def _optional_fields(client: Any, ex: Extraction, doctype: str, base: Sequence[str], extra: Sequence[str], why: str) -> List[dict]:
    """Read with the optional custom fields; when the site has not got them, read without and record what that costs."""
    rows = _read(client, ex, doctype, [*base, *extra], quiet=True)
    if rows is not None:
        return rows
    rows2 = _read(client, ex, doctype, list(base))
    if rows2 is not None and extra:
        # custom fields are an optional extension a site may not have: said, but the read is not incomplete without them
        ex.note(f"erpnext:{doctype}", f"fields {list(extra)} are not available on this site", why)
    return rows2 or []


def extract(client: Any, *, customer_of: Optional[Callable[[str], Optional[str]]] = None, since: str = "", until: str = "") -> Extraction:
    """Every economic event ERPNext can show for the period. ``customer_of(name)`` resolves an ERPNext customer to a canonical entity id
    (see ``agentic_os.integration.contracts.CanonicalEntity``); a name it cannot resolve is kept as ``unresolved:<name>``, never merged."""
    ex = Extraction()
    resolve = customer_of or (lambda n: n)

    def cust(name: str) -> str:
        got = resolve(name)
        return got if got else f"unresolved:{name}"

    date_filter = [["posting_date", "between", [since or "1900-01-01", until or "2999-12-31"]]]
    # ── sales ─────────────────────────────────────────────────────────────────────────────────────
    invoices = _read(client, ex, "Sales Invoice", SI_FIELDS, date_filter)
    items = _read(client, ex, "Sales Invoice Item", SI_ITEM_FIELDS, None) if invoices is not None else None
    comments = _read(client, ex, "Comment", ["reference_name", "content"],
                     [["reference_doctype", "=", "Sales Invoice"], ["comment_type", "=", "Comment"]])
    if comments is None:
        ex.gap("erpnext:Comment", "approval references could not be read", "no discount can be tied to an approver")
    approvals: Dict[str, Approval] = {}
    for c in comments or []:
        m = _APPROVAL.search(str(c.get("content") or ""))
        if m:
            approvals[str(c.get("reference_name"))] = Approval(m["id"], m["who"], m["action"] or "")
    by_parent: Dict[str, List[dict]] = {}
    for it in items or []:
        by_parent.setdefault(str(it.get("parent")), []).append(it)
    replaced = {str(d.get("amended_from")): str(d["name"]) for d in invoices or [] if d.get("amended_from")}
    ex.counts["Sales Invoice"] = len(invoices or [])
    for d in invoices or []:
        name, cur, day = str(d["name"]), str(d.get("currency") or ""), str(d.get("posting_date") or "")
        subj = {"customer": cust(str(d.get("customer") or "")), "customer_source": str(d.get("customer") or ""), "invoice": name}
        src = (f"erpnext:Sales Invoice/{name}",)
        net = money(d.get("net_total"))
        if int(d.get("docstatus") or 0) == 2:                      # cancelled: its posting was reversed; the loss is what it forwent
            again = replaced.get(name)
            ex.events.append(EconomicEvent(
                f"{name}:cancellation", Kind.CANCELLATION, Basis.ESTIMATED, Decimal("0.00") if again else -net, cur, day, subj, src,
                supersedes=None, note=(f"replaced by {again}" if again else "not replaced: the revenue it would have brought is estimated")))
            continue
        if int(d.get("docstatus") or 0) != 1:
            continue
        rows = by_parent.get(name, [])
        listed = sum((money(r.get("price_list_rate")) * money(r.get("qty")) for r in rows), Decimal("0.00"))
        if not rows:
            ex.gap(f"erpnext:Sales Invoice/{name}", "no item rows were read", "list price, discount and cost of goods are not computed for this invoice")
            listed = net
        ret = bool(d.get("is_return"))
        sup = f"erpnext:Sales Invoice/{d.get('return_against')}" if ret and d.get("return_against") else None
        ex.events.append(EconomicEvent(f"{name}:sale", Kind.RETURN if ret else Kind.SALE, Basis.POSTED, listed, cur, day, subj, src, supersedes=sup))
        if listed != net:
            ex.events.append(EconomicEvent(f"{name}:discount", Kind.DISCOUNT, Basis.POSTED, net - listed, cur, day, subj, src,
                                           approval=approvals.get(name)))
        cost = sum((money(r.get("incoming_rate")) * money(r.get("qty")) for r in rows), Decimal("0.00"))
        if cost:
            ex.events.append(EconomicEvent(f"{name}:cogs", Kind.COST_OF_GOODS, Basis.POSTED, -cost, cur, day, subj, src))
    # ── purchases ─────────────────────────────────────────────────────────────────────────────────
    bills = _read(client, ex, "Purchase Invoice", PI_FIELDS, date_filter)
    ex.counts["Purchase Invoice"] = len(bills or [])
    for d in bills or []:
        if int(d.get("docstatus") or 0) != 1:
            continue
        name, ret = str(d["name"]), bool(d.get("is_return"))
        ex.events.append(EconomicEvent(
            f"{name}:supplier", Kind.SUPPLIER_RETURN if ret else Kind.SUPPLIER_COST, Basis.POSTED, -money(d.get("net_total")), str(d.get("currency") or ""),
            str(d.get("posting_date") or ""), {"supplier": str(d.get("supplier") or ""), "invoice": name}, (f"erpnext:Purchase Invoice/{name}",),
            supersedes=(f"erpnext:Purchase Invoice/{d.get('return_against')}" if ret and d.get("return_against") else None)))
    # ── time: support, rework, cancellation handling ──────────────────────────────────────────────
    sheets = _read(client, ex, "Timesheet", ["name", "docstatus", "start_date"], None)
    submitted = {str(s["name"]) for s in sheets or [] if int(s.get("docstatus") or 0) == 1}
    projects = {str(p["name"]): str(p.get("customer") or "") for p in (_read(client, ex, "Project", ["name", "customer"], None) or [])}
    logs = _optional_fields(client, ex, "Timesheet Detail", TS_DETAIL_FIELDS, ["custom_supplier", "custom_reference"],
                            "rework cannot be attributed to a supplier and a cancellation's handling time cannot be tied to its invoice")
    ex.counts["Timesheet Detail"] = len(logs)
    unmapped: Dict[str, int] = {}
    for r in logs:
        if str(r.get("parent")) not in submitted:
            continue
        kind = ACTIVITY_KINDS.get(str(r.get("activity_type") or ""))
        if kind is None:
            unmapped[str(r.get("activity_type") or "")] = unmapped.get(str(r.get("activity_type") or ""), 0) + 1
            continue
        day = str(r.get("from_time") or "")[:10]
        if not ((not since or day >= since) and (not until or day <= until)):
            continue
        subj: Dict[str, str] = {"project": str(r.get("project") or "")}
        c = projects.get(subj["project"], "")
        if c:
            subj.update(customer=cust(c), customer_source=c)
        if r.get("custom_supplier"):
            subj["supplier"] = str(r["custom_supplier"])
        if r.get("custom_reference"):
            subj["invoice"] = str(r["custom_reference"])
        ex.events.append(EconomicEvent(f"{r.get('parent')}:{r.get('activity_type')}:{day}", kind, Basis.ESTIMATED, -money(r.get("base_costing_amount")), "",
                                       day, subj, (f"erpnext:Timesheet/{r.get('parent')}",),
                                       note=f"{r.get('hours')} h at the activity type's costing rate"))
    dominant = max({e.currency for e in ex.events if e.currency} or {""}, key=lambda c: sum(1 for e in ex.events if e.currency == c))
    ex.events = [e if e.currency else EconomicEvent(**{**e.__dict__, "currency": dominant, "note": (e.note + "; currency taken from the invoices").strip("; ")})
                 for e in ex.events]
    for a, n in sorted(unmapped.items()):
        ex.gap("erpnext:Timesheet Detail", f"{n} time log(s) of activity type {a!r} are not mapped to a cost kind", "their cost is not in any answer")
    return ex

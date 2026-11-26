"""An independent check of the projection against the general ledger.

The events come from document headers and item rows. This reads the LEDGER (GL Entry) instead, by a different path, and compares per voucher:

* a sales invoice's posted revenue (sale + discount + return events) against the credits to income accounts for that voucher;
* the cost of goods against the debits to cost-of-goods accounts for that voucher;
* a purchase invoice against what it credited to payable accounts.

A voucher whose ledger lines cannot be read, or whose taxes make the comparison ambiguous, is reported as ``unverifiable``, never as matching.
Estimated events (hours at a costing rate) have no ledger side and are checked against the timesheet's own total instead.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Sequence

from .contracts import Basis, EconomicEvent, Extraction, Kind, money
from .erpnext_events import _read

TOLERANCE = Decimal("0.01")


def ledger_check(client: Any, events: Sequence[EconomicEvent]) -> Dict[str, Any]:
    ex = Extraction()
    accounts = {str(a["name"]): a for a in (_read(client, ex, "Account", ["name", "root_type", "account_type"], None) or [])}
    gl = _read(client, ex, "GL Entry", ["voucher_type", "voucher_no", "account", "debit", "credit", "is_cancelled"], [["is_cancelled", "=", 0]])
    if gl is None or not accounts:
        return {"checked": 0, "matched": 0, "mismatched": [], "unverifiable": ["the ledger could not be read"], "gaps": ex.gaps}
    by_voucher: Dict[str, Dict[str, Decimal]] = {}
    for g in gl:
        a = accounts.get(str(g.get("account")), {})
        v = by_voucher.setdefault(str(g.get("voucher_no")), {"income": Decimal("0"), "cogs": Decimal("0"), "payable": Decimal("0"), "tax": Decimal("0")})
        d, c = money(g.get("debit")), money(g.get("credit"))
        if a.get("root_type") == "Income":
            v["income"] += c - d
        if a.get("account_type") == "Cost of Goods Sold":
            v["cogs"] += d - c
        if a.get("account_type") == "Payable":
            v["payable"] += c - d
        if a.get("account_type") == "Tax":
            v["tax"] += c - d
    mine: Dict[str, Dict[str, Decimal]] = {}
    for e in events:
        if e.basis != Basis.POSTED:
            continue
        voucher = e.id.split(":")[0]
        m = mine.setdefault(voucher, {"revenue": Decimal("0"), "cogs": Decimal("0"), "payable": Decimal("0")})
        if e.kind in (Kind.SALE, Kind.RETURN, Kind.DISCOUNT):
            m["revenue"] += e.amount
        elif e.kind == Kind.COST_OF_GOODS:
            m["cogs"] += -e.amount
        elif e.kind in (Kind.SUPPLIER_COST, Kind.SUPPLIER_RETURN):
            m["payable"] += -e.amount
    matched, mismatched, unverifiable = 0, [], []
    for voucher, m in sorted(mine.items()):
        ledger = by_voucher.get(voucher)
        if ledger is None:
            unverifiable.append(f"{voucher}: no ledger lines")
            continue
        if ledger["tax"] != 0:
            unverifiable.append(f"{voucher}: taxes are posted, so a net comparison is ambiguous")
            continue
        pairs = [("revenue", m["revenue"], ledger["income"])] if m["revenue"] else []
        if m["cogs"]:
            pairs.append(("cost of goods", m["cogs"], ledger["cogs"]))
        if m["payable"]:
            pairs.append(("payable", m["payable"], ledger["payable"]))
        bad = [f"{what}: events {mine_} vs ledger {led}" for what, mine_, led in pairs if abs(mine_ - led) > TOLERANCE]
        if bad:
            mismatched.append({"voucher": voucher, "differences": bad})
        else:
            matched += 1
    # estimated time costs against each timesheet's own total
    sheets = _read(client, ex, "Timesheet", ["name", "total_costing_amount", "docstatus"], [["docstatus", "=", 1]]) or []
    est: Dict[str, Decimal] = {}
    for e in events:
        if e.basis == Basis.ESTIMATED and e.kind in (Kind.SUPPORT_COST, Kind.REWORK_COST, Kind.CANCELLATION_HANDLING):
            sheet = e.id.split(":")[0]
            est[sheet] = est.get(sheet, Decimal("0")) + -e.amount
    for s in sheets:
        name = str(s["name"])
        if name in est and abs(est[name] - money(s.get("total_costing_amount"))) > TOLERANCE:
            mismatched.append({"voucher": name, "differences": [f"time cost: events {est[name]} vs timesheet total {money(s.get('total_costing_amount'))}"]})
        elif name in est:
            matched += 1
    return {"checked": matched + len(mismatched), "matched": matched, "mismatched": mismatched, "unverifiable": unverifiable, "gaps": ex.gaps}

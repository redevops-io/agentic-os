#!/usr/bin/env python3
"""Idempotent in-container seeder for the Summit Roofing Co. books on ERPNext (v15/16).

Run INSIDE the running ERPNext backend container via `bench --site frontend console`:

    sudo docker cp seed_erpnext.py erpnext-backend-1:/tmp/seed_erpnext.py
    sudo docker exec -i erpnext-backend-1 bench --site frontend console <<'EOF'
    exec(open("/tmp/seed_erpnext.py").read())
    EOF

seed.py (the host wrapper) does exactly that and captures the SEED_OK line.

What it creates (idempotently — safe to re-run; nothing duplicates):
  * Fiscal Year 2026 (Jan–Dec) if missing.
  * Company "Summit Roofing Co." (builds the standard Chart of Accounts).
  * Modes of payment + a company Bank + Bank Account (Operating).
  * 5 Customers, 4 Suppliers, an Item.
  * ~7 Sales Invoices (some paid -> A/R), revenue MTD ~$148k.
  * ~4 Purchase Invoices (materials/payroll suppliers) -> A/P, materials ~$52k.
  * Payment Entries against a few invoices (cash in).
  * A couple of Journal Entries (payroll accrual ~$61k, depreciation).
  * ~7 unreconciled, uncategorized Bank Transactions (the "uncategorized queue").

Frappe facts that matter (discovered on this install):
  * Bare site: no Fiscal Year / Company / Chart of Accounts exist until we make them.
  * Company creation auto-installs the standard CoA; abbr = "SRC".
  * Sales/Purchase Invoices need update_stock=0 (services) and a valid income/expense
    account; we let ERPNext default them from the company.
  * Bank Transaction.deposit/withdrawal + unallocated_amount drive the "uncategorized"
    + reconciliation queues; we leave bank_party_* and a payment link unset so they
    show as uncategorized.
"""
import frappe
import json
import datetime
import os

# When run as a plain script (not via `bench console`), bootstrap a frappe request
# context. `bench console` / the host wrapper already have one; guard so both work.
if not getattr(frappe.local, "site", None):
    _site = os.environ.get("FRAPPE_SITE", "frontend")
    frappe.init(site=_site)
    frappe.connect()
    frappe.set_user("Administrator")

from frappe.utils import nowdate, getdate, add_days

COMPANY = "Summit Roofing Co."
ABBR = "SRC"
CURRENCY = "USD"
TODAY = getdate(nowdate())
# Anchor the books to "this month" so MTD numbers are real regardless of run date.
MONTH_START = TODAY.replace(day=1)


_LOGF = open("/tmp/seed_result.txt", "a")


def log(msg):
    line = "SEED> " + str(msg)
    print(line)
    _LOGF.write(line + "\n")
    _LOGF.flush()


def ensure_fiscal_year(year):
    name = str(year)
    if not frappe.db.exists("Fiscal Year", name):
        fy = frappe.get_doc({
            "doctype": "Fiscal Year",
            "year": name,
            "year_start_date": datetime.date(year, 1, 1),
            "year_end_date": datetime.date(year, 12, 31),
        })
        fy.insert(ignore_permissions=True)
        log(f"created Fiscal Year {name}")
    return name


def ensure_prereq_fixtures():
    """Standard fixtures a Company's post-save hooks expect. On a bare site (setup
    wizard never completed) some are missing, so create the ones Company needs."""
    if frappe.db.exists("DocType", "Warehouse Type"):
        for wt in ("Transit",):
            if not frappe.db.exists("Warehouse Type", wt):
                frappe.get_doc({"doctype": "Warehouse Type", "name": wt}).insert(ignore_permissions=True)
    # UOMs
    for u in ("Nos", "Unit"):
        if not frappe.db.exists("UOM", u):
            frappe.get_doc({"doctype": "UOM", "uom_name": u}).insert(ignore_permissions=True)
    # tree roots (is_group=1) + leaves used by customers/suppliers/items
    def _tree(dt, name, is_group=1, parent_field=None):
        if frappe.db.exists(dt, name):
            return
        doc = {"doctype": dt, "is_group": is_group}
        title_field = {"Item Group": "item_group_name", "Customer Group": "customer_group_name",
                       "Supplier Group": "supplier_group_name", "Territory": "territory_name"}[dt]
        doc[title_field] = name
        frappe.get_doc(doc).insert(ignore_permissions=True)

    for name in ("All Item Groups",):
        _tree("Item Group", name, is_group=1)
    if not frappe.db.exists("Item Group", "Services"):
        frappe.get_doc({"doctype": "Item Group", "item_group_name": "Services",
                        "is_group": 0, "parent_item_group": "All Item Groups"}).insert(ignore_permissions=True)
    for name in ("All Customer Groups",):
        _tree("Customer Group", name, is_group=1)
    if not frappe.db.exists("Customer Group", "Commercial"):
        frappe.get_doc({"doctype": "Customer Group", "customer_group_name": "Commercial",
                        "is_group": 0, "parent_customer_group": "All Customer Groups"}).insert(ignore_permissions=True)
    for name in ("All Supplier Groups",):
        _tree("Supplier Group", name, is_group=1)
    for name in ("All Territories",):
        _tree("Territory", name, is_group=1)
    # Price lists (selling + buying) — invoices need a price list + currency
    for pl, selling, buying in (("Standard Selling", 1, 0), ("Standard Buying", 0, 1)):
        if not frappe.db.exists("Price List", pl):
            frappe.get_doc({"doctype": "Price List", "price_list_name": pl,
                            "currency": CURRENCY, "enabled": 1,
                            "selling": selling, "buying": buying}).insert(ignore_permissions=True)
    frappe.db.commit()


def ensure_company():
    ensure_prereq_fixtures()
    if not frappe.db.exists("Company", COMPANY):
        c = frappe.get_doc({
            "doctype": "Company",
            "company_name": COMPANY,
            "abbr": ABBR,
            "default_currency": CURRENCY,
            "country": "United States",
            "create_chart_of_accounts_based_on": "Standard Template",
            "default_holiday_list": None,
            "chart_of_accounts": "Standard",
            "enable_perpetual_inventory": 0,
        })
        # valuation_method is mandatory on Company in v15/16
        c.update({"default_inventory_account": None})
        c.set("default_currency", CURRENCY)
        if c.meta.has_field("valuation_method"):
            c.valuation_method = "FIFO"
        c.insert(ignore_permissions=True)
        frappe.db.commit()
        log(f"created Company {COMPANY} (CoA built)")
    # make it the global default so REST callers don't need to pass company everywhere
    frappe.db.set_default("company", COMPANY)
    frappe.db.set_value("Global Defaults", "Global Defaults", "default_company", COMPANY)
    return COMPANY


def ensure_mode(name, mtype="Bank"):
    if not frappe.db.exists("Mode of Payment", name):
        frappe.get_doc({"doctype": "Mode of Payment", "mode_of_payment": name,
                        "type": mtype}).insert(ignore_permissions=True)


def acc(fragment, root_type=None):
    """Resolve a CoA leaf account by name fragment (optionally constrained to a
    root_type so e.g. 'Sales' resolves to the Income account, not 'Commission on
    Sales' which is an Expense in the standard CoA)."""
    filters = {"company": COMPANY, "account_name": ["like", f"%{fragment}%"], "is_group": 0}
    if root_type:
        filters["root_type"] = root_type
    rows = frappe.get_all("Account", filters=filters, fields=["name"], limit=1)
    return rows[0].name if rows else None


def income_account():
    # Standard CoA: "Sales" (root_type Income). Fall back to any Income leaf.
    return (acc("Sales", root_type="Income")
            or acc("Service", root_type="Income")
            or acc("", root_type="Income"))


def ensure_bank_account():
    bank_name = "Summit Operating Bank"
    if not frappe.db.exists("Bank", bank_name):
        frappe.get_doc({"doctype": "Bank", "bank_name": bank_name}).insert(ignore_permissions=True)
    # a balance-sheet bank GL account
    bank_gl = acc("Bank Accounts") or acc("Bank Account") or acc("Cash")
    ba_name = f"Operating - {COMPANY}"
    existing = frappe.get_all("Bank Account",
                              filters={"account_name": "Operating", "company": COMPANY}, limit=1)
    if existing:
        return existing[0].name, bank_gl
    ba = frappe.get_doc({
        "doctype": "Bank Account",
        "account_name": "Operating",
        "bank": bank_name,
        "company": COMPANY,
        "account": bank_gl,
        "is_company_account": 1,
    })
    ba.insert(ignore_permissions=True)
    log(f"created Bank Account {ba.name}")
    return ba.name, bank_gl


def ensure_customer(name):
    if not frappe.db.exists("Customer", name):
        frappe.get_doc({"doctype": "Customer", "customer_name": name,
                        "customer_type": "Company",
                        "customer_group": "Commercial" if frappe.db.exists("Customer Group", "Commercial") else "All Customer Groups",
                        "territory": "All Territories"}).insert(ignore_permissions=True)
    return name


def ensure_supplier(name, group=None):
    if not frappe.db.exists("Supplier", name):
        sg = "All Supplier Groups"
        frappe.get_doc({"doctype": "Supplier", "supplier_name": name,
                        "supplier_type": "Company",
                        "supplier_group": sg}).insert(ignore_permissions=True)
    return name


def ensure_item():
    code = "ROOF-SERVICE"
    if not frappe.db.exists("Item", code):
        ig = "Services" if frappe.db.exists("Item Group", "Services") else "All Item Groups"
        frappe.get_doc({
            "doctype": "Item", "item_code": code, "item_name": "Roofing Service",
            "item_group": ig, "is_stock_item": 0, "is_sales_item": 1, "is_purchase_item": 1,
            "stock_uom": "Unit" if frappe.db.exists("UOM", "Unit") else "Nos",
        }).insert(ignore_permissions=True)
    return code


# A stable tag in the remarks lets us find + skip our own docs on re-run (idempotency).
TAG = "SEED:SummitBooks"


# The text field that carries our TAG differs per doctype.
_TAG_FIELD = {
    "Sales Invoice": "remarks",
    "Purchase Invoice": "remarks",
    "Journal Entry": "user_remark",
    "Bank Transaction": "description",
}


def find_tagged(doctype):
    field = _TAG_FIELD.get(doctype, "remarks")
    return set(d.name for d in frappe.get_all(
        doctype, filters={field: ["like", f"%{TAG}%"]}, fields=["name"]))


def seed_sales_invoices(item):
    # (customer, total, days_ago, paid?)  -> revenue MTD ~ $148.2k
    plan = [
        ("Henderson Commercial Properties", 42200, 8, True),
        ("Maple Street HOA", 18600, 6, True),
        ("Cedar Ridge Apartments", 28400, 5, False),   # outstanding A/R
        ("Lakeview Office Park", 21800, 4, True),
        ("Northgate Retail LLC", 14200, 3, False),      # outstanding A/R
        ("Henderson Commercial Properties", 12900, 2, False),  # outstanding A/R
        ("Maple Street HOA", 10100, 1, True),
    ]
    existing = find_tagged("Sales Invoice")
    made = 0
    for i, (cust, total, days, paid) in enumerate(plan, 1):
        marker = f"{TAG} SI-{i}"
        if any(marker in (frappe.db.get_value("Sales Invoice", n, "remarks") or "") for n in existing):
            continue
        ensure_customer(cust)
        posting = max(MONTH_START, add_days(TODAY, -days))
        si = frappe.get_doc({
            "doctype": "Sales Invoice", "company": COMPANY, "customer": cust,
            "posting_date": posting, "due_date": add_days(posting, 15),
            "currency": CURRENCY, "remarks": marker,
            "selling_price_list": "Standard Selling",
            "price_list_currency": CURRENCY, "plc_conversion_rate": 1.0,
            "conversion_rate": 1.0,
            "items": [{"item_code": item, "qty": 1, "rate": total, "price_list_rate": total,
                       "income_account": income_account()}],
        })
        si.insert(ignore_permissions=True)
        si.submit()
        made += 1
        if paid:
            pay_invoice(si, "Sales Invoice", cust, total, posting)
    log(f"sales invoices: +{made} (total target ~$148.2k)")


def seed_purchase_invoices(item):
    # materials ~ $52k across suppliers; one payroll-service vendor too
    plan = [
        ("ABC Building Supply", 24500, 7, "materials"),
        ("Metro Metal Roofing", 16800, 5, "materials"),
        ("Felt & Underlayment Co", 10900, 3, "materials"),
        ("FastEquip Rentals", 6400, 2, "overhead"),
    ]
    existing = find_tagged("Purchase Invoice")
    made = 0
    for i, (supp, total, days, kind) in enumerate(plan, 1):
        marker = f"{TAG} PI-{i}"
        if any(marker in (frappe.db.get_value("Purchase Invoice", n, "remarks") or "") for n in existing):
            continue
        ensure_supplier(supp)
        posting = max(MONTH_START, add_days(TODAY, -days))
        exp = acc("Cost of Goods Sold") or acc("Stock Expenses") or acc("Expenses")
        pi = frappe.get_doc({
            "doctype": "Purchase Invoice", "company": COMPANY, "supplier": supp,
            "posting_date": posting, "due_date": add_days(posting, 20),
            "currency": CURRENCY, "remarks": marker, "update_stock": 0,
            "buying_price_list": "Standard Buying",
            "price_list_currency": CURRENCY, "plc_conversion_rate": 1.0,
            "conversion_rate": 1.0,
            "items": [{"item_code": item, "qty": 1, "rate": total, "price_list_rate": total,
                       "expense_account": exp}],
        })
        pi.insert(ignore_permissions=True)
        pi.submit()
        made += 1
    log(f"purchase invoices: +{made} (materials/overhead target ~$58k)")


def pay_invoice(inv_doc, dtype, party, amount, posting):
    """Create + submit a Payment Entry fully settling an invoice (cash in/out)."""
    try:
        from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
        pe = get_payment_entry(dtype, inv_doc.name)
        pe.reference_no = f"{TAG} PAY"
        pe.reference_date = posting
        pe.posting_date = posting
        pe.remarks = f"{TAG} payment {inv_doc.name}"
        pe.insert(ignore_permissions=True)
        pe.submit()
    except Exception as e:
        log(f"payment for {inv_doc.name} skipped: {e}")


def default_cost_center():
    cc = frappe.get_value("Company", COMPANY, "cost_center")
    if cc:
        return cc
    rows = frappe.get_all("Cost Center",
                          filters={"company": COMPANY, "is_group": 0}, fields=["name"], limit=1)
    return rows[0].name if rows else None


def seed_journal_entries():
    existing = find_tagged("Journal Entry")
    cc = default_cost_center()
    made = 0
    # Payroll accrual ~$61k (debit payroll expense, credit a payable/wages account)
    entries = [
        ("Payroll accrual — 12 crew + office (June)", 61000,
         acc("Salary") or acc("Payroll") or acc("Indirect Expenses"),
         acc("Payroll Payable") or acc("Accounts Payable") or acc("Duties and Taxes")),
        ("Depreciation — trucks & equipment (June)", 2100,
         acc("Depreciation"), acc("Accumulated Depreciation") or acc("Plant and Machinery")),
    ]
    for i, (title, amt, dr, cr) in enumerate(entries, 1):
        marker = f"{TAG} JE-{i}"
        if any(marker in (frappe.db.get_value("Journal Entry", n, "user_remark") or "") for n in existing):
            continue
        if not (dr and cr):
            log(f"JE {i} skipped (account missing dr={dr} cr={cr})")
            continue
        je = frappe.get_doc({
            "doctype": "Journal Entry", "company": COMPANY,
            "voucher_type": "Journal Entry",
            "posting_date": max(MONTH_START, add_days(TODAY, -2)),
            "user_remark": f"{title} | {marker}",
            "accounts": [
                {"account": dr, "debit_in_account_currency": amt, "cost_center": cc},
                {"account": cr, "credit_in_account_currency": amt, "cost_center": cc},
            ],
        })
        je.insert(ignore_permissions=True)
        je.submit()
        made += 1
    log(f"journal entries: +{made} (payroll ~$61k + depreciation)")


def seed_bank_transactions(bank_account):
    # Unreconciled/uncategorized deposits + withdrawals -> "uncategorized queue".
    # We DON'T set bank_party_* or allocate to a payment, so they stay unmatched.
    plan = [
        ("Deposit — ACH SUMMIT CEDAR RIDGE", 28400, 0),
        ("Deposit — CHECK 10482 NORTHGATE", 14200, 0),
        ("Deposit — ZELLE HENDERSON 12900", 12900, 0),
        ("POS DEBIT — HOME DEPOT #4471", 0, 1840),
        ("ACH DEBIT — FUEL CARD WEX", 0, 920),
        ("CHECK 2231 — INSURANCE PREMIUM", 0, 3100),
        ("CARD — OFFICE SUPPLIES STAPLES", 0, 410),
    ]
    existing = find_tagged("Bank Transaction")
    made = 0
    for i, (desc, dep, wd) in enumerate(plan, 1):
        marker = f"{TAG} BT-{i}"
        if any(marker in (frappe.db.get_value("Bank Transaction", n, "description") or "") for n in existing):
            continue
        bt = frappe.get_doc({
            "doctype": "Bank Transaction", "company": COMPANY,
            "date": add_days(TODAY, -(i % 5)),
            "bank_account": bank_account,
            "deposit": dep, "withdrawal": wd, "currency": CURRENCY,
            "description": f"{desc} | {marker}",
        })
        bt.insert(ignore_permissions=True)
        bt.submit()  # submitted but unreconciled -> shows as uncategorized
        made += 1
    log(f"bank transactions: +{made} unreconciled/uncategorized")


def summary():
    rev = sum((frappe.db.get_value("Sales Invoice", n, "base_grand_total") or 0)
              for n in find_tagged("Sales Invoice"))
    ar = sum((frappe.db.get_value("Sales Invoice", n, "outstanding_amount") or 0)
             for n in find_tagged("Sales Invoice"))
    ap = sum((frappe.db.get_value("Purchase Invoice", n, "outstanding_amount") or 0)
             for n in find_tagged("Purchase Invoice"))
    uncat = len([1 for n in find_tagged("Bank Transaction")
                 if (frappe.db.get_value("Bank Transaction", n, "unallocated_amount") or 0) != 0
                 or (frappe.db.get_value("Bank Transaction", n, "status") or "") != "Reconciled"])
    return {"revenue_mtd": rev, "ar": ar, "ap": ap, "uncategorized": uncat}


def main():
    # Suppress notification/alert templates during bulk seeding. Several stock ERPNext
    # Notification templates (e.g. "new fiscal year") raise on a bare site; in_import is
    # the documented flag that makes the notification engine skip evaluation. Scoped to
    # this process only — it is NOT a persistent config change to the install.
    frappe.flags.in_import = True
    ensure_fiscal_year(2025)
    ensure_fiscal_year(2026)
    ensure_company()
    for m in ("Cash", "Bank Draft", "Wire Transfer", "ACH/EFT"):
        ensure_mode(m)
    frappe.db.commit()
    bank_account, _ = ensure_bank_account()
    item = ensure_item()
    frappe.db.commit()
    seed_sales_invoices(item)
    seed_purchase_invoices(item)
    seed_journal_entries()
    seed_bank_transactions(bank_account)
    frappe.db.commit()
    s = summary()
    log("done")
    final = "SEED_OK company=%s revenue_mtd=%.0f ar=%.0f ap=%.0f uncategorized=%d" % (
        COMPANY, s["revenue_mtd"], s["ar"], s["ap"], s["uncategorized"])
    print(final)
    _LOGF.write(final + "\n")
    _LOGF.flush()


import traceback as _tb
try:
    main()
except Exception:
    err = "SEED_ERROR\n" + _tb.format_exc()
    print(err)
    _LOGF.write(err + "\n")
    _LOGF.flush()

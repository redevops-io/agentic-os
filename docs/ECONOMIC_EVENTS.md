# Economic events

`agentic_os/economics` turns the accounting records the business applications already hold into a typed, read-only list of **economic events**: what an operation did to money, with the records that prove it. It is a projection, not a ledger. It stores no balance, writes nothing, and adds no second source of truth: ERPNext's documents and general ledger stay the record.

## What an event is

| Field | Meaning |
|---|---|
| `kind` | `sale`, `discount`, `return`, `cost_of_goods`, `supplier_cost`, `supplier_return`, `support_cost`, `rework_cost`, `cancellation`, `cancellation_handling` |
| `basis` | `posted`: an amount written on a submitted accounting document. `estimated`: derived (hours times a costing rate; revenue a cancellation forwent) |
| `amount` | the effect on profit: money in positive, money out and revenue given up negative |
| `subjects` | `customer`, `supplier`, `project`, `invoice`. A customer a resolver cannot place is kept as `unresolved:<name>`, never merged |
| `sources` | the documents it comes from, e.g. `erpnext:Sales Invoice/ACC-SINV-2026-00001` |
| `supersedes` | the document a credit note or debit note corrects |
| `approval` | the approval id and approver, read from an `aos-approval id=… approver=… action=…` comment on the document (what a governed write leaves). Absent means no reference was found, not that none happened |

A correction is a later event that points at the one it supersedes. Nothing is edited or deleted. A cancelled invoice is an event too: its posting was reversed, so its posted effect is nil; what it cost is the revenue it forwent when no amended invoice replaced it (estimated) plus the time logged against it.

## What it reads

Sales Invoice (with item rows), Purchase Invoice, Timesheet (support, rework and cancellation-handling time), Project, Comment, Payment Entry and Bank Transaction. Discount is computed as list price minus invoiced net, so it ties out to the invoice total whether the discount was on the item or on the document.

A read that fails, a field the application leaves out of its answer, a custom field the site does not have, an activity type that is not mapped, and an invoice whose item rows could not be read are each recorded as a **gap** with what they cost. A gap is never a zero.

Two behaviours of ERPNext 16 found while building this, both silent: a read of a child table without `parent=<parent doctype>` answers with rows that hold only `name`, and a requested field the application will not give (for example `costing_amount` on a time log) is left out of the answer without an error. `ErpnextClient.read_list` raises where `get_list` returns an empty list, and passes the parent doctype for child tables; the extractor treats an absent column as a gap.

## The five answers (`answers.py`, `reconcile.py`)

1. Profit per customer: revenue at list, discounts, net revenue, cost of goods, support, rework and cancellation handling, with the posted and estimated parts of profit shown separately.
2. What cancellations cost in a period: forgone revenue and handling time, both estimates; posted effect nil.
3. Discounts given, split by whether the document carries an approval reference, and by approver.
4. Supplier cost: net purchases (posted, after debit notes) plus rework the time logs attribute to the supplier (estimated).
5. Discrepancies between a payment the books recorded and the bank line with the same reference, traced to payment, invoice and customer, with a **proposed** correction (`applied: false`). Making and approving the correction is a person's act through the application, or a governed write once those are enabled; this module then shows from the application's records that the discrepancy is gone.

## Checks

`verify.ledger_check` compares the events with the **general ledger** by a different path: income credits and cost-of-goods debits per sales voucher, payable credits per purchase voucher, and each timesheet's own total. A voucher with taxes or no ledger lines is reported as unverifiable, never as matching.

`reliability.measure` gives, per operation class, the number of independently verified outcomes and the lower end of the 95 % Wilson interval of the verified rate. It counts only independent verification, never the model's stated confidence, and says `gather_evidence` below 30 outcomes. It is a measurement: the permit / approval-required / deny decision belongs to the authorization plane.

## Where it appears

The `economics` dashboard (enabled with the `erpnext` source) shows the same figures in Projects and, through `dashboards.get`, to an external agent whose token holds `dashboards.read`. A tenant is granted it as `dashboards:economics`.

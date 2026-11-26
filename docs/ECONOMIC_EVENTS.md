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
| `approval` | an **unverified claim**: the approval id and approver named in an `aos-approval id=… approver=… action=…` comment on the document. Anyone who can comment in ERPNext can write that line, and an agent that writes a governed comment writes the words itself, so it is not evidence that anyone approved. `verified` is always `False` here; absent means no claim was found. Evidence of an approval would have to come from something only the trusted side writes (a governed request's decided_by, action and input digest, or ERPNext's own workflow or version history); nothing reads one yet |

A correction is a later event that points at the one it supersedes. Nothing is edited or deleted. A cancelled invoice is an event too: its posting was reversed, so its posted effect is nil; what it cost is the revenue it forwent when no amended invoice replaced it (estimated) plus the time logged against it.

## What it reads

Sales Invoice (with item rows), Purchase Invoice, Timesheet (support, rework and cancellation-handling time), Project, Comment, Payment Entry and Bank Transaction. Discount is computed as list price minus invoiced net, so it ties out to the invoice total whether the discount was on the item or on the document.

A read that fails, a field the application leaves out of its answer, a custom field the site does not have, an activity type that is not mapped, and an invoice whose item rows could not be read are each recorded as a **gap** with what they cost. A gap is never a zero.

Two behaviours of ERPNext 16 found while building this, both silent: a read of a child table without `parent=<parent doctype>` answers with rows that hold only `name`, and a requested field the application will not give (for example `costing_amount` on a time log) is left out of the answer without an error. `ErpnextClient.read_list` raises where `get_list` returns an empty list, and passes the parent doctype for child tables; the extractor treats an absent column as a gap.

## The five answers (`answers.py`, `reconcile.py`)

1. Profit per customer: revenue at list, discounts, net revenue, cost of goods, support, rework and cancellation handling, with the posted and estimated parts of profit shown separately.
2. What cancellations cost in a period: forgone revenue and handling time, both estimates; posted effect nil.
3. Discounts given, split by whether the document carries an approval claim, and by claimed approver. Both are labelled unverified (see `approval` above).
4. Supplier cost: net purchases (posted, after debit notes) plus rework the time logs attribute to the supplier (estimated).
5. Discrepancies between a payment the books recorded and the bank line with the same reference, traced to payment, invoice and customer, with a **proposed** correction (`applied: false`). Making and approving the correction is a person's act through the application, or a governed write once those are enabled; this module then shows from the application's records that the discrepancy is gone, and `corrections` lists a payment that was cancelled and entered again under the same reference (the cancelled entry stays in the application) with whether the bank now agrees.

## Checks

`verify.ledger_check` compares the events with the **general ledger** by a different path: income credits and cost-of-goods debits per sales voucher, payable credits per purchase voucher, and each timesheet's own total. A voucher with taxes or no ledger lines is reported as unverifiable, never as matching.

`reliability.measure` gives, per operation class, the number of independently verified outcomes and the lower end of the 95 % Wilson interval of the verified rate. It counts only independent verification, never the model's stated confidence, and says `gather_evidence` below 30 outcomes. It is a measurement: the permit / approval-required / deny decision belongs to the authorization plane.

## Where it appears

The `economics` dashboard (enabled with the `erpnext` source) shows the same figures in Projects and, through `dashboards.get`, to an external agent whose token holds `dashboards.read`. A tenant is granted it as `dashboards:economics`.

## Communications as evidence (`evidence.py`, `correlate.py`)

Chatwoot conversations (customer messages, company replies, private notes) and Twenty notes are read with strict, paginated readers (`ChatwootClient.read_conversations` / `read_messages`, `TwentyClient.read_page` / `read_all`). They raise where the older sensor reads return `[]`, so a failed or cut read is a recorded gap ("its absence is not evidence of silence"), never an empty answer. Chatwoot activity lines (reopened, assigned) are counted, not kept as speech.

`EvidenceRef` is built on `integrations.business.contracts.Provenance` (provider, the source's own id, when it was observed, when it was said) and adds the tenant, version marker, author and role (`customer`, `company`, `internal_note`, `crm_note`), time, link and the access scope that governed the read. Scope is applied before anything is retrieved: the collectors read only the sources they are given.

The customer is resolved by an **explicit identifier** both sides hold (contact e-mail, website domain). A name that matches a customer but no identifier is reported as `name_only` and not resolved. A note attached to more than one company has no customer.

`correlate` links a communication to an economic event with the method named: a named document (`explicit_identifier`; it says what the message is about, not why), or the same customer in a window with a percentage or amount the event's own numbers reproduce and the matching kind of event (`customer+time+percent+kind`). Same customer and period alone is only a `candidate`, or `ambiguous` between equally fitting events, and a figure the event does not reproduce ("25% off" beside a 40% discount) blocks the link. A statement of a discrepancy's amounts by the same customer is linked to it and marked as not independent bank evidence. Nothing here decides why an adjustment was made; that, and whether a message is a commitment, is the next step.

## Why it happened, and what was promised (`reasoning.py`)

The language model only reads evidence and proposes. Deterministic guards decide.

- **Explanation of an adjustment.** Only `linked` communications, and the rest of the conversation each belongs to, are shown to the model, inside a delimited block marked as untrusted data. It judges each message (does it say why?), then gives a reason. A reason is kept only if every message it cites was shown; a statement that points at material not in the data ("as discussed by email", "the attached quote") does not establish the reason; instruction-like text is never shown and is reported. Class: `documented` (one statement, mirrors counted once), `corroborated` (two or more independent statements by different parties, for example the customer and the company), `unknown`. There is no `inferred` output yet. An approval is always an unverified claim.
- **Commitments.** One model call per conversation thread finds promises with a status (confirmed, conditional, proposal, rejected, superseded, under review). Matching to native records is deterministic: a confirmed refund or credit is matched to the customer's credit notes, several records can add up to one promise, a record is used once, and a different currency or amount is `inconsistent`, never converted. A promise with no amount, or a kind no record can confirm (a replacement), is `unverifiable`; a promise with nothing recorded is `missing`.
- **Limits.** A message with no figure is not linked to an event, so a reason stated in words alone (pilot pricing, no percentage) is not found. Results and held-out scoring are in the acceptance notes, not here.

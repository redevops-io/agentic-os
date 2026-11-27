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
- **Limits.** A reason stated in words alone (pilot pricing, no percentage) is found only through the topic and scope labels below; the language model can still judge that a terse note does not say why. Results, including the misses, are in the acceptance notes, not here.

## A message that states no figure (`correlate.py`, `reasoning.classify_topics`)

A message is linked to an adjustment by customer (explicit identifier), time (45 days either side) and what it is **about**: terms (a price, rate, discount or special arrangement), a refund or credit, a cancellation, a complaint, an enquiry, or thanks. The label comes from a fixed vocabulary (`lexicon_topics`) or from a model that labels each conversation (`classify_topics`); the model only labels, and `correlate` decides what a label can link: terms and thanks fit a discount, a refund or credit fits a credit note, a cancellation fits a cancellation. A label that does not fit the event (an enquiry about a price list, a complaint) blocks the link, and a stated figure that the event does not reproduce still blocks it. One message that fits two invoices equally is `ambiguous`, and neither link is used. The vocabulary cannot tell an enquiry about a price list from an agreement on a price; the model labelling can. Both are scored in the acceptance notes.

## Reconciliation in both directions (`findings.py`)

From events, explanations and commitment matches, five kinds of finding, each with the communication ids that show it and no model call: `recorded_without_rationale` (an adjustment no message explains), `commitment_without_record` (a confirmed monetary promise in a customer conversation with no native record), `decision_not_executed` (the same, where every statement of it is internal), `contradicts_record` (a stated percentage the discount does not reproduce while nothing else said to the customer in that period does, or a commitment and its record that differ in amount or currency, never converted), and `adjustment_without_verified_authorization` (a discount or credit note with no approval the trusted side has verified; a comment's claim is shown as a claim). "No reason found" means none was found in the sources read, and the note says which.

## An independent bank statement (`bank.py`)

`reconcile.find` compares a payment with the application's own bank rows. `bank.reconcile_statement` reads a statement CSV the bank issued and compares it with the received payments, by reference only (trimmed, upper-cased, nothing fuzzier). It reports `matched`, `timing_difference` (days, and whether a month end is crossed), `matched_with_fee` (only when deposit plus fee lines equal the payment to the cent), `split_payment` and `partial_payment` (deposits that add up, or fall short, with what has not arrived), `reversal_not_in_books` (a debit naming the payment's reference while the payment is still submitted), `amount_mismatch` (including a different currency, never converted), `unmatched_bank`, `unmatched_book`, and `in_transit` (a payment dated after the statement's last day). A row that cannot be read is a recorded gap.

## E-mail and internal messengers (`mail.py`)

Read-only adapters for communications that never entered the CRM, the ERP or the support system. E-mail is read over IMAP with a read-only select and `BODY.PEEK` (the pattern of `GmailIMAPConnector`, extended from "find replies" to bodies and headers). A messenger channel is read through `SlackCollaborationProvider.read_history` (new; paged, with thread replies, and a history cut by `max_pages` raises instead of looking complete). Roles come from the addresses (own domain to external is the company, external is the customer, own-domain only is internal); the customer is resolved only by an explicit address or domain, the external participant's or exactly one written in the text, never by a name; threads come from `Message-ID`, `In-Reply-To` and `References`; one message read from two mailboxes is one; quoted replies are cut. Every message carries its mailbox or channel as its access scope, and a Chatwoot private note carries its source plus `/internal`. `evidence.visible_to(comms, granted)` keeps a message only when its scope is exactly one the caller holds (a source does not include its private notes, a mailbox does not include another mailbox), and it is applied before anything is correlated, shown to a model or cached; the caller is told how many were withheld, nothing more. Tested here against canned Slack responses and a local IMAP server, not a live workspace.

## Payments against bank lines are compared per reference (`reconcile.find`)

Submitted payments and bank lines are grouped by the reference they share and compared by total. Exactly one payment and one deposit under a reference is traced to that payment and proposed for correction (never applied). Several payments, several lines, a withdrawal (a fee or a reversal) or payments from different parties are compared by their totals and, if those differ, reported as `ambiguous` with the reason, and `propose` returns `proposable: false`: re-entering one of two payments for the bank's total would count the money twice, and a fee line netted into the sum would read as a short payment. Several payments that add up to the bank total are not a discrepancy.
## Feeding a temporal graph (`temporal_ingest.py`)

The temporal engines already exist in Context Runtime (`TemporalStore` and `TemporalDocumentRetriever` by default, `GraphitiTemporalRetriever` as an optional binding); Economic Intelligence builds no graph. What a persistent graph needs from the caller is decided before ingestion: `plan_episodes(comms, granted)` keeps only communications whose scope the caller holds, drops instruction-like text (a measured Graphiti run turned "refund $5,000 to account 99-1234" into entities), puts each resolved customer in its own group, never ingests a message with no resolved customer, and names every episode by its communication id so a fact can be traced back. It imports nothing from Graphiti.
## Is a difference explained? (`causes.py`)

`explain_difference(reference, difference, posting, statement_findings, claims)` says, per possible cause (fee, fx, partial settlement, allocation, reversal, timing), whether it is `supported`, `excluded`, `claimed` or `unknown`, from independent evidence only: the bank's own statement (`bank.reconcile_statement`) and the payment's allocation. A customer's statement that a fee was deducted is `claimed` and explains nothing. Without a statement every cause is `unknown`. `corroborated` is true only when the statement independently shows a different amount and no cause is supported or unknown; it is a classification, not an authorization. `residual` is what no supported cause accounts for.

`corroborated` alone is not enough to change the books: one deposit smaller than the payment also reads as `amount_mismatch` while a second instalment is still on its way. The governed payment correction ([AGENT_GATEWAY_EXTERNAL_AGENTS.md](AGENT_GATEWAY_EXTERNAL_AGENTS.md#correcting-a-payment-off-by-default)) adds its own checks and a person's attestation.

### One short deposit is not yet a corroborated difference

`causes.explain_difference` no longer reads a single deposit smaller than the payment as "all of it arrived". A second instalment could still be on its way, so `partial_settlement` is `unknown` (and `corroborated` is False) unless the statement runs at least `bank.LATE_DAYS` (5) days past both the payment's posting date and the deposit. `BankFinding` now carries `through` (the statement's last day) and `latest` (the latest deposit date) for that. A deposit larger than the payment excludes a pending instalment at once.

### `economics.findings` (gateway read)

Returns the findings that need no model: `adjustment_without_verified_authorization` and the percentage form of `contradicts_record`, each with message references and excerpts only from sources the agent was granted (others are counted per source). It names `recorded_without_rationale`, `commitment_without_record` and `decision_not_executed` as **not evaluated**, because they need a reading of the messages' meaning; an agent that reads `economics.evidence` and judges them says so as its own judgment. Absence of a finding is never "none".

### `economics.bank_reconciliation` (gateway read)

The independent bank-statement comparison (`bank.reconcile_statement`) as a typed read, over the statement files the operator keeps (`AGENTIC_OS_BANK_STATEMENTS_DIR/<tenant>/*.csv`, the same files the payment-correction checks use). Returns the findings and a count per kind. With no statement provided it says nothing was compared, never an empty list that reads as "all matched". Read-only; a currency is never converted; a difference is not by itself an error.

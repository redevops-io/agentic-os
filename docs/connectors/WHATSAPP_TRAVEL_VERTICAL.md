# First real connector — travel agent: missing-document follow-up over WhatsApp

**Status:** design + audit, 2026-10-21. Nothing here sends a real message. The Today / capture / approval path it plugs into is
built and tested against a *fake* adapter; this document is what must be true before a real one is switched on.

## 1. The vertical (why this one)

A travel agent keeps asking: *who still owes me a document, who needs a reminder, what happened after the reminder?* That is the
original complaint (notebooks beat the CRM because entry is extra work) in its smallest form, and it exercises every layer:
observation → case → next action → exact-message review → authenticated approval → governed send → provider receipt →
waiting → follow-up or verified outcome.

```
Inbound WhatsApp message / case observation
  → resolve customer + case                      (E.164 → contact → case; ambiguity asks, never guesses)
  → identify the missing document                (from the case's checklist — a fact, not an LLM guess)
  → create the actionable Today item             (exists: agentic_os/today.py)
  → review exact recipient + exact message       (exists: payload digest binds recipients + content + attempt)
  → authenticated approval                       (MISSING: tenant + actor identity; today a fixed sample principal)
  → governed Mission execution                   (exists for the fake adapter: approval bridge → operator → receipt)
  → provider receipt + verification              (MISSING: needs WhatsApp status webhooks)
  → wait; follow-up resurfaces on timeout        (exists)
  → outcome observed                             (MISSING: inbound document from that contact, then validated)
```

## 2. Audit — what the repo has today (checked 2026-10-21)

| Needed | Exists? | Where / gap |
|---|---|---|
| Send a WhatsApp text | partial | `agentic_os/collaboration/whatsapp.py` — Cloud API text + reaction; credential-gated; injectable transport. **No template messages.** |
| Receive inbound messages | partial | `normalize_event` maps `entry[].changes[].value.messages[]`. **Only `messages` — never `statuses`.** |
| Verify the webhook is really Meta | **no** | no `hub.verify_token` handshake, no `X-Hub-Signature-256` check anywhere in `agentic_os/` or `apps/` |
| Delivery receipts (sent / delivered / read / failed) | **no** | `statuses` webhooks are not parsed, so "provider confirmed delivery" can't be real |
| 24-hour window / template policy | **no** | business-initiated messages outside the window must be pre-approved templates |
| Consent store + STOP handling | **no** | the sample's `consent: bool` on a fixture target is the only thing |
| Wired into a route or app | **no** | referenced only by `tests/test_meta_social_whatsapp.py` |
| Tenant + actor on `/today` | **no** | anonymous per-session sandbox; approver is a fixed sample principal |
| Approval binds exact payload, single-use, principal-bound | **yes** | `ExternalApprovalBridge` (digest, attestation required, consumed once) |
| Governed execution + receipt + verification | **yes** (fake adapter) | `ExternalAgentOperator`; `verify_result` refutes a "false success" |
| Idempotent retry / no duplicate send | **yes** (in-process) | digest-bound approvals + attempt counter; **not crash-safe** (§4.5) |
| Case / contact store | **no** | the five Today cases are fixtures |

## 3. Decisions needed from you (these block building, not designing)

1. **Provider:** Meta WhatsApp Business **Cloud API directly**, or a BSP (Twilio, 360dialog)? Direct is cheaper and matches the
   existing adapter; a BSP simplifies onboarding/templates for non-technical agencies. Webhook shapes differ slightly.
2. **WABA ownership:** one platform-owned WABA with a number per tenant, or each customer brings their own WABA? Decides credential
   scoping (§4.4) and whose quality rating/limits a bad send damages.
3. **Identity/authN:** which IdP (OIDC) issues the tenant + actor the API will trust? Nothing in §4.1–4.2 can be built without it.
4. **Where cases live for v1:** Projects store (Missions + Artifacts + HumanRequests, which already model draft → review → approve →
   send → receipt) vs a Twenty/ERPNext custom object. Recommendation: **Projects store for v1** (no second system of record to
   reconcile), referencing the CRM contact by id; revisit once a real CRM integration is chosen.
5. **Consent basis and jurisdiction** for the pilot agency (opt-in evidence, retention). Needs legal review — passport scans are
   sensitive personal data; do not assume a generic reminder integration is compliant.
6. **Pilot customer:** one real agency willing to run it with human approval on every send.

## 4. Preconditions before any production send (each needs a test that proves it)

### 4.1 Actor identity and authorization
`/today/*` must derive **tenant, actor and roles from a verified credential**, never from the request body or a session header.
Approve requires an *approver* role; optionally maker ≠ checker. The approval binds the verified principal (the bridge already
refuses a mismatched principal) with a real attestation (SSO session / WebAuthn), not the constant `"projects-session"`.
*Proof:* a request with another tenant's token cannot read or approve this tenant's item (404, not 403, to avoid existence leaks);
an actor without the role is refused; the approval record names the verified actor.

### 4.2 Tenant isolation
State is keyed by **tenant** (and actor), not by an anonymous session id. Every query, ledger, outbox and credential lookup takes
the verified tenant from the server's auth context. Webhooks resolve the tenant from the **`phone_number_id` → tenant mapping the
operator configured** — never from fields in the payload.
*Proof:* two tenants, same case keys and phone numbers: no cross-visibility through list, detail, approve, undo, notifications,
capture, batch; a forged `phone_number_id` webhook is dropped.

### 4.3 Webhook authenticity
`GET` verify handshake (`hub.verify_token`, constant-time) and `POST` `X-Hub-Signature-256` = HMAC-SHA256 of the **raw body** with the app
secret, constant-time compare, body-size cap, timestamp freshness window, dedup by message/status id (Meta retries), reject on any
parse doubt. *Proof:* tampered body, wrong secret, missing header, replay, oversized body → rejected with no side effect.

### 4.4 Credential scoping
The WABA access token is **per tenant (or per WABA), in the secret store**, resolved server-side at send time — never in a request,
a Today item, a log, an audit export or a prompt. Least privilege (`whatsapp_business_messaging` only). Rotation without a deploy.
*Proof:* grep of logs/receipts/audit export for the token in a send run finds nothing; a tenant without credentials gets
`not_configured`, never another tenant's sender.

### 4.5 Idempotency and crash safety (the hard one)
The Graph API has **no idempotency key**. Our digest-bound single-use approval stops a *user* double-send, but a process crash
between "Meta accepted the message" and "we recorded it" could duplicate on retry. Design:
write an **outbox row `sending` (attempt id, digest, tenant) before the call**; pass the attempt id as `biz_opaque_callback_data`
(echoed back in status webhooks); on restart, any `sending` row is `HELD` — **never auto-retried** — and is reconciled by the status
webhook carrying that attempt id, or resolved by a human ("we can't tell if this went out"). *Proof:* kill the process after the HTTP
call returns and before the receipt is written; on restart exactly one message exists and the item says so.

### 4.6 Consent
A `ConsentRecord` per (tenant, subject, channel): basis + evidence ref, granted/withdrawn timestamps. Checked **at approval and again
at send time** (withdrawal between the two blocks the send). Inbound `STOP`/`UNSUBSCRIBE` withdraws automatically and surfaces in
Today. No record = no send (today's fixture default of `consent: true` must not survive). *Proof:* withdraw after approval → send
refused, item shows why; STOP inbound → next attempt refused.

### 4.7 WhatsApp policy
Free-form text only inside the 24-hour customer-service window; otherwise only an **approved template** with bound variables. The
preview the user approves must be the exact template + variables, and the digest covers both. Per-tenant daily send cap and a kill
switch. *Proof:* an out-of-window send without a template is refused before approval is even offered.

### 4.8 Receipts, verification, outcome — three different facts
`accepted by Meta` ≠ `delivered` ≠ `read` ≠ `customer responded` ≠ `document received` ≠ `document validated`. Map them onto the
existing lifecycle (suggested → approved → sent → delivery verified → outcome observed) and never collapse them: a `delivered`
status verifies **delivery only**; the item stays *waiting* until an inbound document from that contact is attached to the case
**and** validated (format/legibility/expiry check, or a human). *Proof:* a `failed` status with an error code surfaces as failed with
a plain reason; a "delivered" webhook alone never closes the item.

### 4.9 Audit and privacy
Append-only audit event per step (who, tenant, digest, decision, provider ids, timestamps), exportable. Phone numbers hashed in
logs; message bodies and documents never logged; documents encrypted at rest with a retention policy; **no document content to a
model without an explicit policy.** Passport data is the highest-sensitivity thing this product will hold.

## 5. Where the code goes

Keep `agentic_os/collaboration/whatsapp.py` as the low-level adapter and extend it (template send, `statuses` parsing, raw-body
signature verification helper). Put the **governed connector** where connectors belong — `redevops-connectors` (`IntegrationAdapter`
SDK) — with capabilities `observe.inbound`, `send.template`, `send.text_in_window`, `receipt.status`; the Today/Mission layer calls
capabilities, never the Graph API. The desktop shell and the web UI change nothing: they render what the backend says.

## 6. Delivery phases and exit gates

* **A — real observation, no real sends.** Sandbox WABA + Meta test numbers. Real webhooks with signature verification, real
  `statuses`, inbound → case resolution → Today. Sends only to an allowlist of test numbers. *Gate:* §4.3 proofs.
* **B — identity, tenancy, consent.** Auth (§4.1), tenant keying (§4.2), consent (§4.6), outbox/idempotency (§4.5), credential
  scoping (§4.4). *Gate:* every §4 proof green in CI, including the cross-tenant and crash tests.
* **C — one pilot agency, human approval on every send,** approved templates, daily cap, kill switch. *Gate:* the UX study (see
  `redevops-projects/docs/ux-study/`) with real agency staff, zero false "completed", zero duplicate sends over the pilot.

## 7. What we measure (not vanity)
Verified outcomes (document received **and validated**) per week; follow-ups completed; time from inbound message to a sent,
delivery-confirmed reply; manual fields entered (target: zero beyond the sentence); duplicate sends (must be 0); sends refused by
policy and why; consent withdrawals honoured. Do not count "messages sent" as success.

## 8. Not decided here
Pricing/quotas of WhatsApp conversations, multi-language templates, media (document) download/storage design, group chats, other
channels, and the CRM integration beyond contact lookup. Each is a follow-up, not a blocker for Phase A.

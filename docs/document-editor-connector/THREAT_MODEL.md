# Threat model

Assets: customer documents; the editor JWT secret; approval tokens; the integrity of document versions and receipts.
Trust: the connector, the Mission Runtime and the human approvers. **Not trusted:** document contents, the external
editor's responses, any URL it returns, callers without a valid token.

| # | Threat | Mitigation (test) |
|---|---|---|
| 1 | Unauthenticated or cross-tenant `/invoke` | kernel `mission_gate` on both routes (401/503); tenant token pins the tenant (403 on mismatch); admin must name a configured tenant (`test_secure_invoke`) |
| 2 | Forged approval to trigger a write | kernel `Operator.router()` strips `_approval*`; marker only from a verified, operator/capability/inputs/mission/node-bound, single-use token; client `_auth*` keys stripped (`test_secure_invoke`, conformance forgery suite) |
| 3 | SSRF through the editor URL, result URLs or redirects | `safe_fetch`: allowlist, resolve-once-and-pin, per-zone IP rules, no redirects, scheme/port/credential/encoding rules; result URL must equal the configured editor origin (`test_safe_fetch`, `test_onlyoffice_unit`) |
| 4 | Free-form script execution in the editor (it can reach private hosts) | no caller/document-supplied code ever runs; fixed templates, data via `argument` (`test_injection_lifecycle`, EDIT_API.md) |
| 5 | Prompt injection in a document | document text is returned only as labelled untrusted evidence; no code path turns text into a capability call, an approval or a destination (`test_injection_lifecycle`) |
| 6 | Data exfiltration to a less-trusted editor | classification matrix, unlabeled=CONFIDENTIAL, bound approval, every decision receipted (`test_classification`) |
| 7 | Path traversal / symlink escape in the document store | strict logical ids, per-component link/reparse refusal, realpath containment (`test_store`) |
| 8 | Lost update / silent overwrite | expected-version precondition under a lock; new immutable versions; stale => fail closed (`test_governed_write`) |
| 9 | Double or phantom writes after a network failure | one backend write per idempotency key, `outcome_unknown`, read-back reconciliation, fresh approval required (`test_governed_write`) |
| 10 | Tampered editor output | output validated (magic, container, text/cell expectations from the local simulation) before commit; drafts preserved (`test_governed_write`) |
| 11 | Tampered history | hash-chained receipt ledger, content-addressed blobs re-hashed on read (`test_config_ledger`, `test_store`) |
| 12 | Secret exposure | secrets only from the environment (Vault); config files containing secret-like keys are rejected; `repr` redacts; the JWT secret never appears in session configs or URLs (`test_config_ledger`, `test_onlyoffice_unit`) |
| 13 | Editor crash on malicious OOXML (DoS) | documents are validated and size-capped locally; editor failure is a typed `backend_unavailable` that never affects other apps |
| 14 | Content-URL theft | HMAC-signed, document+version+hash+backend bound, <= 5 min, bytes re-hashed on every read; the URL is the one unavoidable bearer |
| 15 | Uninstall damaging customer data | the connector contains no document deletion; uninstall removes only marker-guarded connector state (`test_injection_lifecycle`) |

Additional controls (review of #458): case-folded document identity; sources are structurally validated and size-capped
before every transmit; a circuit breaker and a per-tenant concurrency limit protect against editor hangs/crashes; the
total request deadline is enforced at socket level; edit results are checked for out-of-scope changes (headers, footers,
comments, notes, external links) and the patch digest is re-derived from the stored ops; idempotency keys are bound to one
proposal. **Prompt-injection tests are structural**: the service contains no LLM, so they prove that no code path turns
document text into a capability call, approval or destination - not that a downstream agent is immune.

Known residual risks: per-process replay guard / content signing key (single replica, as for every operator);
the content URL is a bearer for <= 5 minutes; XMLHttpRequest exists inside the editor's script runtime and the JWT-signed
builder is network-privileged by design (only our fixed templates run); revision marks carry no author.

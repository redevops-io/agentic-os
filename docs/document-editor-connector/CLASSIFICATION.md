# Classification and transmission policy

A document may leave the connector for an external editor only if a **policy decision** allows it, and every decision
(allowed or refused) is written to the document's immutable receipt ledger.

## Labels

`PUBLIC < INTERNAL < CONFIDENTIAL < RESTRICTED`, read from the document's sidecar manifest
(`<root>/<tenant>/.docedit/manifests/*.json`, `classification`). Derived artifacts (conversions, exports, drafts)
inherit the source label. **No label, or an unrecognised label, is treated as `CONFIDENTIAL`** (fail closed).

## Backend trust zones

Each tenant lists the backends it allows with a declared zone: `LOCAL`, `PRIVATE_NETWORK` or `EXTERNAL`. A backend that
is not on the tenant allowlist receives nothing. A tenant cannot claim a zone more trusted than the deployment's real
`backend_zone` (config error). Zones also drive `safe_fetch`: `EXTERNAL` hosts must resolve to global addresses;
`LOCAL`/`PRIVATE_NETWORK` hosts may resolve to private ranges (link-local and metadata addresses are always blocked).

## Default matrix (the floor)

| Label | Allowed backend zones | Extra requirement |
|---|---|---|
| PUBLIC | any allowlisted backend | - |
| INTERNAL | LOCAL, PRIVATE_NETWORK | - |
| CONFIDENTIAL | LOCAL, PRIVATE_NETWORK | a human **approval token bound to (tenant, document id, document sha256, backend)**, single use, <= 300 s |
| RESTRICTED | none | never sent |

Publish destinations are gated by the same matrix (destination name = "backend", zone from config).

## Approvals

* Read-type operations on CONFIDENTIAL/unlabeled documents take `transmit_approval`: a kernel approval token minted with
  `operator="document-editor"`, `capability="document.transmit"`, `inputs=transmit_inputs(tenant, doc_id, sha256, backend)`.
* `apply_patch` / `publish` are already human-approved by the Mission gate; their token binds the document hash
  (`base_hash` / `sha256`), backend / destination and patch digest, so it also serves as the per-document approval.
  RESTRICTED stays refused even then.

## Overrides: stricter freely, looser only if signed

Tenant `policy` may remove zones, add approval, raise the unlabeled default (`default_label`) - never the reverse.
A looser override is accepted only inside `signed_policy` (`payload` + HMAC-SHA256 under the Vault-provided
`DOCEDITOR_POLICY_SIGNING_KEY`, bound to the tenant and an expiry). Sending RESTRICTED additionally needs
`allow_restricted: true` in that signed payload. Any violation is a configuration error: the connector does not start
for that tenant (fail closed).

## Receipts

`transmit_decision` records: allowed, reason, tenant, document id, document sha256, backend/destination, zone,
declared and effective label, whether approval was needed, approver.

## Setting labels

`document.set_label` (always human-approved; the receipt records previous label, new label and whether it was a
**downgrade**) is the production path for labels; until a document is labelled it is CONFIDENTIAL. The policy signing key
(`DOCEDITOR_POLICY_SIGNING_KEY`) must be at least 32 characters or signed policies are refused.

## Identity and trust zone details

Document identity is the NFC, case-folded id, so `Secret.docx` and `secret.docx` share label, versions and lock on every
OS; two directory entries differing only by case are refused as ambiguous. Zones are checked against the RESOLVED address:
LOCAL hosts must resolve to loopback, PRIVATE_NETWORK hosts to RFC1918/ULA/CGNAT (a "private" host resolving to a public IP
is refused), EXTERNAL hosts to global addresses only.

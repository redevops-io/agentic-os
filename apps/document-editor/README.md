# agentic-document-editor

A connector between the Agentic OS Mission Runtime and an **externally managed document editor**. The editor is never
bundled, deployed or managed by Agentic Apps. **Stock ONLYOFFICE Document Server** is implemented (and proven against a
real server); Collabora is a declared-but-unimplemented provider (everything fails closed); ElixirOffice is experimental
and disabled. No undocumented endpoints, no headless-browser automation in any production path.

* Source documents live on a **local document root** (`DocumentStore` interface; S3 is a future implementation).
* Every transmission to the editor passes a **classification gate** (`docs/document-editor-connector/CLASSIFICATION.md`;
  unlabeled = CONFIDENTIAL, RESTRICTED never sent).
* Edits are **proposed, approved by a human, applied as a NEW version, verified by read-back** - never in place.
* Docs: [architecture](../../docs/document-editor-connector/ARCHITECTURE.md),
  [edit API notes](../../docs/document-editor-connector/EDIT_API.md),
  [classification](../../docs/document-editor-connector/CLASSIFICATION.md),
  [threat model](../../docs/document-editor-connector/THREAT_MODEL.md),
  [OS support](../../docs/document-editor-connector/OS_SUPPORT.md),
  [credentials](../../docs/document-editor-connector/CREDENTIALS.md).

## Capabilities (`GET /capabilities`, authenticated)

`document.connect_check`, `inspect`, `open_session` (view-only, signed, <=5 min), `convert`, `export`, `propose_patch`
(no mutation), `apply_patch` (approval-gated, PRESERVE), `set_label` (approval-gated), `publish` (approval-gated, irreversible, allowlisted destinations).
`connect_check` returns the **truthful capability report**: `{open, convert, inspect, tracked_changes, automated_edit,
export}`, each `true` only if declared in config AND proven for that backend.

## Configuration

```yaml
# DOCEDITOR_CONFIG=/etc/docedit/config.yaml   (no secrets in this file; they are rejected)
document_backend:
  provider: onlyoffice          # onlyoffice | collabora | elixiroffice
  deployment: external          # anything else is a config error
  capabilities: {open: true, convert: true, inspect: true, export: true, automated_edit: true, tracked_changes: true}
document_root: /data/documents
state_dir: /var/lib/docedit
content_base_url: https://docedit.internal:8216   # how the EDITOR reaches this service
backend_zone: PRIVATE_NETWORK                     # the editor's real network position
tenants:
  acme:
    backends: {onlyoffice: PRIVATE_NETWORK}
    publish_destinations: {board-share: {path: /data/published/acme, zone: PRIVATE_NETWORK}}
```

Environment (Vault-provided at runtime): `DOCEDITOR_ONLYOFFICE_URL`, `DOCEDITOR_ONLYOFFICE_JWT_SECRET` (+ optional
`DOCEDITOR_POLICY_SIGNING_KEY`, `DOCEDITOR_CONTENT_SIGNING_SECRET`), and the platform's `AGENTIC_OS_APPROVAL_SECRET`,
`AGENTIC_OS_CONTROL_TOKEN` / `AGENTIC_OS_TENANT_TOKENS`. Documents are laid out `<root>/<tenant>/<doc id>`.

## Running

```
uvicorn document_editor.app:app --port 8216      # image: Dockerfile (kernel pinned >= v0.3.32)
```
`/health` never depends on the editor. Without configuration or with the editor down, calls return typed failure-shaped
results (`not_configured`, `backend_unavailable`); other apps are unaffected.

## Uninstall

Uninstalling the connector leaves **external documents, document versions, receipts, published copies and the editor
untouched**. Nothing in the connector deletes documents or editor data (a static test enforces it). Only the connector's
own state directory (proposals) is removed, and only if it carries the connector's marker file (`lifecycle.py`); the
Vault references are removed by the platform's uninstall flow.

## Tests

```
PYTHONPATH=apps python -m pytest apps/document-editor -m "not integration"     # unit, any OS, no Docker
python apps/document-editor/integration/run_throwaway.py                       # real Document Server in a throwaway container
```
CI: `.github/workflows/document-editor.yml` runs the unit tests on ubuntu, macos and windows; the merge gate in
`tests.yml` runs them (and the conformance suites) on Ubuntu.

## Known gaps

* The kernel's `HTTPOperatorClient` sends no `Authorization` header: use `transport.bearer_transport(token)`
  (`HTTPOperatorClient(urls, transport=bearer_transport(token))`) to call this authenticated operator.
* Single replica only (per-process replay guard and content signing key unless `DOCEDITOR_CONTENT_SIGNING_SECRET` is set).
* Stack integration (compose service, Ansible, drift allowlist, credential-inventory entries - see CREDENTIALS.md) is
  a follow-up in `agentic-os-stack`; no production deployment was made.
* View-only editor sessions (no save callback); patch operations are `replace_text`, `append_paragraph` (docx) and
  `set_cell` (xlsx); tracked changes are docx-only and carry no author attribute.
* Collabora, S3 storage and ElixirOffice are not implemented.

## Authorization note

Note: basic permissions authorise the app INSTANCE, not callers: an instance with write grants lets every caller who can reach it write. See `docs/PERMISSIONS_MIGRATION.md` section 29.

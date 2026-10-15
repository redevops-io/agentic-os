# Architecture

```
Mission Runtime --(Bearer + signed approval token)--> document-editor (/invoke, /capabilities)  [kernel mission_gate]
                                                           |
              DocumentService  --(classification gate)--> content URL (signed, <=5 min) <--- fetched by the editor
                |        |                                      
        DocumentStore   DocumentBackend (ABC)  --safe_fetch-->  external editor (ONLYOFFICE; Collabora later)
        (LocalFS; S3                                     
         later)         receipts (hash chain) <- every decision / write / refusal
```

* **App**: `apps/document-editor` (Operator SDK app, AGPL like the others). `app.py` mounts the *kernel's*
  `Operator.router()` behind `mission_gate` (`secure_router.py`) and serves `GET /content/{token}` for the editor.
* **Provider interface** `DocumentBackend` (`backend.py`): `describe()`, `health()`, `inspect`, `open_session`, `convert`,
  `export`, `apply_edit`. Declared capabilities (config) AND the adapter's proven set decide what is `true`; every
  operation calls `require(capability)` first and fails closed (`UnsupportedCapability`).
* **Adapters**: `onlyoffice.py` (read-only: JWT, health, convert/export, inspect, view-only session),
  `onlyoffice_edit.py` (Document Builder edit), `UnimplementedBackend` for Collabora / ElixirOffice (experimental, off).
* **Store** `DocumentStore` (`store.py`): `LocalFSStore` today. Sidecar manifest with a monotonic version counter,
  content-addressed blobs, classification label; S3 is a future implementation of the same interface (not implemented).
* **Service** (`service.py`, `writepath.py`): transmit gate -> backend -> validate -> store -> read-back -> receipt.
* **Registration**: eight `document.*` capabilities (see `operator.py`) with the 0.3.33 per-action compensation model:
  `apply_patch`, `convert`, `export` = `PRESERVE` (new versioned artifacts, never auto-undone); `publish` = irreversible.
  `check_registration`, N2/N4/N8/N10 and the forgery suite pass.
* **Failure isolation**: the process boots and serves `/health` and `/capabilities` with no configuration or editor;
  each call returns a typed failure-shaped result. Nothing else in the platform depends on it.

## Capability map

| Capability | Side effect | Gate |
|---|---|---|
| `document.connect_check` / `inspect` / `open_session` | none (inspect/open transmit under policy) | auth + classification |
| `document.convert` / `export` | new versioned artifact (PRESERVE) | auth + classification + validated output |
| `document.propose_patch` | none (proposal stored in connector state) | auth |
| `document.apply_patch` | new document version (PRESERVE) | **approval** bound to proposal id + base hash + backend + patch digest; expected version; read-back |
| `document.publish` | copy to an allowlisted destination (irreversible) | **approval**; destination allowlist; classification |

## Not implemented (designed only)

S3 `DocumentStore`; Collabora adapter; an editing (non-view) session with save callback; content controls and comments
as patch operations; shared replay/idempotency store for multi-replica; stack compose/Ansible integration (see README).

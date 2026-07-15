# Permissions plane — access-control admin

Fine-grained data access for every app: grant a **subject** (app / role / user) read/write on a
**resource** (database / table / corpus / tool), sliced by **row scope** and **column mask**. It is a
kernel plane (a control-plane primitive alongside Approvals), not a business app, so it deploys with
the core stack — no business apps required.

- **UI:** `/permissions` on the control plane (linked from a *Permissions* tile on `/overview`).
- **Code:** `agentic_os/permissions.py` (grant store + plane + preview + enforcer), wired into
  `agentic_os/control_plane.py` (routes) and `agentic_os/views.py` (page).

## Model

| Piece | What it is |
|---|---|
| `Grant` | `<subject_kind:subject>` may `<actions>` on `<resource_kind:resource_name>`, with `row_scope` (`all`/`own`/`in`) + `masked_columns`. A grant on `crm` covers `crm.customers` (dotted prefix). |
| `GrantStore` | Encrypted-at-rest JSONL CRUD (`grants.enc`), atomic writes, **fail-closed**. |
| `PermissionsPlane` | `authorize(identity, resource, action) → AccessDecision` (privileged bypass → resource grant → row scope → column mask); `slice()` applies it. |
| `make_authorizer(store)` | The **enforce** side a client app installs so grants gate every tool call. |

`authorize` combines matching grants: rows = most-permissive (`all` > `own` > `in`); a column is
masked only if **every** matching grant masks it (an added grant can unmask).

## Encryption at rest + fail-closed

Grants are stored as `grants.enc`, encrypted with **AES-GCM** (AEAD = confidentiality **and**
tamper-evidence in one). Any hand-edit of the file fails authentication on read → the store goes
**sealed** → the plane honors **no grants and denies everything, including the admin bypass**.
`add`/`remove` refuse to overwrite a sealed file.

Key precedence: **`PERMISSIONS_KEY`** (base64 16/24/32 bytes, from Vault/env) → a generated on-box key
file (`.permissions.key`, dev only). Status (`GET /api/permissions/status`) reports
`encrypted` / `key_source` (`env`|`keyfile`) / `sealed` / `count`; the UI shows a badge.

**Honest ceiling (on-host model):** a root user who can read the running key (env/`/proc`) can still
forge grants — encryption stops *file* tampering, not a fully-compromised host. Keep `PERMISSIONS_KEY`
in Vault so the key isn't on the data volume. For a *root-can't-forge* posture, move to an asymmetric
verify-only design (server holds only the public key; signing happens off-box).

## API auth (write path)

`POST`/`DELETE /api/permissions/grants` are gated by **`PERMISSIONS_ADMIN_KEY`** (its own key, so it
is independent of the fleet-ops `AGENTIC_OS_API_KEY`; falls back to it if unset). Reads/preview are
open. The UI has an admin-key field (stored in `localStorage`, sent as `X-API-Key`). This is a
**separate layer** from at-rest encryption: it protects the network write path, not the file.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/permissions` | — | Admin page (grant editor + live preview) |
| GET | `/api/permissions/status` | — | encrypted / key_source / sealed / count |
| GET | `/api/permissions/grants` | — | list grants |
| POST | `/api/permissions/grants` | `PERMISSIONS_ADMIN_KEY` | add/replace a grant |
| DELETE | `/api/permissions/grants/{id}` | `PERMISSIONS_ADMIN_KEY` | remove a grant |
| POST | `/api/permissions/preview` | — | permissionless vs permissioned over the sample resource |
| POST | `/api/permissions/authorize` | — | dry-run a decision |

## Enforcing grants in a client app

The control plane is the **write** side; a client app is the **read/enforce** side. Both share the
grants file (`PERMISSIONS_DIR`, default `/data/agentic-os/permissions`), mounted into both containers.
The app installs the authorizer once:

```python
from context_runtime.tools import set_default_authorizer
from agentic_os.permissions import GrantStore, make_authorizer

store = GrantStore("/data/agentic-os/permissions")   # same dir as the control plane
set_default_authorizer(make_authorizer(
    store,
    app_resources={"support": ("table", "crm.customers")},   # app → its own resource
    tool_resources={"read_customers": ("table", "crm.customers")},  # optional per-tool overrides
))
```

Every gated tool call is then authorized against the plane; a saved grant takes effect on the next
request. Tools that touch no controlled resource pass through.

## Deploy & secrets

The plane reads two keys from the environment — `PERMISSIONS_KEY` (encryption) and
`PERMISSIONS_ADMIN_KEY` (write authorization). Source them however your deployment manages secrets
(a secrets manager, a `.env`, CI variables). Flow:

```
secret source → PERMISSIONS_KEY / PERMISSIONS_ADMIN_KEY in the control-plane environment
  → GrantStore encrypts grants.enc with the key; the write API is gated by the admin key
```

The grants file persists on the control-plane data volume.

### Key rotation

- **Admin key** — safe, no data impact: set a new `PERMISSIONS_ADMIN_KEY` and restart the
  control-plane.
- **Encryption key (`PERMISSIONS_KEY`)** — CAUTION: the existing `grants.enc` is encrypted with the
  old key, so rotating the key alone makes it undecryptable → sealed (all access denied,
  fail-closed). Rotate by resetting the store (delete `grants.enc` and re-seed grants) after setting
  the new key. A rekey-in-place step (decrypt-with-old → re-encrypt-with-new, preserving grants) is
  not yet implemented.

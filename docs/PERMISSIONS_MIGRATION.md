# Permissions plane: migration guide

For operators who run `agentic_os.permissions` — the grant store, the control-plane permissions API, or a client
app that installs `make_authorizer` — and are upgrading to 0.4.0, the release that tightens it. Scope: this is the
**basic** permissions plane of the AGPL edition (resource grants, row/column restrictions at the tool boundary).
Multi-tenant isolation, revocation across conversations and caches, end-to-end document/row/column/graph
authorisation and identity integration are not part of it.

Licence: this kernel is source-available, not open source: AGPL-3.0-or-later with its network clause (§13 of the AGPL) plus the Commons Clause Condition v1.0
(see [LICENSE.md](../LICENSE.md), which also states the separate licence of the reference applications under `apps/`). "The AGPL edition" in this guide means this
kernel, as distinct from the optional Enterprise package.

## What changes, at a glance

| Change | Who is affected | Action |
|---|---|---|
| Tools not mapped to a resource are denied, `admin` included | Apps that install `make_authorizer` and leave a tool (or an app) unmapped | Map the tool, or list it in `public_tools` |
| Every checked tool needs a registered action (`tool_actions`) | Every app that installs `make_authorizer` | Classify each tool `read`, `write` or `delete` |
| Mutating tools need a `write`/`delete` grant | Tools registered `write`/`delete` | Grant the action; approval alone no longer suffices |
| Row/column scope needs a tool that applies it | Deployments with `own`/`in` grants or masked columns | Declare `scoped_tools` and call `slice_for` |
| Several grants are the OR of whole predicates | Callers that read `decision.row_filter` | Handle `{"op": "or", "filters": [...]}` |
| Empty identity gets nothing from `own` grants | Anonymous or user-less principals | Pass a real `user` |
| Malformed grant definitions are rejected, and **already-stored malformed grants stop applying** | Stored grants with a blank `row_column` (own/in), a blank mask column, a blank or non-string action, a non-list `actions`, or an unknown `row_scope` | Find them (`status.malformed`, the log line), delete and re-create them (section 3a) |
| Blank and non-string `in` values never match; a row with `""`/`None`/missing there is never matched | `in` grants that listed `""` or numbers; rows with empty owners | Use real string values (section 3a) |
| **Grants coexist: `add` appends, ids are random, removal and update are by id** | Clients that re-posted a grant to change it (the old add-or-replace); code that derived or guessed ids | Use `PUT /api/permissions/grants/{id}` or `GrantStore.update(id, ...)`, or remove + add (section 3b) |
| **An invalid principal grants nothing** (a malformed `user`, `app` or `roles`, as a whole) | Principals with a non-string `user`/`app`, `roles=None`, a bare-string or junk-member `roles` | Pass strings and a clean role list (section 3c) |
| Padded or comma-packed mask, action and column names make a grant unusable | Stored grants with `" email"`, `"read,write"`, `"a,b"` and the like | Delete and re-create them (section 3c) |
| Every executable tool is listed in `tool_actions`, **public tools too** (as `read`) | Apps with `public_tools` but no `tool_actions` entry for them | List each public tool as `read` (section 1) |
| Roles are validated strictly (list/tuple/set of non-blank strings, all-or-nothing) | Principals whose `roles` is a string, bytes, None, or contains a blank/non-string member | Pass a clean list of role names |
| Column masks are decided per row, per grant | Deployments with several grants that mask differently; code that reads `decision.masked_columns` | Apply `decision.rules` (`slice_for` does); see section 3 |
| No implicit plaintext storage, **no implicit key** | Installs without `cryptography`, or with neither `PERMISSIONS_KEY` nor an existing key file | Set `PERMISSIONS_KEY` (section 7a), or keep the existing key file |
| The control plane's fleet reads (`GET /approvals`, `/jobs/{id}`, `/api/fleet`, `/status`, `/notify/status`) need the fleet key, the control token or the read-only dashboard token; `/health` is minimal and `/capabilities` is trimmed for anonymous callers | Scripts, monitors and readiness checks that read them without a credential, and the dashboard (it now asks for a credential) | Send `X-API-Key`, a bearer control token or `X-Dashboard-Token` and set one of them (section 33) |
| Scanner launchers need `write` | Principals that ran the scan tools with `read` grants | Grant `write`, or leave them denied (section 9) |
| Plaintext `grants.jsonl` is migrated at start-up | Installs that stored plaintext | Nothing, or read the safeguards below |
| One lock per grant transaction (POSIX `fcntl`) | Windows, or filesystems without `flock` | Use a POSIX host / a volume that supports `flock` |
| Files 0600, directory 0700 | Control plane and apps that share the grants volume as different users | Run both as the same user, or share a group deliberately (below) |

## 1. Unmapped tools and `public_tools`

Before: a tool that was neither in `tool_resources` nor belonged to an app in `app_resources` ran with no check.
After: it is denied (reason names the tool and the fix), for every principal. `admin` still has full access to a
*mapped* resource, but no longer skips the mapping rule.

```python
# before — "ping" and "export_everything" both ran for anyone
make_authorizer(store, app_resources={"support": ("table", "crm.customers")})

# after — say what each tool is
make_authorizer(store,
    app_resources={"support": ("table", "crm.customers")},     # tools of the 'support' app are mapped to it
    tool_resources={"export_everything": ("table", "crm.customers")},
    public_tools=["ping"],                                      # touches no controlled data
    tool_actions={"export_everything": "read", "ping": "read"})   # EVERY executable tool is listed; a public one as read
```

A tool cannot be both public and mapped, or public and `write`/`delete` (`ValueError` at start-up). **A public tool is
not exempt from classification:** list it in `tool_actions` as `read` too (above, `"ping": "read"`); an unlisted public tool is
denied, with a message naming `tool_actions`. The first denial of each tool is logged with this advice.

## 2. Write authorisation: `tool_actions`

The action a tool requires comes only from the registration you pass in `tool_actions`. It is never inferred
from the tool's name, its description, or an MCP `readOnlyHint`. A tool with no entry is denied. If the tool's
own spec says `side_effecting` but you registered it `read`, the classification is ambiguous and the call is
denied (a spec can tighten, never downgrade). Approval and authorisation are separate checks: a human approving
`update_customer` does not grant the right to write.

```python
# before — a side-effecting tool needed only a read grant
make_authorizer(store, app_resources={"support": ("table", "crm.customers")})

# after
make_authorizer(store, app_resources={"support": ("table", "crm.customers")},
                tool_actions={"read_customers": "read", "update_customer": "write", "purge_customer": "delete"})
# and give the principal the matching grant:  store.add("app", "support", "table", "crm.customers", actions=["read", "write"])
```

A grant whose `actions` is `["write"]` does not imply `read`; give both when a tool needs both (classify such a
tool `write`). `agentic_os/tool_actions.py` classifies the 135 tools of the 17 apps (their registered tools and operator capabilities; the per-tool record is
[TOOL_CLASSIFICATION_INVENTORY.md](TOOL_CLASSIFICATION_INVENTORY.md)) (`tool_actions("edge-sentinel")` returns a ready mapping); a test fails when an app registers a tool that is not
classified there.

## 3. Row and column scope: `scoped_tools`, `single_predicate_tools`, OR of whole predicates

Before: the tool gate returned only allow/deny, so a grant's row filter and column mask were silently dropped.
After: a grant that carries scope is honoured only for a tool that declares it applies the decision; any other
tool is denied.

```python
# before — read_customers ran under a region-restricted grant and returned EVERY row, unmasked
# after — declare the tool and apply the decision where the data is read
authz = make_authorizer(store, app_resources=..., tool_actions={"read_customers": "read"},
                        scoped_tools=["read_customers"])

def read_customers(args):
    rows = db.query("select * from customers")
    return ToolResult(data=authz.slice_for(current_principal(), "read_customers", rows))   # rows AND masks

# write/delete tools check each target row
if not authz.permits_row(current_principal(), "update_customer", row): raise PermissionError
```

Several grants are combined as the OR of **whole** predicates (column, operator and values together). A row is
allowed when at least one applicable grant authorises it:

```
grant A: owner = <current user>            grant B: region IN ('eu')
decision.row_filter == {"op": "or", "filters": [
    {"column": "owner",  "op": "eq", "value": "alice"},
    {"column": "region", "op": "in", "values": ["eu"]}]}
```

Values are never merged across columns: `region IN ('eu')` plus `team IN ('a')` stays two predicates, never
`region IN ('eu','a')` (that merge, under the first grant's column, was the defect). A tool that reads
`authorizer.decide(...)` and pushes **one** predicate into its own query cannot express an OR: declare it in
`single_predicate_tools`; it is allowed a single predicate and **denied** a decision with several, instead of an
approximation. A tool in neither list is denied under any scoped grant. Callers that read `decision.row_filter`
directly must handle the `"or"` shape (one predicate keeps the old shape).

**Column masks are decided per row.** Before, a column was masked only if *every* matching grant masked it across
all rows, so a masking grant `region IN ('eu')` beside an unmasking `region IN ('us')` left email visible on eu
rows. Now each grant is a rule (its complete predicate and its masked columns) and, on each row, a column is
exposed iff some grant authorising that row exposes it, masked iff every such grant masks it:

```
grant A: region IN ('eu'), masks [email]       grant B: region IN ('us'), masks []
before:  email visible on eu rows AND us rows
after:   email masked on eu rows, visible on us rows     (slice_for does this; rows outside eu/us are not returned)
decision.rules == [{"row_filter": {"column": "region", "op": "in", "values": ["eu"]}, "masked_columns": ["email"]},
                   {"row_filter": {"column": "region", "op": "in", "values": ["us"]}, "masked_columns": []}]
decision.masked_columns == ["email"]     # union: masked on at least one row
```

A tool that applies the decision itself must use `decision.rules` (or `PermissionsPlane.slice` /
`masked_for_row`); one that only masks `decision.masked_columns` everywhere over-masks but never leaks.
`single_predicate_tools` are also denied when masks differ from row to row.

**Roles.** `Identity.roles` (and a principal's `roles`) must be a list, tuple or set of non-blank strings. A bare
string, bytes, `None`, another non-collection, or a collection with any malformed member is rejected as a whole:
no role-based privilege at all, including `admin`. Matching is exact.

An empty, blank or non-string `user` gets nothing from an `own` grant (other grants still apply).

## 3a. Blank values and malformed grants

**Blank values never match.** An `in` scope ignores blank, whitespace-only and non-string listed values (so `in owner ("", "ana")`
matches only `ana`, and `in owner ("", "  ")` matches no row but is still an allowed grant). A row that lacks the column, or
holds `None`, `""` or only whitespace there, is never matched by `in` or `own`, in `slice()`, `row_allowed()`,
`masked_for_row()` and the tool gate. Numbers are not coerced: a grant that listed `[1, 2]` (the API accepts JSON numbers) now
matches nothing; list them as strings (`["1", "2"]`).

**Exact matching.** `in`, `eq`, ownership, role names and column/mask names are compared **exactly**: `EU` does not match `eu`, `" ana "` is not `ana`, a mask on `email` does not mask `Email`. This is a **behaviour change**: `in` used to lower-case both sides, so a grant `in region ("acme")` also matched `ACME`; it no longer does. Nothing in the plane, `slice()`, `row_allowed()`, `masked_for_row()` or the UI folds case. A business field that needs case-insensitivity must be normalised by an explicit, trusted schema-level rule **before** authorisation is evaluated (store `region` in one canonical case, normalise the user's identity the same way); the plane will not guess. (The Enterprise package's grant evaluation is also exact.)

**Malformed definitions fail closed.** `GrantStore.add` rejects the whole grant with `GrantError` (a `RuntimeError`: the control
plane, the enterprise router and the demo answer **409**; the message names the field, never its content) for: an `own`/`in`
grant with a blank or non-string `row_column`; a `masked_columns` list with a blank or non-string member; an `actions` value that
is not a list of non-blank strings; `in` values that are not a list; an unknown `row_scope`. The showcase no longer has a visitor overlay (section 13).

**Already-stored malformed grants stop applying.** A grant read from an existing file with any of those defects is unusable: it
is **not honoured, not even its valid members** (an `actions` list `["read", ""]` grants nothing, not `read`), counted in
`status()` as `malformed`, and logged without its contents (`N stored grant(s) are malformed and are ignored`). It stays in
the file untouched (a later `add` or `remove` of another grant does not drop it) until an admin deletes it by id
(`DELETE /api/permissions/grants/{id}`; `GET /api/permissions/grants` lists only usable grants, so take the id from the
file or from your records). Delete and re-create it correctly. Expect an access change: a malformed all-rows grant that used to
widen a principal's access no longer does.

## 3b. Grants coexist: ids, the grants route, update, seed

* **Ids.** A grant id is `g-` + 128 random bits, assigned once and never derived from the subject or resource. Ids written by
  earlier releases (a 32-bit hash, e.g. `g-1a2b3c4d`) are carried over unchanged and still work, including removal by that id.
* **`add` appends.** It never replaces a grant because kind, subject and resource match: a role may hold several grants on one
  resource (`owner = current_user` **and** `region IN ('eu')` are OR-ed by the plane) and every grant keeps its own masks. Two
  identical adds make **two** grants, on purpose (the lock covers each transaction; nothing is silently merged or dropped).
  There is no `dedupe` option: it could hide a legitimate pair (two identical grants are harmless, one lost grant is not).
* **Routes.** `POST /api/permissions/grants` **creates** (no add-or-replace) and returns the grant with its id;
  `PUT /api/permissions/grants/{id}` (new) updates the scope fields (`actions`, `row_scope`, `row_column`, `row_values`,
  `masked_columns`) of that one grant atomically, keeping its id, subject and resource (404 for an unknown id, 409 for a malformed
  result); `DELETE /api/permissions/grants/{id}` removes that one grant. A client that **re-posted to change a grant** now
  creates a duplicate: switch it to `PUT` (or `remove` + `add`, which leaves a moment with no grant: fail-closed). The enterprise
  router inherits `POST`/`DELETE` unchanged; it does not have `PUT` until the Enterprise package adds it.
* **Python.** `GrantStore.add(...)` returns the new `Grant`; `GrantStore.update(id, **fields)`; `remove(id)`; an optional
  `stable_id="my-token"` names a grant with a fixed token (letters, digits, `. _ : -`, at most 128 characters; an id already
  taken is refused, never overwritten).
* **`seed`.** Idempotent for restarts by an **emptiness check**, not by content: it seeds only a store with no entries at all
  (malformed entries count), under the lock, so a restart or a second worker adds nothing and an admin's removals are not
  undone. Seed dicts may carry an `id` (a stable token, recommended: the shipped control plane and demo use `seed-...` ids); ids
  in one batch must be distinct. Two seeds for one subject and resource both land.
* **The demo** no longer simulates grants per visitor (section 13): there is nothing to cap or evict.
* **Rollback.** A release that predates this change reads the new ids as stored and removes by them; its `add` computes the old
  hash id and would still replace only a grant with that hash id, so after a rollback do not re-add a grant to change it.

## 3c. Invalid principals, padded names, and `public_tools`

* **An invalid principal grants nothing.** `Identity.is_valid()` / `invalid_reason()` (and the tool gate) validate the principal
  as a whole: `app` and `user` must be strings (blank means absent), `roles` a list, tuple or set of non-blank strings. A
  malformed field (`user=42`, `app=["x"]`, `roles=None`, a bare-string `roles`, a junk member) makes the principal invalid: no
  grant is honoured through any field, **no `admin` bypass**, nothing raises. An identity with a malformed `user` but a valid
  role (`user=42` + `clerk`) is now **denied** (earlier revisions normalised field by field and still granted through the role).
  A tool gate whose principal object carries `user=None` or `roles=None` (a common dataclass default) now denies: pass `""` / `()`.
  A public tool, which touches no data, is still callable by any principal.
* **Padded or comma-packed names are malformed.** A mask member, an action member or an own/in column name with leading or
  trailing whitespace (`" email"`) or a comma (`"a,b"`, `"read,write"`) makes the grant unusable: rejected whole at `add`, ignored
  when stored (counted in `status().malformed`, logged without contents). Three of these were fail-open before (`" email"` masked
  nothing and exposed the column). Delete and re-create such grants with clean names.

## 4. Encryption and keys

* No implicit plaintext, no implicit key. The key rule: (a) `PERMISSIONS_KEY` (base64, 16/24/32 bytes) set and valid;
  (b) an **already existing** valid key file (`PERMISSIONS_KEY_FILE`, else `<dir>/.permissions.key`), kept in use and
  logged; (c) neither: start-up raises `GrantStorageError` naming the fix, before any grants file is touched. An invalid
  `PERMISSIONS_KEY` or a damaged key file is fatal, never replaced.
* `PERMISSIONS_REQUIRE_KEY=1` is stricter than the rule and still supported: only `PERMISSIONS_KEY` is accepted, so
  an on-box key file is refused as well (for deployments whose key comes from a secret store).
* A second explicit, development-only opt-in, `PERMISSIONS_ALLOW_GENERATED_KEY_DEV=1`, generates a key and creates a 0600
  key file (loud warning). Like the plaintext opt-in, no file this repository ships sets it, and a test fails if one
  ever does.
* The only route to plaintext (as distinct from a generated key) is the development-only `PERMISSIONS_ALLOW_PLAINTEXT_DEV=1` (a loud warning at
  start-up and on every write; `status` shows `plaintext_dev: true`). It never downgrades an existing
  `grants.enc`. The earlier name `PERMISSIONS_ALLOW_PLAINTEXT` is not recognised. Do not put the setting in a
  compose file, image or shared environment.
* `deploy/sidekick-devops` and the control plane build their store at import: set `PERMISSIONS_DIR` to a
  writable directory (the shipped Dockerfile and compose files already do).

## 5. Plaintext migration (automatic, with safeguards)

If `grants.jsonl` exists and a key is available, start-up migrates it:

1. read it without following a symlink; parse and validate;
2. **back up the original** byte for byte to `grants.jsonl.migrated-<epoch>-<hex>` (0600, created exclusively);
3. encrypt into a temp file in the same directory (0600), `fsync`;
4. verify by decrypting and comparing; `os.replace` onto `grants.enc`; verify the file on disk;
5. remove the plaintext original and set the directory to 0700; keep the backup.

| Situation | Behaviour |
|---|---|
| any error at any step | `GrantStorageError`; the existing data is unchanged (the temp file, a half-made `grants.enc` and the backup made by this attempt are removed) |
| a crash (the process dies) at any step | `grants.enc` is absent or complete and verified; `grants.jsonl` is the original or gone only after `grants.enc` verified; the data never disappears; at worst a stray temp file or backup remains |
| key unavailable (no `cryptography`; no `PERMISSIONS_KEY` and no existing key file; an invalid key) | start-up fails **before** any grants file is touched; bytes unchanged |
| `grants.jsonl` or `grants.enc` is a symbolic link | refused; the link target is never read or written |
| `grants.jsonl` does not parse | refused; left untouched |
| `grants.enc` and `grants.jsonl` both exist | `grants.enc` wins; a warning flags the stale plaintext file (not deleted) |
| `PERMISSIONS_MIGRATE_PLAINTEXT=0` | start-up refuses instead of starting with no grants |

The permissions plane's logs name paths, counts and exception types — never keys, key material or grant contents. (The Agent Gateway's server log is different: it holds the raw text and traceback of a failing capability by design, see section 19.)

Before / after:

```
before:  PERMISSIONS_DIR/grants.jsonl                 (plaintext, mode 0644)
after:   PERMISSIONS_DIR/grants.enc                   (AES-GCM, 0600)
         PERMISSIONS_DIR/grants.jsonl.migrated-1761600000-a1b2c3   (plaintext backup, 0600 — delete once confirmed)
         PERMISSIONS_DIR/.grants.lock                 (0600)
```

**Shared volumes.** The directory is created 0700 and files 0600. If the control plane (writer) and a client app
(reader) share the volume and run as different users, the reader loses access. Run them as the same user, or
choose and apply a group-based mode deliberately; the store does not widen modes on its own.

## 6. Locking

`add`/`remove`/`seed` hold one exclusive `flock` on `.grants.lock` across the whole read-modify-write and write
through a temp file, `fsync` and `os.replace`: no lost update (a removal included), no torn file, readers lock
nothing. POSIX `fcntl` is required; a platform without it (Windows) **raises at construction** instead of running
unlocked. Network filesystems that do not implement `flock` correctly cannot give this guarantee: keep the grants
volume on local or properly locking storage.

## 7. Rollback

* **Back to the previous release, encrypted file kept.** The previous release reads `grants.enc` if it has
  `cryptography` and the **same** key (`PERMISSIONS_KEY`, or the same `.permissions.key`). Keep the key. Files
  are 0600 and the lock file and `*.tmp` leftovers are ignored by it.
* **Previous release with no key or no `cryptography`** looks for the plaintext `grants.jsonl` and ignores
  `grants.enc`: it starts with no grants (everything denied), and its next write would create a new plaintext
  `grants.jsonl`. To return to plaintext: stop the service, delete `grants.enc`, rename the
  `grants.jsonl.migrated-*` backup back to `grants.jsonl`, start the old release. Grants edited after the
  migration are not in that backup.
* **Forward again.** Start the new release: the restored `grants.jsonl` is migrated again.
* A grants file written by the new release cannot be opened by it if the key changes; it fails closed (no grants,
  `sealed: true`) and is never overwritten.

## 7a. Deployments with neither a key nor a key file

**What breaks:** a deployment whose store directory has no `PERMISSIONS_KEY` in its environment and no
`.permissions.key` (a fresh volume, a container recreated without its volume, a test environment that relied on the
old implicit generated key) now refuses to start: the control plane, the permissions showcase, and anything that builds a
`GrantStore` at import raise `GrantStorageError`. Nothing is generated and nothing on disk is touched.

**Fix, pick one:**
1. *Provide a key (recommended).* `export PERMISSIONS_KEY=$(python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())")`
   and keep it in your secret store; pass it to the container (`--env-file` / `environment:`). Add
   `PERMISSIONS_REQUIRE_KEY=1` in production. Do not rotate it casually: a different key cannot open the existing
   `grants.enc` (the store seals and denies everything, and never overwrites it).
2. *Keep an existing generated key.* If the volume still holds `.permissions.key`, do nothing: the store keeps using it
   and logs that a key file is in use. Make sure that volume persists. If a container was recreated **without** its
   volume, the key (and the grants) are gone: provide a new key and re-create the grants.
3. *Single user on one machine:* `agentic-os permissions init` (section 11) creates the key file and the grants once, on purpose.
4. *Development only:* `PERMISSIONS_ALLOW_GENERATED_KEY_DEV=1` (a loud warning) or `PERMISSIONS_ALLOW_PLAINTEXT_DEV=1`.
   Never in a shipped file.

**Shipped deployment paths now provision a key through an explicit step, or stop with the fix.** The key is an external secret
with **no default** (never a literal, never a shared key, never an empty default that silently works):

| Path | What it needs now | Setup step |
|---|---|---|
| Root `docker-compose.yml`, `deploy/docker-compose.yml` (control plane) | `PERMISSIONS_KEY` is a **required** variable (`${PERMISSIONS_KEY:?…}`; `docker compose config` stops with the message while it is missing); the compose files mount `/data/permissions` for the grants. The root `Dockerfile` as tagged at v0.4.0 does not build (it does not copy `hatch_build.py`); `main` after that tag fixes it. The compose stack itself was not started as part of the release | `make permissions-key` (or `agentic-os permissions key --env-file permissions.env`), then `docker compose --env-file permissions.env up` |
| `make run` (local `uvicorn`) | a key in the environment, `PERMISSIONS_KEY_FILE`, or `permissions.env`; otherwise it stops and names `make permissions-key`; `PERMISSIONS_DIR` defaults to `.local/permissions` | `make permissions-key` |
| Runtime Helm chart (`deploy/reference-system/redevops-runtime`) | `permissions.keySecret` (the name of a Secret holding `PERMISSIONS_KEY`); the chart **refuses to render** while it is empty (rendered with Helm 3.16.2 in a container and with helm v4.3.1 in a separate check; the chart was not installed on a cluster, and the runtime image it pulls is published separately from this repository) | `agentic-os permissions key --env-file permissions.env --k8s-secret NAME`, run the `kubectl` command it prints (`--from-env-file`, the key is never on a command line), delete the file, set `permissions.keySecret=NAME` |
| Permissions showcase compose | `PERMISSIONS_KEY` required. A **running** demo keeps its key: `agentic-os permissions key --env-file permissions.env --from-key-file <volume>/.permissions.key` writes the **same** key as an environment key, so the existing grants still open | as above |
| Enterprise Sidekick bundle (compose, Helm) | the same rule; apply it to the Enterprise package's own bundle (that bundle is not part of this repository) | as above |

`agentic-os permissions key --env-file F` writes `PERMISSIONS_KEY=<32 random bytes, base64>` to a new 0600 file (exclusive
create, no symlink, **never overwritten**, a different key every run) and never prints the key. A different key cannot open a
volume's existing `grants.enc`: the control plane then refuses to run (sealed), and nothing is overwritten. Docker Compose can be
checked without starting anything: `docker compose config` stops with the message while the key is missing or empty.

## 8. Checklist

- [ ] every tool is mapped (`tool_resources` / `app_resources`) or in `public_tools`
- [ ] every checked tool is in `tool_actions`; principals hold the matching `write`/`delete` grants
- [ ] tools under row/column grants are in `scoped_tools` (or `single_predicate_tools`) and call `slice_for`
- [ ] code that reads `row_filter` handles `{"op": "or"}`
- [ ] `PERMISSIONS_DIR` set and a key provisioned: `PERMISSIONS_KEY` (preferably from a secret store, with `PERMISSIONS_REQUIRE_KEY=1` in production) or an existing key file
- [ ] neither `PERMISSIONS_ALLOW_PLAINTEXT_DEV` nor `PERMISSIONS_ALLOW_GENERATED_KEY_DEV` appears in any shipped configuration
- [ ] the grants volume is on a POSIX filesystem with `flock`; readers and writers run as a user that can open 0600 files
- [ ] after the first start: confirm `status.migrated_from_plaintext`, then delete `grants.jsonl.migrated-*`

## 9. Scanner tools and the `execute` question

The scanner tools are classified by what their launchers actually do, not by their `side_effecting` flag. `read` means:
returns existing state; any process it spawns has a fixed argv with a read-only subcommand; no file is written and no work
is started. Anything that starts a scanner against a caller-influenced target, writes files (a scanner's cache), downloads
data or pulls images, or directs another service to scan is **`write`**: `supply_chain_scan`, `list_vulnerabilities` (starts the
scan when nothing is cached), `scan_app`, `scan_apps`, `sbom`, `malware_scan` (and the already-flagged `scan_deployment`).
`list_scannable_apps`, `scanner_status`, `explain_vulnerability`, `scan_status` and `scan_results` stay `read`. A tool whose
effects cannot be established is left out of `agentic_os/tool_actions.py` and is denied until someone classifies it.

Consequence for a user with only read grants: the scan launchers are denied; the status, explanation and listing tools
work. Grant `write` on the app's resource to allow scans (`agentic-os permissions init --include-write` does this for the
local user). A `write` grant on a resource also covers that resource's other write tools: grants are per resource, not per tool.

There is **no `execute` action**, and none was introduced: the engine can enforce any flat action string, but the boundary
between "launches a process" and "writes" is not consistent here (the scanners write caches, `scan_deployment` both runs a
probe and has external effects, `ban_ip` mutates through a process), and a tool registers **one** required action, so
"execute and write" cannot be expressed. Process-launching tools are therefore write-grade. Adding `execute` needs
multi-action registration first.

## 10. Execution profiles: authorisation and approval are separate

Authorisation (`make_authorizer`) and human approval (the harness `ApprovalPolicy`) are two checks; removing the admin
bypass makes nothing wait for a human click. Unattended runs are made to work by configuration, never by weakening either
check: `agentic_os/permissions_profiles.py` (`build_gate`, `DeterministicApprover`, `ApprovedOperation`,
`SimulatedEffects`, `simulated_effects`, `provision_demo_grants`, the `DEMO_*` principals).

| Profile | Authorisation | Approval of side-effecting tools | Side-effecting tool bodies |
|---|---|---|---|
| **production** | enforced; grants from the store | a human approver you supply; none supplied means every gated call is denied; a list of auto-approved operations is refused | the real body |
| **demo** (unattended) | enforced; limited, visible demo grants (`created_by="demo-fixture"`) | deterministic: auto-resolved only for the explicitly listed `ApprovedOperation`s (each with a written reason and an optional argument predicate); everything else is denied immediately, never waited on | a recorded **stub**, never the real body (`gate.registry()`), unless the tool is named in `build_gate(safe_synthetic=[...])` |
| **test** (automated) | enforced; synthetic grants | the same deterministic approver; never prompts | the same recorded stub |

**A deterministic approval decision is not a simulation of a side effect**: approving `ban_ip` does not make `ban_ip`
harmless. So, in the demo and test profiles, the registry the profile builds (`gate.registry()`) swaps every side-effecting tool
(`side_effecting` in its spec, or classified `write`/`delete`) for a recorded stub when it is registered. The stub keeps the tool's spec
(approval and authorisation still apply) and records what would have happened in `gate.effects` (`kind` = the tool name, argument names
only); the real body is never run, even with an `ApprovedOperation` and a write grant. The one exception is `safe_synthetic`: trusted
metadata the registering code passes to `build_gate`, saying that a body only touches synthetic fixtures; a tool cannot mark itself
safe through its own spec. The profile does not know what a body does, so a registry built by hand (not through `gate.registry()`)
is not covered; `simulated_effects(registry, tools)` applies the same substitution to such a registry explicitly. Production is unchanged.

There is no switch that disables authorisation, governance or approval, and no blanket auto-approval (a test asserts the
signatures carry none). What the repository already had: the harness `ApprovalPolicy` with an `approver` callable (modes
`deny_side_effects`, `allowlist`, `bypass`) for the tool gate, and the Agent Gateway's own `ApprovalPolicy` enum
(`AUTO`, `IF_POLICY`, `REQUIRED`, `MANDATORY`) with standing grants for missions. The profiles module extends the first
with a named, deterministic approver whose decisions are logged, and the substituting registry; it does not use `bypass`. The Agent Gateway has its own
plane and is not affected (section 12).

**Scanners are approval-gated too**: the six scanner launchers are classified `write` **and** registered
`side_effecting=True` in `apps/edge-sentinel/app.py`, so the approval policy applies in addition to the write grant. A live
console with `allow_side_effects=[]` therefore waits for a human before starting a scan; the demo/test profiles do not auto-approve
one unless it is listed as an `ApprovedOperation`, and then only a stub runs.

## 11. Quickstart for one user (`agentic-os permissions init`)

Note: basic permissions authorise the app INSTANCE, not callers: an instance with write grants lets every caller who can reach it write (section 29).

```
agentic-os permissions init [--dir D] [--user local] [--include-write]
export PERMISSIONS_DIR=D            # the printed next step; the key file is found there
agentic-os permissions list [--dir D]
agentic-os permissions revoke <grant-id> [--dir D]
```
`init` creates the persistent key (`.permissions.key`, 0600, once, never replaced) and one visible, revocable grant per
**bundled** tool (`agentic_os/tool_actions.py`) for the local user on the resource `<app>.<tool>`; read-class tools get
`read`, and write-class tools (state changes, scanner launchers) get `write` only with `--include-write`. Third-party
tools are not mapped and are denied. `agentic_os.permissions_setup.load_gate(dir)` returns the production-profile gate and the
local principal. For anything beyond one machine provide `PERMISSIONS_KEY` from a secret store and set
`PERMISSIONS_REQUIRE_KEY=1`. A fresh-install walkthrough runs end to end in `tests/test_demo_compatibility.py`.

## 12. Demo compatibility

Which demos and examples touch the plane, and what happens to each on this release: only the permissions showcase and the control plane build a `GrantStore`; the Agent
Gateway, the Sidekick capability layer, `examples/` and the apps do not use this plane; the enterprise Sidekick bundle
builds a `GrantStore` at import. `tests/test_demo_compatibility.py` (and `.github/workflows/permissions-demo-compat.yml`)
start each documented path with synthetic data, run its workflow with no human, and assert that the intended operation
succeeds and the unauthorised ones stay denied.

## 13. Admin API, public demo API and grant-store robustness

**Breaking.** The permissions API is two interfaces now.

* **Admin API** (control plane, the showcase's admin routes; the enterprise router's inherited routes are patched separately): `GET status`, `GET
  grants`, `POST preview`, `POST authorize`, `POST`/`PUT`/`DELETE grants` **all require the admin credential**. No credential, a wrong, empty or
  malformed one: 401. No admin key configured on the server: 503 (the routes are disabled; there is no "open when no key" branch). A credential in
  the query string: 400. Constant-time comparison. `X-API-Key` or `Authorization: Bearer` (`PERMISSIONS_ADMIN_KEY` ONLY, at least 24 characters: neither `AGENTIC_OS_API_KEY` nor the control token administers grants); HTTP
  Basic and cookies are not credentials. **Clients must send the key on every call, reads included**, and must stop reading grants, status,
  preview or authorize anonymously: a script that did `curl .../api/permissions/grants` now gets 401. The control-plane admin page already sends it.
* **Public demo API** (showcase only): `GET /api/demo/scenarios`, `GET /api/demo/scenarios/{id}`: fixed, synthetic, allowlisted scenarios; no
  identities, roles, resources or actions accepted; never a real grant, principal, predicate, decision or store status. The old per-visitor
  overlay, its cookie and the anonymous read routes are removed. `/healthz` is liveness only.

**Seen by a deployment:** the showcase compose and `deploy.sh` now start with `--env-file permissions.env`, which should also carry
`PERMISSIONS_ADMIN_KEY`; without it the admin API is disabled (503) and the page's admin panel says so. The cloudflared rule must also route
`/api/demo/*`. `/api/permissions/status` is no longer a health probe: use `/healthz`.

The sealed/unsealed state is per operation (inside the locked transaction); a failed read aborts a write. The grants lock wait
is bounded (default 10 s, `PERMISSIONS_LOCK_TIMEOUT`); the error leaves the data intact. `actions: []` is rejected at add/update and
unusable when stored (it no longer becomes `["read"]`); only an absent or null `actions` defaults. `seed()` runs at most once per store
(a marker inside the encrypted payload), so revoking everything is not undone by a restart. An existing key file wider than 0600 or owned by
someone else is refused (`chmod 600` and own it, or provide `PERMISSIONS_KEY`; a mounted secret is often 0444 root-owned: use the environment
variable there); only the explicit generated-key dev opt-in uses it, with a warning. Exact matching is described in section 3a. A malformed
request is a clean denial or a 400, never a 500; bodies over 64 KB are 413. `remove_count(id)` reports how many entries went; legacy
duplicate ids are all removed with a log line.

**Backup and rollback.** While a `grants.jsonl.migrated-*` plaintext backup exists every start warns; `agentic-os permissions migrate
--purge-backup --dir D` verifies that the encrypted store opens, overwrites the backup in place, fsyncs and removes it (no symlink is followed;
journals, snapshots and copies elsewhere are not reached). Each write now bumps a counter inside the encrypted payload and records it in a sidecar
(`grants.counter` next to the store, or `PERMISSIONS_COUNTER_FILE` on another volume); an **older** store than the sidecar recorded is refused with a
clear message and an explicit recovery, `agentic-os permissions accept-rollback --dir D`. A MISSING sidecar next to a store above its first
write (counter > 1) is refused too (fail closed, nothing honoured, writes refused); it used to count as "floor 0", so an older store plus a deleted
sidecar passed. Recovery after you moved or lost the sidecar on purpose: `agentic-os permissions recover-counter --dir D --confirm` (verifies the
store opens, refuses when a sidecar exists, records the store's own counter; without `--confirm` it changes nothing). A pre-counter store (written
by an older release) and a store at its very first write are still accepted; a garbled or symlinked sidecar fails closed. The documented plaintext
rollback (section 7) also deletes `grants.counter`. **Recommended deployment: keep the sidecar off the data volume** (`PERMISSIONS_COUNTER_FILE` on a
different volume or host path): a snapshot restore of the data directory is then detected. Residual risk, unchanged: with the default placement a
snapshot that restores the whole directory restores the sidecar too. Adopting an off-volume sidecar on an existing store needs one `recover-counter
--confirm`.

Profiles substitute stubs for side-effecting bodies (section 10); a public tool that declares side effects is denied; the six
scanner launchers are registered `side_effecting=True`, so approval applies in addition to the write grant. No dynamic value reaches an
inline handler; escaped for attribute context. `.dockerignore` excludes every key-bearing path and `make permissions-key` defaults to
`~/.config/agentic-os/permissions.env`. `deploy.sh` has `--init-key`, stops when the host has no key, excludes key and env files from
`rsync --delete`. CI installs `agent-harness` from a commit.

**Dispatch paths.** Besides the harness `ToolRegistry`, the `books`, `billing` and `compliance` consoles dispatch 13 tools through
`assistant.Assistant`; they are now classified and `Assistant(authorizer=...)` consults the same seam (an authorizer that raises is a denial).

## 14. Gated app start-up, console gating, grant administration, pages, and the identifier contract

**Apps start gated or not at all.** Every app that joins the enterprise permissions plane now calls `agentic_os.app_gate.install_gate("<app>")` at
import instead of `try: bootstrap(...) except Exception: <ungated>`. Permissions are *requested* when `CR_PERMISSIONS` is set to anything but an explicit
off value (`0`, `false`, `no`, `off`, empty) or any `CR_IDENTITY_*` variable is set. When requested, a missing enterprise package, a bootstrap too old to take
`tool_actions`, a table that cannot be resolved, ANY exception from `bootstrap` and a bootstrap that installs no process-wide authorizer are a
`GateStartupError`: the app refuses to start and the message names the cause. Not requested: the basic default is unchanged (no enterprise package = ungated;
other failures are logged). The classification table of an app is resolved once (`agentic_os.tool_actions.resolve_tool_actions`): the kernel's table for the tools
the app registers plus the enterprise `fleet_tool_actions` of the same app, merged, never copied; a disagreement is an error. **Image builds:** the app
Dockerfiles install the kernel from a pinned `KERNEL_REF` tag; an image built from this app code needs a kernel release that contains `agentic_os.app_gate`
(nothing in this change moves a pin).

**Consoles are gated.** The books, billing and compliance consoles use their own `authorizer=` or else the process-wide default authorizer the
enterprise bootstrap installs (read at call time, the same seam as a harness `ToolRegistry`); with permissions requested and no authorizer installed they deny.

**Grant administration is its own privilege.** Only `PERMISSIONS_ADMIN_KEY` (at least 24 characters) opens `/api/permissions/*`; neither
`AGENTIC_OS_API_KEY` nor the control token does, and a shorter key answers 503 like no key (one log line, never the value). Wrong credentials are throttled per
client: 10 in 5 minutes, then 429 with `Retry-After` (a missing credential is not counted; a locked client is locked for good guesses too). The client is the
connection peer; set `PERMISSIONS_TRUSTED_PROXIES` (addresses or CIDRs) when a proxy fronts the service, and only then is `X-Forwarded-For` believed (rightmost
untrusted hop). Without it everything behind one proxy shares one lockout.

**Rollback protection.** See section 13: a missing sidecar next to a store above counter 1 is refused; `agentic-os permissions recover-counter --dir D
--confirm` re-establishes it. Keep the sidecar off the data volume (`PERMISSIONS_COUNTER_FILE`).

**Pages and the admin API.** The control-plane `/permissions` page keeps the admin key in memory only: nothing is sent until the
operator presses Unlock (never per keystroke), the field is cleared, reloading forgets the key. Both pages carry a strict CSP with a per-response nonce, `X-Frame-Options:
DENY`, `nosniff`, `Referrer-Policy: no-referrer`; the admin API answers `Cache-Control: no-store`. There are no inline handlers or style attributes. Trailing-slash
redirects are relative (the showcase answers 404); `/docs`, `/redoc` and `/openapi.json` are off (the control plane serves `/openapi.json` only to the fleet
credential); the 503 body names no variable (the fix is logged). The showcase starts and serves the synthetic API with no `PERMISSIONS_KEY` and builds the real
store on the first authenticated admin request. The pages say what the plane does: it enforces where an application installs the authorizer over its tools; the AGPL
plane gives basic resource permissions and governed tool access suitable for individual and trusted small-team deployments.

**The sidekick MCP server.** `deploy/sidekick-devops/mcp_server.py` is stdio by default; `MCP_TRANSPORT=http` needs `MCP_BEARER_TOKEN` (24+ characters) or it
refuses to start; it binds `127.0.0.1` unless `BIND_HOST` is set; the host's ambient AWS credentials are used only with `SIDEKICK_MCP_ALLOW_AWS=1`.

### The identifier contract (breaking validation change)

An *identifier* (app, user, role, tenant, grant subject, resource name, owner and row column, mask column, action) is a non-empty string with **no leading or
trailing Unicode `White_Space`** and **no control (`Cc`), format (`Cf`), surrogate (`Cs`), unassigned (`Cn`, including noncharacters), private-use (`Co`), line-separator (`Zl`, U+2028) or paragraph-separator (`Zp`, U+2029) code point anywhere**, and none
of the bidirectional formatting controls (U+061C, U+200E/F, U+202A..202E, U+2066..2069). There is **no case folding and no Unicode normalization** during authorization:
`é` (U+00E9) and `e` + U+0301 are two different identifiers and two different principals; so are two names that merely look alike. Non-ASCII letters, combining marks,
plain emoji stay valid; private-use characters and U+2028/U+2029 do not (the previous release accepted them). A ZWJ emoji sequence contains U+200D (a format character) and is not valid. A *present* identifier that breaks the
rule makes the principal invalid as a whole (nothing granted, no admin bypass), like any other malformed principal; an empty or all-white-space field is still just
"absent". Row `in` values are data, not identifiers, and stay exact.

The forbidden code points come from one generated table, `agentic_os/identifier_ranges.json` (**Unicode 17.0.0**, generated by the kernel-owned `scripts/generate_identifier_ranges.py` from the pinned UCD files; the table's SHA-256 is `5cb60e8d4c97612146bcd7df69a9aca0f31f1ac7f457ac97d4ae993902b8fc45` and its inputs are recorded in `agentic_os/identifier_ranges.provenance.json`), and never replaced by the interpreter's `unicodedata` (so the Python version cannot decide whether an id is valid). The file lists `forbidden` (Cc, Cf, Cs, Cn, Co, Zl, Zp and the bidi controls, merged ranges) and `white_space` (the Unicode property, used only for the leading/trailing rule). U+2028 / U+2029 (Zl/Zp) and private-use code points are rejected anywhere. See [IDENTIFIER_CONTRACT.md](IDENTIFIER_CONTRACT.md) for the full rule and the provenance record. It is data derived from the public Unicode Character Database (Unicode License v3). **Migration:** a
stored grant whose subject, resource name, action, mask column or row column breaks the rule becomes unusable, exactly like any other malformed grant: it authorises
nothing, `status()["malformed"]` counts it, and an admin removes it by id (`DELETE /api/permissions/grants/{id}`); new grants are refused with a 409. Identity providers
that put a zero-width or bidi character in a `sub` or role name will find those principals denied until the provider is fixed.


## 15. The feature boundary: basic (AGPL) permissions need no enterprise package

**Two different things.** *Basic permissions* are a feature of this repository: grants over tools and resources, the trusted `tool_actions` table, basic row/column
restrictions within their documented scope (`scoped_tools`, `single_predicate_tools`) and a local single-user identity, all through `PermissionsPlane` / `GrantStore` /
`make_authorizer`. *Enterprise identity and permissioning* (SSO / tenant identity, `CR_IDENTITY_*` including `CR_IDENTITY_MODE`, the `CR_PERMISSIONS` bootstrap, tenant
authorization, permission-aware retrieval and cache lifecycle) are Enterprise capabilities. `agentic_os.app_gate.install_gate(app)` keeps them apart:

| requested | enterprise package absent (import blocked) | enterprise package present |
|---|---|---|
| nothing | AGPL governance, approvals, budgets and audit work; the app's tools are not authorised (the basic default, unchanged) | unchanged |
| **`AGENTIC_OS_PERMISSIONS=1`** (basic) | the kernel authorizer over the app's trusted tool table; the principal is the **local profile**; no admin bypass; no grants yet = the app **starts restricted** | the same (basic never needs the package; its tool table is merged only to be cross-checked) |
| `CR_PERMISSIONS` / any `CR_IDENTITY_*` / `AGENTIC_OS_PERMISSION_AWARE_RETRIEVAL=1` / `AGENTIC_OS_PERMISSION_AWARE_CACHE=1` | `EnterpriseCapabilityMissing`: the app refuses to start and names the capability, the variable and the fix | the enterprise bootstrap runs; any failure refuses to start |

**Basic mode in practice.** `agentic-os permissions init [--user U] [--include-write]` creates the key, the local profile (`profile.json`: the user, non-secret, 0600) and
one visible, revocable grant per bundled tool (reads by default; `--include-write` adds write tools); it prints the next step `export PERMISSIONS_DIR=... AGENTIC_OS_PERMISSIONS=1`.
The principal of an app is that profile user, with no roles: **never a request header** (`X-User`, `X-Roles`, `Authorization` change nothing) and **no admin bypass**
(`privileged_roles` is empty; a role called `admin` grants nothing). With nothing provisioned the app **starts restricted**: it runs, every protected operation is denied
with a message pointing at `agentic-os permissions init`, and nothing protected runs without its grant. That is the ONLY reason an app starts without a gate: a key that is
provisioned but cannot be used (a key file wider than 0600, a profile naming an invalid user, a conflicting tool table, a store that will not open) refuses to start.

**Where it is enforced.** Authorisation is checked where execution happens, never only where a tool is advertised: the harness registry and the `Assistant` consoles
(through the process authorizer), `Operator._execute` (every capability of a gated app, whoever calls `/invoke` or runs in-process), every route and console intent that
executes a tool (`/agent/run`, `/api/run`, `/api/chat`, `/api/agent`, `/api/discover`, the reel and vibexgen routes, the read APIs that return business data). A call that needs
two grants needs both (a `push` needs `assistant.push_assets` as well as the action). Authorisation and approval stay separate: a write grant lets the capability STAGE and, with the
operator approval the core still demands, execute; an approval never substitutes for a grant. In the enterprise mode nothing new is enforced on the routes (the enterprise gate keeps its own seam).

**Tool classification (kernel side, no enterprise package).** Classified from what the code does, with the registered `side_effecting` flag as a cross-check, and the optional Enterprise package's tool table compared once (never imported or copied): its overlapping entries agree with the kernel table and none is classified only by the Enterprise table. **The kernel table lists 64 tools for the ten apps and 135 in total.** The table is cross-checked only in the enterprise mode, only from an installed distribution, and can only add denials (section 18). One kernel-side disagreement with a registration flag: `compliance.scan` is registered non-side-effecting but runs the oscap scan script, so it is `write`. Anything not
listed is unclassified and denied. Not covered by the seam: `sky`, `video-ad` and `zoning` ship an operator but no `app.py` (nothing installs a gate for them). (Edge-sentinel's case, network, backup and permission routes are gated; section 18.)

Per-app inventory of the ten apps that execute tools through routes and operators (evidence = where the tool is declared; every row is also a test in `apps/test_basic_permissions_apps.py`):

| app | tool | action | approval needed | evidence | execution paths covered |
|---|---|---|---|---|---|
| control-tower | `bi.summary` | `read` | no | apps/control-tower/operator.py:28 | operator /invoke; GET /api/activity; GET / |
| control-tower | `bi.ask` | `read` | no | apps/control-tower/operator.py:36 | operator /invoke; GET /api/ask; POST /agent/run ask |
| control-tower | `bi.refresh` | `read` | no | apps/control-tower/operator.py:44 | operator /invoke; POST /agent/run refresh |
| growth-assistant | `assistant.ask` | `read` | no | apps/growth-assistant/operator.py:127 | operator /invoke; POST /api/chat; GET /api/chat/job/abcd; GET /api/activity; GET /api/assets; GET /api/assets/x; GET /; POST /agent/run ask |
| growth-assistant | `assistant.playbook` | `write` | no | apps/growth-assistant/operator.py:61 | operator /invoke; POST /agent/run playbook |
| growth-assistant | `assistant.subreddit_plan` | `write` | no | apps/growth-assistant/operator.py:70 | operator /invoke; POST /agent/run subreddit_plan |
| growth-assistant | `assistant.founder_content` | `write` | no | apps/growth-assistant/operator.py:79 | operator /invoke; POST /agent/run founder_content |
| growth-assistant | `assistant.community_blueprint` | `write` | no | apps/growth-assistant/operator.py:88 | operator /invoke; POST /agent/run community_blueprint |
| growth-assistant | `assistant.cold_outreach` | `write` | no | apps/growth-assistant/operator.py:97 | operator /invoke; POST /agent/run cold_outreach |
| growth-assistant | `assistant.hire_brief` | `write` | no | apps/growth-assistant/operator.py:118 | operator /invoke; POST /agent/run hire_brief |
| growth-assistant | `assistant.push_assets` | `write` | yes | apps/growth-assistant/operator.py:106 | operator /invoke |
| guide | `guide.retrieve` | `read` | no | apps/guide/operator.py:25 | operator /invoke; POST /api/ask; GET / |
| guide | `guide.walkthrough` | `read` | no | apps/guide/operator.py:33 | operator /invoke; GET /api/walkthrough |
| infra | `infra.plan` | `write` | no | apps/infra/operator.py:113 | operator /invoke |
| infra | `infra.verify` | `read` | no | apps/infra/operator.py:130 | operator /invoke |
| infra | `infra.drift` | `write` | no | apps/infra/operator.py:132 | operator /invoke |
| infra | `infra.provision` | `write` | yes | apps/infra/operator.py:56 | operator /invoke |
| infra | `infra.configure` | `write` | yes | apps/infra/operator.py:123 | operator /invoke |
| infra | `infra.rollback_release` | `write` | yes | apps/infra/operator.py:126 | operator /invoke |
| infra | `infra.destroy_delta` | `delete` | yes | apps/infra/operator.py:118 | operator /invoke |
| document-editor | `document.connect_check` | `read` | no | apps/document-editor/operator.py:35 | operator /invoke |
| document-editor | `document.inspect` | `read` | no | apps/document-editor/operator.py:35 | operator /invoke |
| document-editor | `document.open_session` | `read` | no | apps/document-editor/operator.py:168 | operator /invoke |
| document-editor | `document.propose_patch` | `read` | no | apps/document-editor/operator.py:35 | operator /invoke |
| document-editor | `document.convert` | `write` | no | apps/document-editor/operator.py:172 | operator /invoke |
| document-editor | `document.export` | `write` | no | apps/document-editor/operator.py:182 | operator /invoke |
| document-editor | `document.apply_patch` | `write` | yes | apps/document-editor/operator.py:117 | operator /invoke |
| document-editor | `document.set_label` | `write` | yes | apps/document-editor/operator.py:146 | operator /invoke |
| document-editor | `document.publish` | `write` | yes | apps/document-editor/operator.py:132 | operator /invoke |
| outreach-engine | `outreach.refresh` | `read` | no | apps/outreach-engine/operator.py:26 | operator /invoke; GET /api/pipeline; GET /; POST /agent/run refresh; POST /api/run refresh |
| outreach-engine | `outreach.pipeline_summary` | `read` | no | apps/outreach-engine/app.py:475 | console intent |
| outreach-engine | `outreach.draft_followup` | `read` | no | apps/outreach-engine/app.py:475 | console intent |
| outreach-engine | `outreach.synced` | `read` | no | apps/outreach-engine/app.py:507 | GET /api/synced |
| outreach-engine | `outreach.discover` | `write` | no | apps/outreach-engine/app.py:342 | POST /api/discover; POST /api/agent |
| outreach-engine | `outreach.approve` | `write` | yes | apps/outreach-engine/operator.py:34 | operator /invoke; POST /agent/run approve |
| outreach-engine | `outreach.send_all` | `write` | yes | apps/outreach-engine/operator.py:46 | operator /invoke; POST /api/run send_all |
| social-autopilot | `social.activity` | `read` | no | apps/social-autopilot/app.py:545 | GET /api/activity; GET /; GET /api/reel/status; GET /api/reel/video; GET /api/vibex/status |
| social-autopilot | `social.vibex_trends` | `read` | no | apps/social-autopilot/app.py:611 | POST /api/vibex/trends; POST /agent/run vibex_trends |
| social-autopilot | `social.draft` | `write` | no | apps/social-autopilot/operator.py:31 | operator /invoke; POST /api/reel/schedule; POST /api/vibex/draft; POST /agent/run draft |
| social-autopilot | `social.draft_visual` | `write` | no | apps/social-autopilot/operator.py:41 | operator /invoke |
| social-autopilot | `social.publish` | `write` | yes | apps/social-autopilot/operator.py:52 | operator /invoke; POST /agent/run publish |
| social-autopilot | `social.record_reel` | `write` | no | apps/social-autopilot/app.py:568 | POST /api/reel/record; POST /agent/run record_reel |
| social-autopilot | `social.vibex_generate` | `write` | no | apps/social-autopilot/app.py:619 | POST /api/vibex/generate; POST /agent/run vibex_generate |
| agentic-crm | `pipeline_summary` | `read` | no | apps/agentic-crm/app.py:273 | GET /api/activity; GET /; POST /agent/run ask; POST /api/agent |
| agentic-crm | `stage_breakdown` | `read` | no | apps/agentic-crm/app.py:365 | console intent |
| agentic-crm | `lead_queue` | `read` | no | apps/agentic-crm/app.py:366 | console intent |
| agentic-crm | `connection_status` | `read` | no | apps/agentic-crm/app.py:367 | console intent |
| agentic-crm | `crm.score_lead` | `write` | no | apps/agentic-crm/operator.py:33 | operator /invoke; POST /agent/run score_lead |
| agentic-crm | `crm.research` | `write` | no | apps/agentic-crm/operator.py:42 | operator /invoke; POST /agent/run research |
| agentic-crm | `crm.draft_outreach` | `write` | no | apps/agentic-crm/operator.py:51 | operator /invoke; POST /agent/run draft_outreach |
| agentic-crm | `crm.qualify` | `write` | yes | apps/agentic-crm/operator.py:60 | operator /invoke; POST /agent/run qualify |
| lifecycle | `list_campaigns` | `read` | no | apps/lifecycle/app.py:250 | POST /api/agent |
| lifecycle | `list_segments` | `read` | no | apps/lifecycle/app.py:251 | console intent |
| lifecycle | `lifecycle_summary` | `read` | no | apps/lifecycle/app.py:252 | GET /api/activity; GET / |
| lifecycle | `lifecycle.segment` | `read` | no | apps/lifecycle/operator.py:40 | operator /invoke; POST /agent/run segment |
| lifecycle | `lifecycle.suggest_flow` | `read` | no | apps/lifecycle/operator.py:48 | operator /invoke; POST /agent/run suggest_flow |
| lifecycle | `lifecycle.compose_campaign` | `write` | no | apps/lifecycle/operator.py:29 | operator /invoke; POST /agent/run compose_campaign |
| support | `inbox_summary` | `read` | no | apps/support/app.py:468 | GET /api/activity; GET / |
| support | `list_conversations` | `read` | no | apps/support/app.py:467 | POST /api/agent |
| support | `draft_reply` | `read` | no | apps/support/app.py:15 | console intent |
| support | `support.draft_reply` | `write` | no | apps/support/operator.py:31 | operator /invoke; POST /agent/run draft_reply |
| support | `support.resolve` | `write` | yes | apps/support/operator.py:41 | operator /invoke; POST /agent/run resolve |
| support | `support.escalate` | `write` | yes | apps/support/operator.py:51 | operator /invoke; POST /agent/run escalate |
| support | `support.send_onboarding` | `write` | yes | apps/support/operator.py:61 | operator /invoke; POST /agent/run send_onboarding |


## 16. Identifier audit and the comma rule

**`agentic-os permissions audit-identifiers [--dir D] [--json]`** is a READ-ONLY report of the stored grants whose names break the CURRENT identifier contract (section 14: subject,
resource name, owner / row column, mask columns, actions; also a comma in a role subject or in a column, mask or action name). It prints the grant id (only when it is a plain token), the
field and the reason, never the offending value; it opens the store without migrating, generating a key or taking the lock file, and changes and deletes nothing (exit 0 clean, 1 violations,
2 the store does not open). Nothing is transformed silently: the stricter contract (now also rejecting line/paragraph separators anywhere, private use and noncharacters) needs an explicit
migration by the operator: **remove the grant by id (`agentic-os permissions revoke <id>`) and add it again under a corrected name.** Until then it authorises nothing and counts in `status()["malformed"]`.

**A comma inside a role, owner-column, column, mask or action name rejects the WHOLE collection or grant.** A comma-packed string such as `admin,ops` is a role-string smuggling vector
(whatever splits on commas downstream reads two roles), so a principal with such a role is invalid as a whole (no other role is honoured, no admin bypass), a role grant whose subject
contains a comma is refused (409) and a stored one is unusable and counted in `status()["malformed"]`; the same holds for a comma in an action, mask or row column name (already the case
for those three). This is a **breaking** validation change; user and app names may contain commas (they are single strings compared exactly). It matches the CR planes (Python and Go).

## 17. Admin API throttle behind a proxy, and the required deploy step

With `PERMISSIONS_TRUSTED_PROXIES` unset (the compose default) every visitor behind the reverse proxy shares ONE failed-attempt bucket. A hard lock would let ten anonymous wrong keys lock the real
admin out, so a shared bucket now **backs off** (each request waits `min(5 s, 0.25 s * 2^(failures - 10))`, identically for correct, missing and wrong keys; at most 8 threads sleep, the overflow is a
uniform 429) and the hard 429 lock stays only where the client identity is trustworthy (trusted proxies configured and a distinct client address). A one-time start-up warning is logged when the admin
API is enabled without the setting. **Deploy step (breaking for the showcase deployment):** set `PERMISSIONS_TRUSTED_PROXIES=<the proxy hop address>` (for a local cloudflared, `127.0.0.1`) in the host's
`permissions.env`; `deploy.sh` refuses to deploy without it (it was not run here). uvicorn's proxy headers are explicit: `--no-proxy-headers` unless the setting is present, then `--proxy-headers
--forwarded-allow-ips <that list>`. IPv6 clients are bucketed by /64 (an IPv4-mapped address is its IPv4). The showcase page has a Lock button that forgets the in-memory key. Details and caveats
(shared /64, bounded table eviction, per-process counters): `docs/SECURITY_MODEL.md`, "Admin API throttle".


## 18. Switch spellings, authorizer precedence, route gating and smaller fixes

**Switch spellings.** `AGENTIC_OS_PERMISSIONS`, `AGENTIC_OS_PERMISSION_AWARE_RETRIEVAL` and `AGENTIC_OS_PERMISSION_AWARE_CACHE` accept, case-insensitively and ignoring surrounding white space, **1, true, yes, on** (on) and
**0, false, no, off** or empty/unset (off). Any other value is "not a recognised value": the app refuses to start with a message naming the variable and these spellings, never starts ungated; at call time such a value counts as
requested, so a console that is somehow running denies. `CR_PERMISSIONS` keeps its own rule: any non-off value is a request.

**Authorizer precedence.** When the engine's default authorizer (what the enterprise bootstrap installs, or anything that calls `set_default_authorizer`) and the kernel's own (basic mode) are BOTH
installed, the consoles and `default_authorizer()` use the **stricter rule: both allow** (the first deny reason wins). A permissive default installed later can never loosen basic mode and basic mode never loosens the
enterprise gate; one alone is used as is.

**Route gating under either gate.** `app_gate.RequestContext` (installed in every app) makes the current request visible to `tool_denial`, so every route that executes a tool or returns tool/activity
data asks the ACTIVE gate (basic: the local profile; enterprise: the principal the bootstrap resolves from the request, anonymous when there is none) with the request's principal. The seven older apps' routes
(`/agent/run`, `/api/activity`, `/`, edge-sentinel's case/network/backup/permissions routes, privacy's fulfil/retention/audit) are gated like the ten newer ones. The disposition of EVERY route of the 17 apps (gated with its
tool, public by design with the reason, operator-authenticated, engine console, framework) is `apps/_route_table.py`; `apps/test_route_walk.py` fails when a route has no decision and walks every gated route anonymously
under the enterprise gate and under restricted basic mode.

**Smaller changes, one line each.** The Operator seam denies when basic permissions are requested and no gate is registered for the operator (the suite server installs its gates; `sky`, `video-ad`, `zoning` are denied
under permissions until they get a tool table; the operators the kernel planes build themselves are exempt per capability by registration, see section 26). The routes ask the active gate with the request's principal. Switch parsing is strict. `infra.plan` and `infra.drift` are `write`; an automated sink heuristic covers the read classifications. The Operator authorises before its idempotency cache and keys the cache by principal.
The profile file is read like a secret (no symlink, regular file, owned by the user, mode 0600). When both authorizers are installed the stricter of the two applies. The enterprise table is read only from an installed distribution, only in the enterprise mode,
and can only add denials. The engine's default authorizer is looked up where the engine keeps it. With nothing requested the enterprise bootstrap is used for identity only (`install=False`).
A store at counter 1 with a deleted sidecar is accepted, and a whole-directory restore revives revoked grants: both are disclosed residuals, not code changes.

## 19. An unset setting is not consent: the rule, the execution invariant, cache order, and the threat model

**The rule (implemented).** `AGENTIC_OS_PERMISSIONS` has three meaningful states and one failure:

| setting | meaning | what happens |
|---|---|---|
| `1` (true, yes, on) | permissions enforced (basic, AGPL) | gate installed; no grants provisioned = starts restricted (denies, pointing at `agentic-os permissions init`) |
| `off` (0, false, no) | **deliberate standalone mode** | no authorization on the app's tools; a start-up INFO line says "authorization is explicitly disabled (standalone)" |
| **unset** | *unconfigured*, not consent | the documented single-user quickstart keeps working (basic default) **but** every app logs, once, a WARNING that authorization is NOT enforced and how to enforce it (`agentic-os permissions init`, `AGENTIC_OS_PERMISSIONS=1`) or silence it deliberately (`=off`). With `AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING=1` an unset setting REFUSES TO START |
| anything else | malformed | refuses to start |

**Loopback versus non-loopback: not implementable robustly, so the alternative is used.** Whether an app is reachable is decided by the process manager (`uvicorn --host`, a container's published port), not by
the module, and containers bind 0.0.0.0 by design; a check at import time would either miss the exposure or refuse local development. Implemented instead: (1) where the code binds itself (14 apps' `__main__`)
`require_explicit_setting_when_exposed` refuses a non-loopback host with no explicit setting; (2) every shipped app image (the 16 app Dockerfiles and the suite server's) sets
`AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING=1`, so a container never starts on an unset setting; (3) the unset notice. Loopback dev (`uv run uvicorn ... --host 127.0.0.1`) keeps working with the notice.

**Impact table (every shipped flow in this repository that starts a gated app).**

| flow | before | now |
|---|---|---|
| `docker run` of an app image (16 app Dockerfiles, `apps/_suite-server`) | started ungated, silently | refuses to start unless `-e AGENTIC_OS_PERMISSIONS=1` or `=off`; the `docker run` examples in the 8 app READMEs that have one now carry `-e AGENTIC_OS_PERMISSIONS=off` explicitly (change to `1` after `permissions init`) |
| `python3 -m uvicorn app:app --host 0.0.0.0` / `python app.py` (14 apps' `__main__`) | ungated | `python app.py` with a reachable bind refuses with no setting; `-m uvicorn` runs with the notice |
| fleet `up` (`agentic_os.fleet`, `modules.yaml`) | runs each module's own `docker compose` | **follow-up outside this repository**: those module compose files must pass `AGENTIC_OS_PERMISSIONS` (they inherit the REQUIRE image default) |
| root `docker-compose.yml`, `deploy/docker-compose.yml`, `deploy/reference-system` Helm chart, `deploy/sidekick-devops` | control plane / runtime images, no gated app | no gated app is started by them, so the image default does not apply; not started or installed in the release checks (apart from the `permissions.keySecret` render check above) |
| `deploy/permissions-showcase-demo` | the showcase has no tool gate | unchanged |
| `make run` (control plane) | control plane | unchanged |
| the single-user quickstart `agentic-os permissions init` then `export ... AGENTIC_OS_PERMISSIONS=1` | works | works (tested, also with the REQUIRE switch on) |

**Execution invariant.** The Operator (the final execution boundary) enforces whatever app wiring does: permissions not requested = standalone/basic policy; requested and a gate applicable = evaluated BEFORE execution
(basic: the local profile; enterprise: the principal of the current request, anonymous in process unless a service identity is configured); requested and **no gate, or none applicable, or initialisation failed = DENIED**
with an actionable message, whether the call is `Operator.invoke`, `/invoke`, the suite server's `/m/<module>/invoke`, `/agent/run`, a job or the CLI. The operators the kernel planes build (customer interaction, the Agent Gateway's social and external-agent operators) are
exempt per capability by registration of the operator object (section 26), never by name. The older apps' tool bodies share ONE wrapper (`app_gate.guarded`), asked before the body whatever calls it; app middleware and route checks stay as defence in depth. Entry points this does not
reach: processes that never import `agentic_os.mission.operator_sdk` (none ships), and `sky`/`video-ad`/`zoning`, which are simply denied while permissions are on.

**Idempotency cache order.** authenticate the principal -> evaluate the CURRENT grants and resource scope -> look in the cache within that scope -> return or execute. A cache entry records the principal and a digest of the
effective scope (resource, action, row filter, column masks); a read whose scope changed executes again inside the new scope, a write whose scope changed is refused (new key needed), and a principal who lost access gets the
denial, not the cached body, and the side effect is not repeated.

**Authorizer conflicts.** When the engine's default and the kernel's own authorizer are both installed, both must allow; if they disagree the call is denied (tests: engine denies/kernel allows, kernel denies/engine
allows, both allow). **Classification**: a planted `sys.path` module or an unexpected distribution can never weaken it; the enterprise table can only add denials. The per-tool classification record is
`docs/TOOL_CLASSIFICATION_INVENTORY.md`; the sink heuristic is a regression signal that cannot prove a tool read-only.

**Threat model (documentation only).** The rollback counter protects against restoring an older `grants.enc` while the sidecar stays (partial restores) and against deleting the sidecar of a store past its first
write. It does NOT protect against an attacker who controls the storage volume itself (a whole-directory or whole-volume restore, which restores the sidecar and the key file too), against a store at counter 1 with a
deleted sidecar, or against anyone who can write the permissions directory and read the key. Those are out of scope for the first AGPL release unless made an explicit guarantee; keep `PERMISSIONS_COUNTER_FILE` on a
different volume, and the key outside the data volume (`PERMISSIONS_KEY` from a secret store), if they matter.

**Scope of the execution invariant, stated plainly.** Covered and tested: the 17 apps' routes, `Operator.invoke`, each operator's HTTP `/invoke`, `/agent/run`, the suite server's `/m/<module>/invoke`,
the consoles' `/api/agent`, and the apps' tool bodies (the shared `guarded` wrapper). Not part of this grant model: the agent gateway's REST and MCP adapters (`agentic_os/agent_gateway`), which authenticate
callers with their own OAuth/token layer and govern kernel operators by action class (those operators are registered as kernel-governed, section 26), and the `agentic-os` CLI, whose commands administer the permissions
store and local state rather than execute app tools. The gateway's REST adapter, MCP bridge and MCPEndpoint return a short fixed code (for example `capability_error`, `gateway_error`, `duration_exceeded`) when a capability, a write port or a gateway seam raises, never the exception text; the code is chosen from a fixed list, not copied from the message. The exception text and traceback go to the `agentic_os.agent_gateway` log together with the request id (control characters escaped), so gateway logs hold the raw exception text by design and access to them is privileged. A handler that returns its own error string is unchanged. The reproducible sweep is `tests/sweeps/sweep.py` (about 86,000 calls, no violation at the commit that adds it).

## 20. The operator's caller credential and the user's identity token use separate headers

An operator's `POST /invoke` authenticates its CALLER (the control token, or a tenant token) and, under an enterprise gate in token identity mode, the END USER comes from `Authorization: Bearer <JWT>`.
Both used to read `Authorization: Bearer`, so with a control token configured a user identified by a token could not use `/invoke` (the JWT failed as a control credential, or the control token made the
request anonymous). The contract now:

| header | meaning on `/invoke` |
|---|---|
| `X-Control-Token: <token>` | the caller credential (admin or tenant token, the bare token, no scheme). When it is present and not blank it IS the credential and `Authorization` is left entirely to the identity layer |
| `Authorization: Bearer <JWT>` | the user's identity (enterprise token mode); never read as a control credential when `X-Control-Token` is present |
| `Authorization: Bearer <control token>` | **still works for existing callers** (no `X-Control-Token`): the original contract. In enterprise token mode the same header would also be the user's identity channel, so such a request identifies no user (anonymous): switch those callers to `X-Control-Token` |

Neither credential stands in for the other: a wrong `X-Control-Token` is not rescued by a correct Bearer; a JWT in `X-Control-Token` or alone in `Authorization` is refused (401); a forged or invalid user token
is anonymous even with a valid control token; with no token configured the route stays disabled (503, or the explicit development flag). A repeated `X-Control-Token` or `Authorization` header, or an `X-Control-Token` together with a DIFFERENT valid control credential in `Authorization`, is refused with 401: a duplicated credential header is a
request-smuggling signal, and no copy is preferred. Only `/invoke` changed; the other control routes still read `Authorization`.
Tests: `tests/test_kd4_control_token_channel.py`.

## 21. The idempotency cache rule holds under the enterprise gate too

`app_gate.principal_key` and `app_gate.operator_scope` are mode-aware. Basic mode: the local profile user and the digest of the plane's decision. Enterprise mode: a digest of the identity the enterprise
gate resolves for THIS request (user, app, role set; anonymous when there is none, as for a background job) and a digest of the plane's CURRENT decision for it (allow with row filter, column masks and
per-grant rules, or the deny reason; when the installed authorizer cannot describe a decision, the outcome of its `check` alone). Two principals never share a cached result; a changed grant re-executes a read
and refuses a write replayed under a changed scope. Tests: `tests/test_kd5_enterprise_cache_principal_and_scope.py`.

## 22. `/invoke`: authorise, then approve, then execute

An operator's `POST /invoke` consults the grant FIRST. Before this change a capability that needs an approval answered `pending_approval` before any grant was looked at, for every caller including an
anonymous one: that revealed that the capability exists and has side effects to a caller who may not run it, and let such a caller queue approval requests for the approvers. Now a caller who may not run the
capability gets the denial (`status: denied`, `[blocked]`, the actionable reason) instead of an approval prompt, of the production profile's "execution authorization required" 403, or of a 404 for an unknown
capability name; a denied call verifies and consumes nothing single-use (an approval token or an execution-authorization nonce). A caller who holds the grant meets the approval and the profile rules exactly as
before. With permissions not requested nothing changes. Tests: `tests/test_invoke_authorise_before_approve.py`.

## 23. Process-wide gate state

The gate registries (`_GATES`, `_ENTERPRISE`, the "not enforced" notices and the process authorizer) live as long as the process. `agentic_os.app_gate.reset_state()` clears them in place; the test fixtures call it before
and after every test, and a harness that re-imports apps in one process should too. Production does not need it: `install_gate` starts each app from a clean entry, a reinstall that no longer enforces basic
permissions drops the stale process-wide authorizer once no gate is left, and if an app name were ever in both registries the stricter answer wins. Tests: `tests/test_gate_state_reset_and_stale_entries.py`.

## 24. The enterprise package is imported only on an explicit request, and only from an installed distribution

* **Nothing requested, or basic permissions (`AGENTIC_OS_PERMISSIONS`)**: the kernel does NOT import `context_runtime_enterprise` at all, whether or not it is installed (before, with nothing requested it was
  imported to resolve the request identity; the bootstrap is no longer called). Basic mode only sets the engine's default authorizer when the PUBLIC engine package (`context_runtime`) is installed.
* **Enterprise requested (`CR_PERMISSIONS`, `CR_IDENTITY_*`, the permission-aware switches)**: before the first import the kernel locates `context_runtime_enterprise`, `context_runtime` and `agent_harness`
  with `importlib.util.find_spec` of the TOP-LEVEL name (which runs nothing) and requires the located `__init__.py` to be a file an installed distribution's metadata lists (its RECORD), or, for an editable
  install, to lie under the project directory in the distribution's `direct_url.json`. A package that is present but not provided that way (a directory on `sys.path`, such as the working directory of
  `python -m uvicorn`, a module injected into `sys.modules`) makes the app REFUSE to start ("not provided by an installed distribution"); nothing is imported. Absent keeps the missing-capability error.
* The distribution itself is looked up ONLY in the interpreter's own package directories (`sysconfig` purelib / platlib, `site.getsitepackages()`, the user site; the venv's site-packages inside a venv), never in the
  working directory or another `sys.path` entry (`importlib.metadata` reads dist-info from every `sys.path` entry, so a forged `*.dist-info` next to a planted package would otherwise vouch for it). A RECORD-listed
  file must lie there too; an editable install is trusted when its dist-info is there and the package lies under the project directory it records. Both recipes work: a normal `pip install` of the optional Enterprise package, and `pip install -e`.
* The enterprise tool table (`tool_actions`) and the engine hook use the same check.
* **Limit:** this does not defend against an attacker who already controls the import environment (the installed packages, the interpreter, `PYTHONPATH`-style startup hooks, the metadata itself); it removes
  the case where a stray directory ahead on `sys.path` silently wins. The apps' own top-level `import context_runtime` is app code, unchanged.

Tests: `tests/test_dr02_enterprise_import_trust.py` (planted packages write a marker file that must never exist), superseded expectations in `tests/test_kd2_basic_default.py` and `tests/test_rr01_app_gate_startup.py`.

## 25. A path with an empty segment is not a child resource

A grant on `infra` covers `infra` and its real children (`infra.state`, `infra.state.x`). `Grant.covers` used to test only `startswith(grant + '.')`, so it also covered `infra.`, `infra..x`, `infra.x.`, `infra...`
and `infra.` followed by a NUL, tab, newline or zero-width character. Every segment after the grant's own must now be non-empty and a valid identifier (no padding, no control or format characters); exact equality is
unchanged (a grant spelled `X.` covers a resource named exactly `X.`, not bare `X`, not `X.y`). One coverage site exists in the kernel (`Grant.covers`, used for every resource kind: database, table, corpus, tool);
the tool seam looks a tool up by exact name in the trusted table, and the other prefix tests in the kernel match URL paths, not resources. Tests: `tests/test_resource_empty_segment_coverage.py`; the Enterprise package's authorization suite,
run against this kernel, also passes.

## 26. No exemption by operator name

`app_gate.KERNEL_OPERATORS` (a set of three NAMES) is gone: an operator someone calls `social`, `interaction` or `external-agent` is gated like any other. The three kernel planes that build their own operators
register the operator OBJECT and the capabilities it has at that moment with `app_gate.kernel_governed(operator, reason)`, which refuses any caller outside `agentic_os.interaction.operator`,
`agentic_os.agent_gateway.social.operator` and `agentic_os.agent_gateway.external.operator`. `operator_denial(name, capability, operator=...)` honours the registration for that object and capability only; a second
operator with the same name and capabilities, or a capability added later, is not exempt. Tests: `apps/test_dr04_no_name_based_exemption.py` (also pins the exact registration sites).

## 27. A missing `profile.json` means the default local user `local`

Quickstart threat model: the single local principal comes from `profile.json` (written by `agentic-os permissions init`) and from nothing a request carries. When the file is missing (a store provisioned before
profiles existed) the principal is the default local user `local`, with a warning at start-up that names the file and `agentic-os permissions init`; the fallback fails closed unless grants for `local` exist. Request
headers, query strings and tokens never choose the principal (tested with a missing profile too). Tests: `tests/test_dr09_missing_profile_default_user.py`. See `docs/SECURITY_MODEL.md`.

## 28. What this release claims, and what it does not

The statement this release makes, and no more: *The permissions plane and the app execution paths were reviewed in a multi-stage, AI-assisted security review with separate reviewer agents working from the code diff; this is not an external audit or third-party certification. Deployment configuration may still run unenforced when the setting is absent.*. This release does not describe a deployment as protected by default: an
app started with `AGENTIC_OS_PERMISSIONS` unset (or `=off`) enforces nothing, and says so:

* a WARNING at start-up (`AUTHORIZATION IS NOT ENFORCED` / `authorization is DISABLED`) that names the configuration for an externally reachable deployment;
* `authorization: unenforced` (with `authorization_setting: unset` or `off`) in the app's `/health` answer and in the operator's `/_security` answer; `restricted` means permissions are requested and no permissions key is
  provisioned (everything protected is denied), `enforced` means a gate decides.

**The exact configuration for an externally reachable deployment.** `AGENTIC_OS_PERMISSIONS=1` after `agentic-os permissions init` (grants provisioned, `PERMISSIONS_KEY` from a secret store), or, for the
enterprise gate, `CR_PERMISSIONS=1` with the enterprise package and an identity mode; AND `AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING=1`, so that a lost or emptied setting refuses to start instead of running unenforced
(the shipped app images set it). Then check `/health` for `authorization: enforced`.

### Ways an unset-and-reachable deployment can still run unenforced

* `uvicorn app:app --host 0.0.0.0` (or any server started directly) with the setting unset: a warning only; only an app's own `python app.py` bind refuses a reachable host with nothing set.
* The `infra` app has no image: its container is yours to build, so the image default does not apply.
* A derived image or compose file that unsets or empties the variable (an empty value, for example from a compose `${VAR:-}`, counts as unset), or overrides `AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING`.
* Apps without a `__main__` bind: `document-editor`, `guide` and `outreach-engine` are started by a server you configure.
* The fleet `up` compose files of the module repositories (outside this repository) that pass neither variable.
* `AGENTIC_OS_PERMISSIONS=off` on purpose (reported as `authorization: unenforced`).

In each case the mitigation is the same pair of settings (`AGENTIC_OS_PERMISSIONS=1` and `AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING=1`) in the process's own environment, and a check of `/health` for `authorization: enforced`.

Making the strict permission setting the default (an unset setting refusing to start) is planned for the next compatibility-breaking release.

## 29. AGPL basic plane: app grants are union subjects; the principal is the local profile; fleet app/principal two-gate model is Enterprise

* **Union subjects.** In the kernel plane a grant's subject (`app`, `role` or `user`) is one more way to be matched; the grants matching the identity are UNIONed. A grant whose subject is an app allows every identity
  that carries that app; there is no second gate. (The enterprise plane's fleet model, the template with default `read`, independently revocable write grants, a fresh install denying and a new app inheriting the
  template but no grants, needs the app grant AND the principal's own grant. That two-gate model is Enterprise; the shared spec marks those cases `app_gate` and the kernel skips them.)
* **What an `app` grant means for the apps that run under basic permissions.** Nothing. The basic gate's principal is the local profile user with an EMPTY `app` and no roles, taken from the profile file and from nothing
  a request carries; an `app` grant needs `identity.app` to equal its subject, so it never matches that principal, whatever a caller sends. A store holding only an app-wide grant for `books` (every action) leaves
  every `books` write tool denied to the local profile and to every caller, and `agentic-os permissions init` never creates app-subject grants: it creates one `user`-subject grant per tool and action.
* **Where the union semantics do apply.** To code that builds an `Identity(app=...)` itself and calls `PermissionsPlane.authorize` (an integrator, not the apps' gates): an app grant alone allows that app's identities.
  If you provision `app` grants through the API, know that they are for such callers, and write actions should be granted to a named user or role instead.
* **The real property to know for an externally reachable deployment.** Basic permissions authorize what the app INSTANCE (the local profile) may do; they do not authenticate callers. Every caller who can reach an
  instance acts as the local user, so an instance with write grants lets every caller write. Put authentication in front of it (the enterprise identity gate, or a proxy), or grant the profile read only.
  No code change was found necessary; a candidate hardening, not implemented: refuse `app`-subject grants for write or delete actions unless a principal grant for the same resource also exists.

Tests: `tests/test_basic_plane_app_grants_are_union_subjects.py`.

## 30. The privacy portal is public by design: a constrained exception (`public_intake`), not a bypass

**What it is.** `agentic-privacy`'s `POST /intake` and `POST /request` (and the static form at `GET /intake`) are reachable WITHOUT a grant: a data subject has to be able to file a request. They are the only routes
of the 17 apps with this property. They are registered by id in trusted code (`agentic_os.tool_actions.PUBLIC_INTAKE_ROUTES`: app, method, path, and the one tool they may act for), no environment variable or
configuration adds to the registry, and `app_gate.public_intake` mints a ticket that exactly one core function (`core.public_intake`) accepts. A ticket is not an argument of the Operator, `/invoke`, `/agent/run` or
the console. `public_intake` is a classification of its own, separate from read / write grants.

**What it does, and nothing more.** It opens a PENDING request (`status: awaiting_verification`) and sends ONE verification e-mail to the address the subject typed, through a fixed Postmark operation
(`POST https://api.postmarkapp.com/email`, keys `From`, `To`, `Subject`, `HtmlBody`, `MessageStream`; the SMTP fallback sends the same message). It looks up no customer record and discloses, changes and deletes none: access,
erasure and correction need the emailed single-use token and then the authenticated fulfilment route. The response is the same whoever the address belongs to and echoes nothing typed (`status`, `request_id`, `type`, `delivery`, a generic `message`).
(`PRIVACY_DEMO_MODE`, the synthetic-data demo, keeps its separate one-click flow on `/request`.)

**What protects it.**

| control | detail |
|---|---|
| strict schema | JSON object with only `email`, `type` (`access`, `delete`, `opt_out`, `correct`, plus the legacy spellings) and the honeypot `website`; anything else is 400, wrong content type 415, body over `PRIVACY_INTAKE_MAX_BODY` (default 2048 bytes, enforced while reading) 413 |
| one plain mailbox | ASCII, at most 254 characters, no list, display name, whitespace, control or format character; re-checked by the send primitive, so CR/LF, a recipient list or a Bcc can never ride in; the subject and the message are fixed text, the type is a closed set |
| no Host-derived link | the link in the message is built from `PRIVACY_PUBLIC_URL`; the request's own host is used only for a loopback host. **With `PRIVACY_PUBLIC_URL` unset and a non-loopback host no message is sent** (`delivery: undeliverable`, logged): set it |
| abuse controls | per-client token bucket, `PRIVACY_INTAKE_PER_EMAIL_HOUR` (default 5) per address, `PRIVACY_INTAKE_GLOBAL_PER_HOUR` (default 500) for the whole app, a pending-backlog cap `PRIVACY_PENDING_MAX` (default 200, then 503), a honeypot (a filled `website` gets the normal-looking answer and nothing is created or sent); limiter memory is bounded and evicts the oldest entry. No CAPTCHA is included |
| minimal retention | the record keeps the address, type, timestamps and the token HASH; no IP address or user agent; an unverified request is deleted after `PRIVACY_UNVERIFIED_RETENTION_HOURS` (default 72) |
| audit | a hash-chained `public_intake` event with the route, request id, type, delivery channel and the address MASKED (`j***@example.com`), never the full address |

**What does not change.** `privacy.intake` stays classified `write` for the Operator, `/invoke`, `/agent/run` and the console: an anonymous caller and a read-only principal are still denied there (tests). The classification
inventory row says `public intake: unauthenticated by design`. Behaviour changes: unknown or forged fields on `/request` (`verified`, `confirm`, `_approval`) are now a 400, not silently ignored; a deployment
without `PRIVACY_PUBLIC_URL` sends no verification message. Tests: `apps/agentic-privacy/test_public_intake.py`, `apps/test_public_intake_exception.py`.

## 31. Known limitations of this release (stated, not fixed)

* **Public intake can be filled.** The portal's pending backlog (`PRIVACY_PENDING_MAX`, default 200) and the global cap (`PRIVACY_INTAKE_GLOBAL_PER_HOUR`, default 500) bound the e-mail the portal can be made to send, and
  they can be filled by valid, unique addresses: once full, real data subjects get 503 or 429 until entries expire (the unverified retention is 72 hours, `PRIVACY_UNVERIFIED_RETENTION_HOURS`) or are verified. Each accepted
  submission sends one e-mail to the typed address; the raw address is kept in the requests store, unverified, for up to that retention, while the audit log holds it masked. There is no CAPTCHA. What an operator can do:
  lower the caps to what the mailbox provider tolerates, put a rate limit or a challenge in front of `/intake` and `/request`, and watch for `public_intake` audit events and 503s. The limiter state is per process.
* **`kernel_governed` identifies its caller by the frame's `__name__`.** Only the three kernel plane modules may register an operator as governed by their plane; the check reads the calling frame's module
  name, so in-process code that sets `__name__` to one of those names could register an operator. A normal import path cannot. In-process code can do far more than this; the check keeps a user-chosen operator
  or capability NAME from obtaining the exemption, which it does.
* **`/health` and `/_security` tell anonymous callers whether authorization is enforced.** `authorization: enforced | restricted | unenforced` and `authorization_setting: on | off | unset | enterprise` are part of
  the answer (no key, path or grant content). This is deliberate transparency for deployers and also tells a scanner which instances are open. If the endpoints are reachable from a network you do not control, restrict
  them at the proxy; the fix for an `unenforced` answer is the configuration in section 28, not hiding the answer.
* **The public-intake ticket seal is reachable in process as `app_gate._SEAL`.** The ticket is not an argument of the Operator or any HTTP route and the registry is read-only, so the seal protects against
  mistakes and against anything that reaches a route, not against code running inside the process.
* **A spent approval token in the revoke race.** `/invoke` checks the grant before it verifies an approval token and `Operator._execute` checks again. If a grant is revoked between the two, the call is denied and
  the approval token that was verified in between has been consumed (single use). Nothing executes; the approver must approve again.
* **`enforced` with no grants.** A basic-plane app reports `authorization: enforced` when a permissions key is provisioned, even when no grant exists yet: the gate decides and denies everything. `restricted`
  means that no permissions key is provisioned, so there is no grant store to consult. A missing local profile (`profile.json`) alone does not make an app `restricted`: the principal is then the default local user `local`, and the app logs a warning.

## 32. Where the key lives: an environment key means no key file on the data volume

The store takes its key from, in this order: (1) `PERMISSIONS_KEY` (base64, 16/24/32 bytes), which wins whenever it is set; (2) with it unset, an EXISTING key file (`PERMISSIONS_KEY_FILE`, else
`<dir>/.permissions.key`) that is valid, mode 0600 and owned by the running user; (3) nothing: the store and the control plane refuse to start, and an app starts restricted and denies every protected call (or, with `PERMISSIONS_ALLOW_GENERATED_KEY_DEV=1`, a development key file is generated and a
warning says so). `PERMISSIONS_REQUIRE_KEY=1` accepts only (1). A set but invalid `PERMISSIONS_KEY` is an error and never a fallback to a file; a blank one counts as unset.

**Defect fixed in this release.** `agentic-os permissions init` created `<dir>/.permissions.key` even when `PERMISSIONS_KEY` was set. The grants were sealed with the environment key, so the file sealed
nothing, but it was a second key beside the ciphertext and would have been picked up if `PERMISSIONS_KEY` were ever unset. Now:

| `PERMISSIONS_KEY` | key file | `permissions init` | `init --key-only` | every other command / app / control plane |
|---|---|---|---|---|
| set, valid | absent | seals with the env key; **creates no key file** | nothing to provision, creates nothing | env key; creates no key file |
| set, valid | present (stray) | env key; the file is left alone | creates nothing | env key; **WARNING once, by path only**: the file is not used, remove it |
| set, invalid | any | error, nothing written | reports "nothing to provision", exit 0, writes nothing | error (never a fallback to the file) |
| unset | absent | creates `PERMISSIONS_KEY_FILE` if set, else `<dir>/.permissions.key` (0600, exclusive), seals with it (the single-user quickstart) | creates only the key file | the store and the control plane refuse to start; an app starts restricted and denies every protected call; nothing is written to the volume (no implicit key) |
| unset | present, 0600, ours | keeps it, seals with it | keeps it | uses it, with the existing "on-box key file" warning |
| unset | present, wider than 0600 | refuses | reports "kept", exit 0 (the file is not checked) | refuses (fix the mode, or use the environment key) |
| unset, `PERMISSIONS_REQUIRE_KEY=1` | any | **refuses** (does not create a key file) | refuses | refuses |

A stray file next to an environment key is left untouched (the operator decides) and its content is never printed. Tests: `tests/test_keyfile_with_env_key.py`.

Small things to know: re-running `init` with no environment key over a store that was sealed with an environment key creates a new `.permissions.key` and then fails (exit 1), leaving `grants.enc` unchanged; the stray-file warning says to remove the file, which cannot be followed when `PERMISSIONS_KEY_FILE` points at a read-only secret mount; a relative `PERMISSIONS_KEY_FILE` is resolved against the current directory, so use an absolute path.

**No grant is created implicitly.** The control plane and the permissions showcase used to seed two example grants into every fresh store (`seed-support-crm-customers`: app `support`, table `crm.customers`, read,
rows `region = us`, `email` masked; `seed-finance-crm-customers`: role `finance`, same table, read, `notes` masked). They are now seeded, once, only when the deployment opts in with `AGENTIC_OS_SEED_DEMO_GRANTS=1`;
the permissions showcase compose file sets it (demo-only), the root and `deploy/` compose files do not, and a fresh install starts with no grant. They are `table` grants, so the apps' tool gate never matches them.
A store that was already seeded keeps its two grants; remove them with `DELETE /api/permissions/grants/<id>`.

## 33. The control plane's fleet reads need a credential; /health and /capabilities are trimmed

Five read routes of the control plane (`agentic_os/control_plane.py`) used to answer anyone who could reach the port, and a GET is not safe for that reason:

| Route | What it returns |
|---|---|
| `GET /approvals` | every pending approval with its payload: the request text an approver is asked to sign off |
| `GET /jobs/{job_id}` | the status and the result text or error of a background dispatch; the ids are sequential (`job-00001`) |
| `GET /api/fleet` | per module: the repository, ports, approval-required capability names, backend names, deploy kind and live health |
| `GET /status` | which modules are deployed (one health probe each) |
| `GET /notify/status` | which chat channels are wired and whether the inbound gateway runs |

They now take the fleet credential, as the other fleet routes do (`POST /up`, `/down`, `/dispatch`, `/approvals/{id}/{decision}`, `/openapi.json`): `Authorization: Bearer <AGENTIC_OS_CONTROL_TOKEN>` or
`X-API-Key: <AGENTIC_OS_API_KEY>`. **These five reads, and only these, also accept a separate read-only dashboard token** in `X-Dashboard-Token` (`AGENTIC_OS_DASHBOARD_TOKEN`): at least 24 characters,
different from the fleet key, the control token and the permissions admin key (an unusable value disables it and is logged once), compared in constant time. It opens no mutating route, not `/openapi.json`, not
the mission routers, not the permissions admin API, not the security block of `/health` and not the capability detail; the fleet key and the control token keep working on everything they worked on. A missing,
empty, wrong or wrong-scheme credential answers 401; with nothing configured they answer 503 (never open). The permissions admin key is a separate privilege and does not open them, and a credential in the query
string is ignored (it would still appear in an access log). A refusal runs nothing (no job lookup, no registry walk, no health probe) and returns no content. Names, repositories, pains, taglines and agents of the
modules are still public through `GET /modules` and `/overview`.

**`GET /health` is minimal liveness.** An anonymous caller gets `{"status": "ok", "modules": N}`, which is what kubelet `httpGet` probes (the Helm chart's), load balancers and `curl` need. The `security` block
(profile, durability of authority and replay protection, egress proxy, warnings) now appears only for a caller that presents the fleet key or the control token; the dashboard token does not open it. A readiness
check that reads the posture sends the credential. (The suite server's `/health` and each operator's `/_security` are separate endpoints and are not changed here.)

**`GET /capabilities` (mission routers) shows an anonymous caller only names.** It returns the name, the operator and what each capability provides; discovery by `need` returns names without scores. Inputs and
outputs, cost, permissions, side effects and undo or compensation, approval flags, secrets, network and authority requirements, isolation, resource keys, data classes and the embedding are returned only to a
caller with a valid control or tenant token (or on a server that opted out of read gating with `AGENTIC_OS_GATE_READS=0`, for local demos). `/cockpit` and `/buildinfo` stay public.

**This release does not make the control plane as a whole protected. What is still open:**

* **The module proxy is anonymous, including POST.** `GET /m/<name>`, `GET /m/<name>/raw`, and `GET` and `POST /m/<name>/api/<path>` are reachable without any credential in every default deployment. They forward
  to the module's own service (its dashboard and its `/api/*`, which can include chat actions and data reads). The only restriction is `DEMO_READ_ONLY=1`, which no shipped configuration sets and which blocks
  only `POST`. This is pre-existing, is tracked as public issue #528, and **is not changed by this release.** Whether each app's own gate (`AGENTIC_OS_PERMISSIONS`) stops a request that arrives through
  the proxy has **not been verified.** **Operator mitigation:** do not expose the control plane's port to an untrusted network, and put an authenticating reverse proxy in front of it (or block `/m/` there).
* `GET /modules`, `/overview`, `/`, `/permissions`, `/assets`, `/cockpit` and `/buildinfo` are public.

**The dashboard credential.** The dashboard asks for a credential in a field (read-only dashboard token first; the fleet key or the control token also work). It is kept in that page's memory only, sent as a request
header to the dashboard's own origin, never stored or put in a URL or log, and dropped on Clear, on a refusal and when the page is closed or reloaded. Use the dashboard token: it cannot dispatch, start, stop or
approve. If the fleet key or control token is typed instead, it is the full credential, re-sent every 5 seconds while the tab is open over whatever transport the deployment uses (the stock compose file serves
plain HTTP on port 8080), and `AGENTIC_OS_API_KEY` has no minimum length and no failed-attempt throttle. `GET /` is served with `Cache-Control: no-store`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and a
Content-Security-Policy of `frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'; connect-src 'self'`. That stops framing, a `<base>` tag, a form posted elsewhere and plugins, and limits
`fetch`/XHR/WebSocket/EventSource/beacon connections to the page's own origin. It does not restrict images, links or navigation, and there is **no `script-src`**: it is not a defence against script injection (the
page builds its DOM with `textContent` and has no known injection sink). A nonce policy like the one on `/permissions` would need the page's inline styles and its one handler attribute reworked and a browser to
check it; that is left for a later change.

**Upgrade impact.**

* Anything that called the five routes without a credential now gets 401, or 503 on a deployment that sets none of `AGENTIC_OS_API_KEY`, `AGENTIC_OS_CONTROL_TOKEN` and `AGENTIC_OS_DASHBOARD_TOKEN`. `deploy/docker-compose.yml`
  passes all three variables through, empty by default, and the root `docker-compose.yml` now does too (before, the container never saw them, so a key could not be set without editing the
  file). A stock compose deployment answers 503 on these routes until one of them is set, for example `export AGENTIC_OS_API_KEY="$(openssl rand -hex 24)"`, then `docker compose ... up`, and then
  `curl -H "X-API-Key: $AGENTIC_OS_API_KEY" http://localhost:8080/api/fleet`. For a browser dashboard set `AGENTIC_OS_DASHBOARD_TOKEN="$(openssl rand -hex 24)"` too and type it into the page. The Helm runtime chart pulls a
  separately published image that its values describe as built from this control plane (`ghcr.io/redevops-io/redevops-runtime`, pinned at 0.3.1, so existing installs are unaffected until that tag is rebuilt from this
  release). The chart has no value for any of these variables, so after that the five reads answer 503 on a Helm install (the mutating fleet routes already did) until the chart gains one.
* The dashboard shows no fleet and no approvals until a credential is entered, and says what it needs; with none configured on the server it says the views are disabled. Because a page cannot share the
  credential with another page without storing it, `/overview` and `/m/<name>` (which also poll `GET /api/fleet` for their live health dots) hold none: they ask once, and after the first 401 or 503 stop polling and show
  one line ("credential required" or "no fleet credential configured on the server"); their health dots stay grey. Behind a reverse proxy that adds the credential they keep working.
* `GET /health` no longer carries `security` for an anonymous caller (see above); `GET /capabilities` no longer carries the detail for an anonymous caller.
* A fresh install no longer starts with the two example grants (section 32); a demo that wants them sets `AGENTIC_OS_SEED_DEMO_GRANTS=1`.
* The 503 body for these routes reads "the fleet endpoints are disabled on this server" (it said "mutating endpoints").
* On the public demo, an anonymous visitor would see the credential prompt instead of the module cards, since the cards come from `GET /api/fleet`; the demo can publish a read-only dashboard token for that purpose.

## 34. Agent Gateway: an idempotent replay is revalidated and bound to the request

**What changed (0.4.1).** The gateway used to return the stored result of an earlier idempotent call under the key `tenant:subject:capability:key` before it checked the caller again, and the key was not bound to the arguments, the workspace or the authority the call ran under. A stored result is now served as a call:

| | before | now |
|---|---|---|
| Visibility, grants, OAuth scope | not checked on a replay | checked first; a caller who no longer holds them gets the ordinary denial |
| Authority guard (envelope, revocation of an envelope or a child agent, child credential, expiry) | not checked on a replay | checked without reserving or consuming the call allowance; an invalid credential gets the same answer as on a first call |
| What a key is bound to | tenant, subject, capability | per tenant: workspace, subject, authority key (the child, when a child credential is used), capability and the canonical arguments |
| Same key, different request | the earlier result | `idempotency_conflict` (no earlier result, no arguments) |
| Same key while the first call runs | a second execution was possible | `idempotency_in_progress`; of any number of concurrent retries one executes |
| Egress policy on the stored output | not asked | asked again: it may withhold the output (deny, require approval) or, when its action is now higher-ranked than the action recorded with the stored result (order: allow, tokenize, summarize, redact, require approval, deny), redact, tokenize or summarize it; only the rank of the action is compared, so a policy that stays at `redact` but masks more fields is not applied to an output that is already stored, and an unchanged policy does not transform it a second time |
| Audit | a replay left no event | an event with the policy rule `idempotent_replay`, or the conflict, without arguments or results |
| Store | unbounded, no expiry | `InMemoryIdempotencyStore(ttl_s=86400, max_entries=10000)`: when full, the oldest completed *read* entries are evicted first; the completed entry of a write is kept until its TTL ends, an in-flight claim is never evicted, and a new keyed call that finds no room fails with `gateway_error` |

**Which calls are covered.** A call that carries an `idempotency_key` is covered when its capability is declared `idempotent` **or is side-effecting**. A key on a write means "do this once": a retry of a successful write with the same key replays the stored result and does not send again, whether or not the capability is declared `idempotent`. This includes a result that a person established with `reconcile_call(executed=True, result=...)`: the next retry gets that result, not a second send. It also includes a write whose output the egress policy holds for export approval: the write executed, so the call returns `pending_approval` and its held outcome is stored (with the unfiltered output, in this process's memory, never returned to a client from there); a retry replays `pending_approval` and never sends the write again, and once the policy lets the output out a retry returns it. A held output follows the policy of the moment it is replayed (allow, redact, deny or hold). Without a key, or for a read that is not declared `idempotent`, nothing is stored and every call executes, as before.

**Behaviour you may notice.**

* A client that reused one idempotency key for different arguments, a different workspace or a different capability now receives `idempotency_conflict`; use a new key per distinct request. The two new codes are in the fixed list of codes a client can receive (section 19).
* Keys are per tenant, not per caller: a colleague (another subject or workspace of the same tenant) who sends the same key for the same capability gets `idempotency_conflict` until the entry's TTL ends or it is evicted. Use keys that are unique to the request (for example a UUID). Another tenant's identical key is independent and sees nothing.
* Arguments that JSON cannot carry (objects, bytes, non-finite numbers), more than 256 KiB of canonical arguments, nesting deeper than 16 levels, two argument names that are equal after Unicode NFC normalisation, or a key outside 1 to 256 characters cannot be bound to a key: the call fails with `gateway_error` (detail in the server log) when it carries an idempotency key for a covered capability. The canonical form is built in full before the size check (a list of five million integers is rejected in about 0.4 s).
* A replay is refused with the ordinary denial once the caller's grant, scope or authority is gone, even if the original call succeeded. A replay does not ask the approval store or the budget again, because nothing executes.
* After the TTL or an eviction a key is unknown and the call runs again. A call whose outcome is unknown stays held by the authority ledger (`authority:outcome_unknown`) until a person reconciles it; that state is not stored in, and is not affected by, the idempotency store. After `reconcile_call(executed=True, result=...)` a retry with the same key replays the reconciled result (the fingerprint recorded with the reservation must match; a reservation created before 0.4.1 has none, and a retry then gets `idempotency_conflict` rather than a second send). After `reconcile_call(executed=False)` the reservation is returned and a retry executes: a person attested that the effect did not happen.
* Without an authority guard there is no `outcome_unknown` state, as before: a side-effecting call that raised is not recorded and a retry runs.
* Under an authority guard, a concurrent retry of a side-effecting direct call while the first call is still running is refused with `authority:outcome_unknown` (the reservation of the running call is dispatched), not with `idempotency_in_progress`; `idempotency_in_progress` is what concurrent retries of an idempotent read, or of a call without an authority guard, receive.

**A write can be sent again when its key is no longer remembered.** The idempotency store is a cache with a time-to-live, not a ledger. A write that carries the same key is sent again when its entry has expired (24 hours by default), after a process restart, when a second worker process has its own empty in-memory store, and with a custom store that evicts completed entries; the built-in store does not evict a completed write before its TTL ends. For production, use a durable store shared by every worker (an implementation of the `IdempotencyStore` protocol over a database) and keep the authority guard configured: a reservation that was dispatched and not resolved stays `authority:outcome_unknown` regardless of the store. The authority ledger also holds the settled reservation of a successful call; consulting it for a write whose store entry is gone is a possible future change and is not part of this release.

**Limits of the built-in store.**

* The entry bound (`max_entries`, default 10000) is shared by all tenants. When the store is full the oldest completed read entry of the tenant holding the most entries is evicted first, so a busy tenant evicts its own reads before another tenant's. Completed writes and in-flight calls are never evicted: when only those remain, any new keyed call (read or write) fails with `gateway_error` until entries expire, so a tenant that keeps many keyed writes in the store can use up the room of all tenants. Without `max_entries_per_tenant` (default: no lower cap) a smaller tenant's new claims can still evict part of a heavy tenant's excess reads and a heavy tenant can fill the store with writes; set `max_entries_per_tenant` (and size `max_entries` for your write rate times 24 hours) for shared deployments. An evicted read key is unknown and its call runs again.
* The bound counts entries, not bytes: a stored output has no size cap.
* The TTL is measured on the wall clock (`time.time`); a clock that moves backwards extends it and one that jumps forward ends it early.
* An in-flight claim has no timeout: a call that never returns keeps its key "in progress" and its entry unevictable until the process restarts.
* The store is local to one process; several worker processes need a shared implementation of the `IdempotencyStore` protocol.

**Custom stores.** The `IdempotencyStore` protocol changed from `get`/`put` to `begin`/`complete`/`abandon`/`seed`. A gateway given a store without those four methods refuses to be built: `AgentGateway(...)` raises `TypeError: the idempotency store must implement begin/complete/abandon/seed (missing 'begin'); the 0.4.0 get/put protocol is no longer accepted`. If your process fails at startup with that message, implement the four methods (`begin` must be atomic: of concurrent callers with the same tenant and key exactly one gets `new`) or use `InMemoryIdempotencyStore`.

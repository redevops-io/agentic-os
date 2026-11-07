# Projects mission drill-down: mount, session-to-principal bridge, data-source report

This describes what `projects_api.create_app` does with the read-only mission drill-down (`agentic_os/drilldown`), who is allowed to read it,
and what the server tells the UI about where its data comes from. Code: `agentic_os/drilldown/{mount,operator,service,api}.py`,
`agentic_os/local_capability.py`, `agentic_os/cli.py` (`permissions allow-drilldown` / `revoke-drilldown`). Tests: `tests/test_drilldown_mount.py`,
`test_drilldown_principal.py`, `test_drilldown_operator.py`, `test_event_store_shrink.py`.

## What is mounted, and when

The drill-down routes exist only when the app is given a runtime-backed event store, a grant store, an audit sink and the project the store's missions belong to:

* by injection: `create_app(provider, drilldown=DrilldownConfig(store=..., grants=..., audit=..., project_id=...))`; or
* from the environment (read by `create_app` when no config is injected; unset = not configured):

| Key | Meaning |
|---|---|
| `PROJECTS_DRILLDOWN_EVENT_PATH` | path of the event log (JSONL `EventStore`, the log a `MissionRuntime` appends to; opened by the server as a reader) |
| `PROJECTS_DRILLDOWN_EVENT_BACKEND` | `jsonl` (default) or `duckdb`; `memory` is refused |
| `PROJECTS_DRILLDOWN_PROJECT` | the project id (permission scope) of every mission in that store; the bundled UI uses `customer-ops` |
| `PROJECTS_DRILLDOWN_GRANTS_DIR` | the permission plane's grant store directory (needs `PERMISSIONS_KEY` or the key file of `agentic-os permissions init --key-only`) |
| `PROJECTS_DRILLDOWN_AUDIT_PATH` | hash-chained audit log; a read that cannot be recorded is not served |
| `PROJECTS_DRILLDOWN_AUTHORITY_DB` | optional: a `SqliteEnvelopeStore`; only reported (verified or not) by `GET /api/ui-config` |

No path is invented. If any of the first four is set and another is missing, the server refuses to start (it does not fall back to sample mode).
Without configuration there are no drill-down routes and `GET /api/ui-config` says `sample`.

## Who the caller is

There are two disjoint authentication paths, chosen by how the app was built.

**Behind the local capability gate (the desktop sidecar).** The gate (`LocalCapabilityMiddleware`) is the outermost layer. After it has validated the
`X-Agentic-Session` of a request (host, origin, fetch-site, CSRF on writes) it removes any `agentic_os.local_session` value that arrived with the
request and sets that scope key to the `Session` it validated. `LocalSessionBridge` reads it, asks the capability that issued it whether it is
still live (`session_is_live`: identity of the object, not equality; expired and signed-out sessions fail), and only then issues the principal
`local-owner` (method `local-capability`, tenant `default`, no roles) through the process's `PrincipalIssuer` (`mission/principal.py`). A header, query
parameter, cookie or body cannot name or select a principal, the browser holds no bearer token, and no route accepts one. If the issuer or the capability
is missing or broken, nobody is authenticated. Under the gate a `Bearer` credential is not consulted at all.

**Without the gate (hosted).** `Authorization: Bearer <token>` through `control_auth` (`ControlTokenAuthenticator`: the admin, tenant or approver
tokens; nothing configured, a wrong token or an invalid token configuration authenticates nobody), or an `Authenticator` the deployment injects. The
principal is `admin`, `requester:<tenant>` or `approver:<tenant>` with that tenant and **no roles**, so the permission plane's `admin` role bypass is not
inherited: the principal needs its own `evidence.<project>.inspect` grant. A session header means nothing there.

In both paths `DrilldownService` then (1) verifies the principal's proof, issuer and age (default 300 s; a forged, edited, foreign-issuer or stale principal
is a 401 and the unverified id is not written to the audit log), (2) hides a mission recorded under another tenant (the principal's tenant must equal the
mission's recorded tenant, or be `*`; a mission with no recorded tenant is open to any tenant), (3) asks the permission plane for
`evidence.<project>.inspect` on every request, and (4) writes the audit receipt before returning anything.

## Possession of the launch capability is not permission

A fresh installation has no grant. The session holder reads nothing: every route answers the same 404 body as an unknown mission. The operator gives
and takes away the permission with the product's own command, which also records it:

```
agentic-os permissions allow-drilldown  --project customer-ops --audit "$PROJECTS_DRILLDOWN_AUDIT_PATH" --dir "$PROJECTS_DRILLDOWN_GRANTS_DIR" [--operator NAME]
agentic-os permissions revoke-drilldown --project customer-ops --audit "$PROJECTS_DRILLDOWN_AUDIT_PATH" --dir "$PROJECTS_DRILLDOWN_GRANTS_DIR"
```

The grant is an ordinary entry (user `local-owner`, tool `evidence.<project>.inspect`, action `read`) in the encrypted grant store, so `agentic-os
permissions list` shows it and `revoke <id>` removes it too. `allow-drilldown` appends a `drilldown.grant` receipt (operator, grant id) to the audit log and
removes the grant again if that receipt cannot be written; `revoke-drilldown` removes first and then records a `drilldown.revoke` receipt. The grant store is read
on every request, so a revocation applies to the next request of the same open session. There is no HTTP route that creates a grant, and the grant store
has no expiry field, so a grant lasts until it is revoked (the session itself expires with the capability's session TTL).

## Routes

| Route | Auth | Policy | Failure modes |
|---|---|---|---|
| `GET /api/projects/{project}/missions/{mission}/timeline` | gate session (bridge) or Bearer (hosted) | read-only; `evidence.<project>.inspect` per request | 401 no/invalid credential or principal; 404 (one body) unknown mission, other project, other tenant, no grant; 400 bad parameters (after authorization only); 503 audit log cannot be written (nothing returned); 304 only to an authorized caller |
| `GET .../evidence` | same | same | same |
| `GET .../evidence/{id}` | same | same | same, plus 404 for an unknown item |
| `GET /api/ui-config` | gate session or Bearer for the details; the mode itself is not secret | read-only; no grant needed | under the gate: 401 without a session; the stores and integrity facts are given only when a principal can be resolved |

All four are `GET`; the mount adds no write route, so `PROJECTS_ROUTE_POLICIES` is unchanged. They are registered ahead of the UI's catch-all static mount.

## `GET /api/ui-config` (`aos.ui-config/v1`, closed schema: `drilldown/schema/ui-config.v1.schema.json`)

`data_source`: `runtime` only when a store is configured and the routes are mounted, else `sample`. `drilldown.mounted`, `projections.basis` (`sample` while the
provider is the sample projection), `drilldown.integrity` and `stores[]` (kind, durable, the store's own tail-anchor status: `ok`, `lagging`, `unanchored`,
`truncated`, `mismatch`, `invalid`, `not_reported`) and `authority` (`configured`, `verified` = the result of `SqliteEnvelopeStore.verify()`). Nothing is claimed
that the store did not report. The Timeline carries the same checks per mission as an optional `chain` object (`ok`, `checked`, `unchained`, `anchored`,
`anchor.status`); a store that runs no integrity check omits it and the UI says "not reported by this server". A log file found shorter than what the
reader already read is reported `truncated` and the store refuses to append at the old offset.

The UI reads this once at start-up and when a mission's Refresh is pressed. There is no build flag.

## Running the desktop in `runtime` mode (today)

1. A runtime must write a JSONL `EventStore` that the sidecar can read (for example `MISSION_EVENT_BACKEND=jsonl`, `MISSION_EVENT_LOG=<path>` for the process that runs missions). The desktop does not run one yet and no durable ledger is created in the application-data directory.
2. `agentic-os permissions init --key-only --dir <perm dir>` (or `PERMISSIONS_KEY`).
3. Start the sidecar with the four required `PROJECTS_DRILLDOWN_*` keys. The audit path and the permissions directory must be writable by the sidecar.
4. Run `allow-drilldown` (above). Until then every mission answers "No recorded history to show".
5. Open a mission by its id: `#/missions/<mission id>/timeline`. The Missions list is still the sample projection (the UI says so); a runtime-backed list needs a runtime projection provider.

## Not covered

Server-sent updates, reconstruct/resume/re-execute, a durable ledger created by the desktop, a runtime projection provider (mission list), the hosted
sidecar's source-root and control-token checks that return 503 behind the desktop gate, per-grant expiry, and identity providers other than the single
local owner / control tokens.

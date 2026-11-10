# Registering a source folder on the desktop

Purpose: say how the desktop sidecar lets the person using the app register a folder as a source ("use the files in <folder> as context"), what the sidecar
checks, what it records and what it does not protect against. Implementation: `agentic_os/desktop_sources.py` (authority, audit, revocation),
`agentic_os/source_folder_policy.py` (which folders), the routes in `agentic_os/projects_api.py`.

## Why there is an authority at all

A `files` source makes the server walk a folder on the user's disk. `POST /api/sources/confirm` therefore refuses a folder outside
`$PROJECTS_SOURCE_ROOTS` unless the caller presents the control token (`AGENTIC_OS_CONTROL_TOKEN`; 503 when none is configured, 401 when wrong). A desktop
sidecar has no operator and no configured token, and the browser must not hold one, so with the local capability gate on, registration answered 503 for
every folder.

## The control token

* Created when the app is built with the local capability gate (`AGENTIC_OS_LOCAL_CAPABILITY` not `off`) and no token of any kind is configured
  (`AGENTIC_OS_CONTROL_TOKEN`, `AGENTIC_OS_TENANT_TOKENS`, `AGENTIC_OS_APPROVER_TOKENS`; checked again on every request, so configuring one switches the
  automatic token off). 32 random bytes (`secrets`), held in the memory of the sidecar process. A new one at every start.
* Not written to the environment, a file, a log, an audit record, a response, a header, a URL, `GET /api/ui-config` or the About record, and not inherited
  by a child process (it is never in the environment). The object that holds it cannot be pickled or copied.
* A hosted deployment (no gate) never has one. With a token configured, behaviour is exactly that of `origin/main`.

## Who can use it, and for what

The gate validates the request first (Host allow-list, Origin, Fetch-Site, the session in `X-Agentic-Session`, the CSRF header) and records the session it
validated in the ASGI scope (`agentic_os.local_session`; the gate removes any inbound copy). Server-side code then re-checks that the session is live (the
object the capability issued, not an equal-looking one) and re-checks Host, Origin and Fetch-Site, and only then uses the token. Nothing in a header,
query string, cookie or body can name, select or supply it.

| Route | Effect | Local session | Hosted |
|---|---|---|---|
| `POST /api/sources/propose` | proposes sources from text; records which folders this session was offered | gate session | unchanged (open) |
| `POST /api/sources/confirm` | registers the folders | uses the token, folder policy below | unchanged (`$PROJECTS_SOURCE_ROOTS` or control token) |
| `POST /api/sources/remove` (new; body `{project_id, source_id}`) | revokes one registration of THIS session before it returns | uses the token | control token (503/401 as for confirm outside the roots) |

Policy entries (`projects_route_policy.py`): `confirm` stays `human_only`; `remove` is `authority_required` (it only narrows what the system may read).
Every other write route answers the local session exactly as before (for example the policy promotion cycle and authoritative outcomes: 503, no token).

## Which folders

None by default. A folder is registered only if

1. it is a folder the user was offered in a proposal made by the same session for the same project (`propose`, then "Looks right - connect"), within one
   hour; or it lies inside an explicit `$PROJECTS_SOURCE_ROOTS` (which still restricts: a proposed folder outside it is refused, and it is never widened;
   inside it no proposal is needed, as on `origin/main`); and
2. it passes the folder policy. The exact normalised and resolved folder is what is scanned and registered; never a parent, never a pattern.

| Refusal code | Rule |
|---|---|
| `invalid_path` (422) | not text, empty, over 4096 characters, a control character, `*` or `?`, `~user`, on Windows forms also `< > " \|`, a `:` after the drive, a name ending in a dot or space, a UNC path with no share |
| `not_absolute` (422) | relative, drive-relative (`C:dir`), rooted without a drive |
| `device_path` (403) | `\\?\`, `\\.\`, `//?/`, `//./`; Windows reserved names (`NUL`, `CON`, `COM1` ...) in a Windows-form path |
| `unc_admin_share` (403) | a UNC share ending in `$` |
| `drive_root`, `share_root`, `filesystem_root` (403) | `C:\`, `\\server\share`, `/` |
| `home_directory` (403) | the home directory itself (a folder inside it is fine) |
| `protected_location` (403) | `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube`, `~/.config/gcloud`, `~/.docker`, `~/.azure`, `~/.config/gh`, macOS keychains, cookies, mail, Safari and browser profiles, Linux and Windows browser profiles, `Microsoft\Credentials|Protect|Vault`, `/etc`, `/dev`, `/proc`, `/sys`, `/System`, `%SystemRoot%`, and the application's own data, state, grant, audit, key and secrets directories (including those named by the environment), and anything inside them |
| `contains_protected_location` (403) | a folder that holds one of the above (the directory above home, `~/.config`, `~/Library`, `%APPDATA%`, `%LOCALAPPDATA%`, the directory that holds the audit log ...) |
| `not_found` (422), `not_a_directory` (422) | missing or unreadable; a file, device node, socket or pipe |
| `outside_allowed_roots` (403) | outside an explicit `$PROJECTS_SOURCE_ROOTS` |
| `not_proposed` (403) | not offered in this session's proposal for this project |

The list is compared case-insensitively and Unicode-normalised (NFC) against both the lexical path (`..`, `.`, repeated and trailing separators collapsed) and
the real path (symlinks, junctions and 8.3 short names resolved by the operating system, `..` evaluated after the links in front of it). A refusal carries
the code, fixed wording and the normalised requested path, never the real path (the audit has both). Windows rules are text rules tested on every host
through an injected resolver; the 8.3 and junction behaviour of the real Windows `realpath` was not exercised here.

Refusal body (status 403 or 422): `{"detail": {"code": "<code>", "message": "<fixed wording>", "path": "<normalised requested path or empty>"}}`.
Unauthenticated: 401 (gate `{"error": ...}` or `{"detail": {"code": "session_required", ...}}`; also a confirm whose session ended while it ran). Registration that cannot be
recorded: 503 `audit_unavailable`, nothing registered. Removal of a source that is unknown, another session's, another project's or already removed: the same 404
`source_not_found` (no answer differs, so existence does not leak across sessions).

## Audit

`<application data>/io.redevops.agentic-apps/audit/source-registrations.jsonl` (macOS `~/Library/Application Support`, Windows `%LOCALAPPDATA%`, elsewhere
`$XDG_STATE_HOME` or `~/.local/state`; `AGENTIC_OS_SOURCES_AUDIT_PATH` overrides with an absolute path), directory 0700, file 0600. One line per registration,
refusal and removal: `receipt_id`, `ts`, `kind`, `principal` (`local-owner`), `method` (`local-capability`), `scope` (project), `action`
(`register`, `refuse`, `remove`, `session_end`), `result`, `reason` (`ok`, a refusal code, `user_request`, `session_ended`, `shutdown`, `audit_unavailable`, `failed`), `path` (normalised), `real_path`, `source_id`,
`derived` on a `remove` (`dropped`, `drop_failed`, `shared`, `none`: what became of the scanned and indexed content), `count` on a `session_end`, plus `prev` and `hash` (each line hashes its predecessor).
When a session ends with registrations, one `session_end` record (`reason` `session_ended`, `count` = how many) precedes one `remove` record per registration. A second file, `source-registrations.jsonl.anchor`, records the line count and the last hash
(`mission/integrity.py`, MAC with `AGENTIC_OS_ANCHOR_KEY` when set). `SourceAuditLog.verify()` reports `ok`, `lagging`, `unanchored`, `truncated`,
`mismatch`, `invalid` or `chain`. A change to a line, a line cut off the end, and a re-chained rewrite that no longer matches the anchor are detected by
tests; a writer who can write both files can rewrite both. No secret, token, session or CSRF value is ever in a record.

## Revocation and persistence

Owner requirement: source registrations and their capabilities are session-scoped and nonpersistent, are invalidated on sign-out or on the end of the capability
session, and Remove revokes access immediately; it does not merely hide the registration.

**Scope.** A registration belongs to the capability session OBJECT that made it (identity, not an id) and is visible to, removable by and usable by that session
only. A second window has a session of its own and neither sees nor removes the first one's registrations. The same folder registered by two sessions is two
registrations; the scanned content they share is dropped when the last of them goes.

**Eager invalidation (item: "no dependence on a later request").** `LocalCapability` reports every session that ends (`add_session_end_listener`, called
synchronously after the store's lock is released) and the authority revokes inside that call, so the sign-out response is sent after the registrations are gone:

| Ending | Where it is caught |
|---|---|
| sign-out (`DELETE /api/local/session`) | `LocalCapability.drop_session` notifies before the route replies |
| expiry | a sweeper thread (`desktop-source-expiry`, daemon, holds the authority weakly) wakes at the nearest deadline (at most every 30 s, so a clock jump or a laptop sleep is noticed within that) and calls `sweep_expired`; the gate's own purge (any request, any session) and every read also notify |
| eviction (`MAX_SESSIONS` = 16, oldest out) | `LocalCapability.new_session` reports the evicted session |
| process shutdown | the app's lifespan end (`close_on_shutdown`, also on the outer app of a mounted base path) and an `atexit` hook call `close()`: everything is revoked, reason `shutdown` |
| a session that vanished without telling anyone | `sweep()` (routes, sweeper) compares what is held against the capability |

Revoking a registration (`_detach`, then `_finish_revoked`): the registration leaves the table under the lock (from that instant no reader can see it), then the
scanned and indexed content is dropped (`indexer.drop`; the default `CountingIndexer` keeps nothing, `RagIndexer.drop` closes the store and deletes the database and
its write-ahead log), the session's proposals are dropped, and the audit records it. The audit is never a condition: if it cannot be written the registration is
still gone. If dropping content fails the registration is still gone and the record says `drop_failed`.

**Remove (`POST /api/sources/remove`)** does the same for one registration of the calling session: (a) it is out of the table before the route returns; (b) the
content is dropped; (c) a handle taken earlier is dead (below); (d) the pick (proposal) that allowed it is consumed, so a replayed confirm is `not_proposed`
until the folder is proposed again; (e) it is audited (`remove` / `user_request`). A source that is unknown, another session's or already removed is the same
404.

**Read-time authority.** Each access path (table below) asks the authority at the moment of the read. `live_projections_for`, `lease` and `guarded_read` take the
authority's lock and, inside the same critical section, read the session's liveness from the capability (the object it issued and still holds) and the table. A
session found dead by any reader is revoked on the spot. A `Lease` is a handle on one registration at one epoch (`generation`, taken from a counter, and the
registration object itself); `Lease.check()` raises `SourceRevoked` after Remove, after the session ended, and after the same folder (the same source id) is
registered again.

Exact semantics for a read that overlaps a revocation (`guarded_read(session, project, source_id, fn)`, `RagIndexer.retrieve` with `authorize` bound):

* `fn` runs outside the lock (a read can be slow, and Remove never waits for a reader).
* The final check takes the lock that Remove and the end of a session take to revoke. A read whose final check comes BEFORE the revocation returns its value (the
  revocation follows it). A read whose final check comes AFTER is refused (`SourceRevoked`) and whatever `fn` produced is discarded, so no content is returned
  after the revocation has taken effect.
* A read that STARTS after Remove, sign-out or expiry has taken effect never runs `fn`.
* Remove, sign-out and expiry are linearised with every read by that lock; the capability's own state (what `session_is_live` reads) changes before listeners
  are told, so a reader in that gap already sees a dead session.
* Not guaranteed: bytes a reader has already received (a response in flight on the socket, a value returned before the final check) are not recalled; the check
  and the return of the value are not one atomic step across the process boundary.

**Nonpersistence.** Registrations, proposals, the token and the indexed content (default indexer) are in memory. The only thing written is the audit log (and its
anchor); the audit log is a record of events: nothing reads it to learn a registration, a session or an authority (the only readers are `SourceAuditLog.verify` and the
tally behind the next append). A new process, and a new session in the same process, start with zero registrations; the old session's credentials are refused by the
gate (it does not know them), and a proposal made to an ended session cannot be confirmed by another. Nothing is written to the environment, a keychain-like store
or the temporary directory. If an on-disk indexer is bound (`RagIndexer`, `PROJECTS_RAG`; not part of the desktop path today) its files are deleted at revocation; files
left by a process that was killed are not reachable from any route (retrieval needs a live registration) and are not cleaned up.

## Access paths

Every place where a registered source, or something derived from it, is read, and how it is guarded. "view" is `DesktopSourceAuthority.live_projections_for`.

| # | Read path | Code | Guard |
|---|---|---|---|
| 1 | `GET /api/projects/{id}/sources` | `projects_api.py:1946` -> `SampleProjectionProvider.sources` `:732` -> `session_source_view` | `current_projections` -> view: this request's session, judged now; the route also `sweep()`s |
| 2 | `GET /api/projects/{id}/overview` (carries `sources`) | `projects_api.py:1615` -> `overview` `:769` -> `sources` `:732` | same view (this route did not sweep before: a signed-out session's registration was readable here until a sources listing ran) |
| 3 | agent gateway `sources.search` (REST `/api/agent-gateway/capabilities/{name}/invoke`, MCP `/api/agent-gateway/mcp`) | `projects_api.py:1427-1428` (`_Sources.search`) -> `prov.sources` | same view. The gateway routes sit behind the gate, so a gateway call carries a session: it sees ITS session's registrations, never another's, never a dead session's |
| 4 | gateway `projects.*` overview projection | `projects_api.py:1419` -> `prov.overview` | same view |
| 5 | listing of the registration table itself | none: no route returns `_registrations`, the token or a session | tested |
| 6 | file, preview and read routes for a registered folder | none exist: a registration is a name, a location, counts and a fingerprint; the sidecar serves no file content from it | n/a (tested: no route under `/api` takes a source id other than `remove`) |
| 7 | scan and index at confirm | `projects_api.py:1097` `confirm_and_connect_sources` via `desktop_sources.py:616` `confirm` | the scan happens before the registration exists; it enters the table invisible (`pending`), is recorded, then shown, each step re-checking the session; a session that ends in between leaves nothing (`_unwind`) and its content is dropped |
| 8 | index retrieval (`RagIndexer.retrieve`; the Sidekick "use these files as context" path once an on-disk index is bound) | `sources_rag.py:126` | `authorize` (bound to `authorize_current`) before the search and a lease check after it; `drop` deletes the store |
| 9 | Sidekick answers | `projects_api.py:1165` `sidekick_reply` | reads no source or registration today (tested: a removed source's name, path and id are in no answer); any future context builder must use paths 3/8's guard |
| 10 | `GET /api/ui-config`, drill-down, app directory | `drilldown/mount.py`, `app_directory.py` | do not read sources (searched; tested for the report) |
| 11 | the provider's own `session_sources` | `projects_api.py:304-748` | used only by the hosted path and by a desktop build that has a control token configured (operator registrations, not capability sessions); the desktop path never writes it |
| 12 | proposals | `desktop_sources.py:409` | dropped with the session (`_sessions_ended`, `_purge_proposals`) and by Remove |
| 13 | audit log | `desktop_sources.py` `SourceAuditLog` | a record, never read to grant |
| 14 | sweeper thread, `atexit`, lifespan | `desktop_sources.py:566`, `:556`, `:263` | revocation only; hold the authority weakly |

## Tests

`tests/test_desktop_sources_revocation.py` (every guard was mutated and the test that kills it is in the report) plus the updated `tests/test_desktop_sources.py`:
eager (sign-out before it returns, the store dropping a session, eviction, expiry by the sweeper with no request, expiry with no sweeper, the capability's purge, a vanished
session, shutdown), scope (another session cannot list, remove or replace; the same 404 for unknown and another session's; equal-looking live sessions are two sessions;
a forged scope session), reads (overview, gateway, look-alike sessions, a read straddling Remove and sign-out, a reader thread, Remove not waiting for a reader, an old
lease after re-registration, a lease of another session, index retrieval and drop), the in-flight cases (sign-out during a scan, between the record and the visibility,
Remove arriving during a confirm, a failed audit unwinding a scan, confirm racing sign-out over HTTP in 25 rounds), audit unavailable (sign-out, Remove, expiry), persistence
(restart with the disk left as it was, a forged audit log, the audit log is not read, a new session starts empty, a proposal for an ended session, replays of propose,
confirm and remove, environment, keychain-like modules, temporary directory), exposure (the token, the session and CSRF values and other sessions' sources are in no
response, error or log), the removed source in no Sidekick answer, the four endings against every read path, and no dynamic imports in the changed modules.
The browser test (`tests/test_desktop_sources_browser.py`, opt-in) registers, removes, reloads (gone), registers again, signs out (the old tab shows the signed-out
panel; the server already holds nothing) and opens a new session (no sources; the old pick is refused).

## What is not protected

* A process running as the same OS user can read the sidecar's memory (the token, the registrations) and the launch capability, and can call the routes itself.
* Content that was already shown to the person, or already received by a reader before the revocation took effect, is not recalled (see the read semantics above).
* Revocation is immediate for the server-side access paths in the table. Registrations made with a configured control token (an operator, no capability session) are the operator's and are not session-scoped.
* Files a killed process left behind from an on-disk indexer are not cleaned up (they are not reachable through any route).
* The launch capability gives its holder this authority: a stolen launch code (single use) or session token (per Host) can register the folders it can
  also have proposed, within the policy above, and remove registrations. It gives nothing else: no other write route, no grant, no bearer.
* A request with no `Origin` header (a non-browser caller that already holds the session and the CSRF token) is accepted; a request with a mismatching
  `Origin`, a foreign `Host` or a cross-site Fetch-Site is refused.
* The folder policy is a list. A credential directory that is not on it can be registered. A folder with a name that differs from a protected one only
  by a link created after the check is scanned by its real path as checked, but a race between the check and the scan is not closed.
* The audit is a record, not a guarantee against someone who can write the application data directory.

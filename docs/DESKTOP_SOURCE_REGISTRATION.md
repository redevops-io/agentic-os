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
| `POST /api/sources/remove` (new; body `{project_id, source_id}`) | forgets a registered source | uses the token | control token (503/401 as for confirm outside the roots) |

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
Unauthenticated: 401 (gate `{"error": ...}` or `{"detail": {"code": "session_required", ...}}`). Registration that cannot be recorded: 503
`audit_unavailable`, nothing registered. Unknown source on removal: 404 `source_not_found`.

## Audit

`<application data>/io.redevops.agentic-apps/audit/source-registrations.jsonl` (macOS `~/Library/Application Support`, Windows `%LOCALAPPDATA%`, elsewhere
`$XDG_STATE_HOME` or `~/.local/state`; `AGENTIC_OS_SOURCES_AUDIT_PATH` overrides with an absolute path), directory 0700, file 0600. One line per registration,
refusal and removal: `receipt_id`, `ts`, `kind`, `principal` (`local-owner`), `method` (`local-capability`), `scope` (project), `action`
(`register`, `refuse`, `remove`), `result`, `reason` (`ok`, a refusal code, `user_request`, `session_ended`), `path` (normalised), `real_path`, `source_id`,
plus `prev` and `hash` (each line hashes its predecessor). A second file, `source-registrations.jsonl.anchor`, records the line count and the last hash
(`mission/integrity.py`, MAC with `AGENTIC_OS_ANCHOR_KEY` when set). `SourceAuditLog.verify()` reports `ok`, `lagging`, `unanchored`, `truncated`,
`mismatch`, `invalid` or `chain`. A change to a line, a line cut off the end, and a re-chained rewrite that no longer matches the anchor are detected by
tests; a writer who can write both files can rewrite both. No secret, token, session or CSRF value is ever in a record.

## Revocation and persistence

* `POST /api/sources/remove` forgets a registered source at once; the next listing no longer has it.
* A registration belongs to the capability session that made it. When that session is signed out (`DELETE /api/local/session`) or expires, its
  registrations are forgotten on the next request that lists or changes sources, recorded as `session_ended`.
* Registered sources stay in memory only ("(this session only)"); nothing about them is persisted. The only thing written is the audit log.

## What is not protected

* A process running as the same OS user can read the sidecar's memory (the token) and the launch capability, and can call the routes itself.
* The launch capability gives its holder this authority: a stolen launch code (single use) or session token (per Host) can register the folders it can
  also have proposed, within the policy above, and remove registrations. It gives nothing else: no other write route, no grant, no bearer.
* A request with no `Origin` header (a non-browser caller that already holds the session and the CSRF token) is accepted; a request with a mismatching
  `Origin`, a foreign `Host` or a cross-site Fetch-Site is refused.
* The folder policy is a list. A credential directory that is not on it can be registered. A folder with a name that differs from a protected one only
  by a link created after the check is scanned by its real path as checked, but a race between the check and the scan is not closed.
* The audit is a record, not a guarantee against someone who can write the application data directory.

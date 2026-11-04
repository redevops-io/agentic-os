# Local API security: the sidecar's loopback capability

Purpose: the desktop sidecar (`redevops-sidecar serve`, `agentic-os-projects`) serves the Projects UI
and its API on `127.0.0.1`. Loopback is not an access control. Any process on the machine, and any web
page open in the user's browser, can send requests to it. This document describes how the API is made
deny-by-default, what that protects, what it does not, and the exact contract a launcher implements.

Implementation: `agentic_os/local_capability.py` (the gate), `agentic_os/local_serve.py` (`serve`),
`agentic_os/local_capability_ui.py` + `agentic_os/local_capability_shim.js` (UI side),
`agentic_os/projects_api.py` (hook: `create_app(capability=...)`, `mounted_app`, `main`).
Tests: `tests/test_local_capability.py`, `tests/test_local_api_attack_regression.py`.

## 1. The defect this fixes

In the frozen v0.3.2 sidecar the app was built with `CORSMiddleware(allow_origins=["*"])` and no
authentication. A web page the user visited could run

```
fetch("http://127.0.0.1:8787/api/sources/confirm", {method: "POST", headers: {"Content-Type": "application/json"},
       body: JSON.stringify({sources: [{kind: "files", location: "/Users/<name>/Documents"}]})})
```

and read the answer: the sidecar walked the folder and returned file counts and names. Evidence level
OBSERVED twice: against the copied unsigned sidecar (audit), and again for this work in a headless
Chromium against the feeca065 tree (status 200 with the folder listing; the same page against the fixed
tree gets a 403 on the preflight and a 403 on the simple POST, and nothing is read).

## 2. Threat model

| Actor | Capability | Protected? |
|---|---|---|
| Malicious web page in the user's browser (cross-origin `fetch`, forms, `<img>`, navigations) | Send requests to `127.0.0.1:<port>` with the browser's ambient state | Yes |
| DNS rebinding page (attacker hostname re-pointed to 127.0.0.1) | Same-origin from the browser's view; sends `Host: attacker` | Yes (Host allowlist) |
| Other local web app or server on another port of 127.0.0.1/localhost (same-site) | The browser sends cookies for the host to it, whatever the port; it can make requests to us | Yes: the session is not a cookie (it is a header token kept in the UI origin's `sessionStorage`, which is port-scoped), and requests are also checked by Origin, `Sec-Fetch-Site` and the CSRF header |
| Another OS user on the same machine (shared host) | Connect to the loopback port; read other users' process arguments; list or read files in world-readable places | Yes for the secret (memory only, never in argv or on disk) and the session. The single-use launch code never appears in any argv: the launcher hands the OS opener only the path of a private redirect file (section 4.3). Residual: a same-user process can read that file during its 60 s lifetime (same-user malware is out of scope); Windows ACL behaviour of the file and directory is UNVERIFIED (section 8). |
| A stale or foreign process holding the sidecar's port | Be mistaken for the sidecar by the launcher | Yes (HMAC handshake, section 5) |
| Malware running as the same OS user | Read launcher memory, the browser's storage, the process list, attach a debugger | NO. Out of scope. |
| A compromised user session or browser (extension, XSS in the UI) | Act with the session | NO (an XSS inside the UI origin acts as the user; the CSP reduces this surface) |
| Network attacker | Reach the port | Not applicable while bound to loopback; with `PROJECTS_HOST` non-loopback the traffic is plain HTTP (section 7) |

The controls are layered so that no single browser mechanism (CORS, `Origin`) is relied on:

1. Capability required for every request (secret-derived session). This is the control.
2. Host allowlist, `Origin` same-origin, `Sec-Fetch-Site`, custom CSRF header, refusal of preflights:
   defence in depth for the browser case.
3. Loopback bind, memory-only secret, headers (CSP, `nosniff`, `no-referrer`, frame denial, `no-store`).

## 3. Behaviour

Deny by default. Every request needs a session, except:

* `GET`/`HEAD` of the static UI files (the exact set of files in the UI directory, plus `/` and
  `/index.html`, plus `/local-capability-shim.js`; dot-files, `/api/...` and `/docs`, `/redoc`,
  `/openapi.json` are never public);
* `POST /api/local/session` (exchange a launch code), `POST /api/local/handshake` and
  `POST /api/local/launch-code` (launcher, authenticated by HMAC proof), which are answered by the gate
  itself and never reach the app.

All 21 API routes, `/docs`, `/redoc`, `/openapi.json`, unknown paths (they answer 401, not 404, so the
route table is not an oracle), and the agent-gateway routes when `AGENT_GATEWAY_TOKEN` enables them,
require the session. The gate is an ASGI middleware wrapped around the outermost app, so it also covers
`PROJECTS_BASE_PATH` mounts. WebSocket connections are refused.

Order of checks for a gated request: Host, preflight refusal, Origin, `Sec-Fetch-Site`, then the route
class (session endpoints, or the `X-Agentic-Session` header and, for non-`GET`/`HEAD` methods, the CSRF header).

No CORS: no `Access-Control-*` header is ever emitted (any the app produced is stripped); `OPTIONS`
requests carrying `Access-Control-Request-Method` get 403. `create_app()` no longer defaults to a
wildcard; `allow_origins` is an explicit opt-in and is rejected together with the gate.

## 4. Wire contract (for the launcher)

### 4.1 Process start and the hand-off record

Environment: `AGENTIC_OS_HANDOFF=stdout` or `AGENTIC_OS_HANDOFF=fd:<n>` (n is an inherited, writable
file descriptor), `PROJECTS_PORT=0` (recommended: the sidecar binds first and reports the real port;
the default without it is 8787), optional `PROJECTS_BASE_PATH`.

The sidecar binds `127.0.0.1` only (a non-loopback `PROJECTS_HOST` is refused in this mode), starts
listening, then writes exactly one ASCII JSON line to the channel. The line is written as **bytes** and
ends in a single `\n` (never `\r\n`, even on Windows; a launcher should still tolerate a trailing `\r`):

```json
{"v":1,"proto":"agentic-os-local-capability/1","event":"ready","host":"127.0.0.1","port":50123,"pid":4242,"base_path":"","secret":"<43 chars base64url>"}
```

`launch_code` (43 chars base64url) and `launch_code_ttl` (60) are **optional fields**. In launcher mode they
are omitted by default, because a launcher that mints a fresh code for every open (4.4) does not use the
first one and an unused code would hold one of the 8 outstanding-code slots for its lifetime. Set
`AGENTIC_OS_HANDOFF_LAUNCH_CODE=1` in the sidecar's environment to have them included (the code is then
single use and valid `launch_code_ttl` seconds from the moment the line is written). A launcher must
accept the record with or without these two fields and must ignore them if it mints its own codes.
Required fields: `v`, `proto`, `event`, `host`, `port`, `pid`, `base_path`, `secret`.

* `secret`: 32 random bytes, base64url without padding. The launch secret. Held in memory by the sidecar
  and the launcher only. It never appears on a command line, in a URL, in a log, on disk, or in an
  HTTP message (the handshake proves possession with an HMAC).
* With `stdout`, the line is the first thing on stdout, the banner moves to stderr and uvicorn's access
  log is disabled, so stdout carries nothing else of interest. The launcher must read the line into
  memory and MUST NOT forward it to its own logs, crash reports, or the UI.
* On failure the sidecar writes `{"v":1,"event":"error","code":"bind_failed","detail":"..."}` (no
  secret) and exits 1; configuration errors exit 2 with a message on stderr. If the port is already
  taken (for example by a stale sidecar), the launcher sees `bind_failed` and must not adopt whatever
  is listening there.
* `AGENTIC_OS_HANDOFF` combined with `AGENTIC_OS_LOCAL_CAPABILITY=off` is refused (exit 2).

### 4.2 Handshake (the launcher authenticates the sidecar)

```
POST /api/local/handshake            Content-Type: application/json
{"client_nonce": "<22..86 base64url chars, fresh random per call>"}

200 {"v":1,"proto":"agentic-os-local-capability/1",
     "proof":"<base64url(HMAC-SHA256(secret, "agentic-os-handshake-v1\n" + port + "\n" + client_nonce))>",
     "server_nonce":"<22 base64url chars>","expires_in":30}
```

The key is the raw 32 secret bytes (base64url-decoded). `port` is the decimal port the launcher
connected to; `client_nonce` is used as the ASCII string that was sent. The launcher MUST recompute the
proof and compare in constant time before doing anything else; on mismatch it kills what it spawned,
reports the sidecar as untrusted, and never opens the browser. This is what makes a stale or foreign
process on the port unadoptable: it cannot know the secret that this launch's sidecar generated.

### 4.3 Opening the UI

The browser has to arrive at

```
http://127.0.0.1:<port><base_path>/#lc=<launch_code>
```

The code is in the URL fragment: the browser never sends it to the server, and it is not in access logs
or `Referer`. The page's script removes the fragment from the address bar immediately and exchanges it
(section 4.5). The page must be loaded by a browser at the sidecar origin itself (same origin as the API);
an app-bundled page on another origin (for example a `tauri://` origin) cannot call the API, by design,
so the launcher does not load the sidecar into its own webview.

**The URL is never given to the OS opener.** A URL passed to `open` / `xdg-open` / `cmd /C start` sits in
that process's argv, which every other local user can read (`ps`, `/proc/<pid>/cmdline`) while the opener
or a freshly started browser runs (review finding SR-01). Instead the launcher (`deploy/launcher/src-tauri/src/opener.rs`,
`main.rs::open_ui`):

1. performs the verified handshake and mints a fresh code (4.2, 4.4, 4.7);
2. writes a small redirect page `open-<32 hex digits = 128 random bits>.html` into
   `<app_cache_dir()>/launch/` (on macOS `~/Library/Caches/io.redevops.agentic-apps/launch/`). The directory
   is created with mode 0700 and must be a real directory (a symlink is refused) owned by the current user
   (otherwise the open is refused); a looser existing mode is tightened to 0700. The file is created
   exclusively (`create_new`, never follows a planted name) with mode 0600. The page holds
   `location.replace("<URL>")` (and a `<noscript>` meta refresh), a `no-referrer` policy and a CSP that
   allows only its own inline script. The URL is refused unless it starts with `http://127.0.0.1:` and
   contains only inert characters (`[A-Za-z0-9:/._-#=~]`);
3. gives the opener **only that file path** as its argument (`open <path>`, `xdg-open <path>`,
   `cmd /C start "" <path>`): no URL, no code, no secret in any argv;
4. deletes the file 60 s later (the code's lifetime), sweeps any `open-*.html` older than 120 s before each
   write (crash leftovers), and deletes every file it wrote on exit. If starting the opener fails the file is
   deleted at once.

The redirect is a top-level navigation from a `file:` page to `http:`, with the fragment as part of the target,
which Chromium, WebKit and Gecko allow (observed in headless Chromium; see section 8 for the browsers not tried).

`legacy_service_warning` (launcher command, shown in the launcher window): after a start, the launcher makes a
best-effort probe of the OLD fixed port 8787 with one unauthenticated `GET /api/projects`, bounded to 300 ms. A
200 with a JSON body means a v0.3.2 sidecar (orphaned by the old launcher) is still serving an unauthenticated API
there; the launcher then shows a warning text and nothing else: it never adopts, contacts further, or stops that
process. Nothing listening, 401/403/404, a slow peer or a non-JSON body gives no warning. The new sidecar uses an
OS-chosen port and cannot collide with it.

### 4.4 Minting another launch code (re-open a window, expired code)

```
POST /api/local/handshake  {"client_nonce": ...}      -> server_nonce          (as 4.2)
POST /api/local/launch-code                           Content-Type: application/json
     X-Launcher-Proof: <base64url(HMAC-SHA256(secret, "agentic-os-mint-v1\n" + port + "\n" + server_nonce))>
     {"server_nonce": "<from the handshake>"}
200  {"v":1,"launch_code":"<43 chars>","expires_in":60}
```

The challenge (`server_nonce`) is single use and expires after 30 s; it is burned on every attempt,
right or wrong, so a failed proof cannot be retried against the same challenge.

Pinned test vectors (key = bytes `00 01 ... 1f`, port 8787):
`HMAC("agentic-os-handshake-v1\n8787\nAAAAAAAAAAAAAAAAAAAAAA") = XrELUoD7WHe1e8IgKZZYoltOOKdPcPN3PR-qWdhqCeY`,
`HMAC("agentic-os-mint-v1\n8787\nBBBBBBBBBBBBBBBBBBBBBB") = JYxsI49qlI_AzpnZPgZwDLjyQLNRgueAarKpIxB19VQ`
(both base64url, no padding; asserted in `test_proof_test_vector_for_the_rust_side`).

### 4.5 Session endpoints (used by the UI script; the launcher normally does not call them)

| Request | Result |
|---|---|
| `POST /api/local/session`, `Content-Type: application/json`, `{"code": "<launch code>"}` | 200 `{"v":1,"session_token":"...","session_header":"X-Agentic-Session","csrf_token":"...","csrf_header":"X-Agentic-CSRF","expires_in":86400}`. No `Set-Cookie`. |
| `GET /api/local/session` (header `X-Agentic-Session`) | 200 `{"v":1,"csrf_token":"...","expires_in":N}` (so a reload of the same tab recovers the CSRF token). Without a valid session header: 401, nothing is handed out. |
| `DELETE /api/local/session` (session header + CSRF header) | 200, session dropped |

The session is a header credential, not a cookie. The server never sets a cookie and never reads one.
`session_token` is 32 random bytes (base64url) held server-side only as a SHA-256 digest; it is bound to the
exact `Host` (`127.0.0.1:<port>`, `localhost:<port>`, ...) it was established on, and a request with a
different or missing `Host`, a missing header or more than one `X-Agentic-Session` header is 401. It lives
24 hours at most. Every authenticated request carries `X-Agentic-Session: <session_token>`; every
state-changing request (any method other than `GET`/`HEAD`) also carries `X-Agentic-CSRF: <csrf_token>`
(kept as defence in depth: both are non-simple headers a cross-site page cannot send without a preflight,
which is refused). The UI script keeps the token in `sessionStorage` and adds both headers to all
same-origin `/api/` fetches. At most 16 sessions and 8 outstanding launch codes exist at a time (oldest
evicted). Consequences, all by design: a browser address-bar navigation to `/docs` or `/openapi.json`
cannot carry the header and gets 401 (use a client that sets it); a reload of the same tab keeps the
session; a new tab, a bookmarked URL or a restored session needs a fresh launch code from the launcher;
a hand-started `serve` user opens the stderr URL again (restart `serve` if its 300 s code has expired).

### 4.6 Error codes

All errors are `{"error":{"code":"...","message":"..."}}` with `Cache-Control: no-store`.

| Status | `code` | Meaning |
|---|---|---|
| 400 | `invalid_host` | `Host` missing, duplicated, or not `127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>` (or a configured extra host) |
| 400 | `bad_request` | malformed or oversized (over 4 KiB) JSON, or a bad nonce |
| 401 | `auth_required` | no valid session (also for unknown paths) |
| 401 | `invalid_launch_code` | wrong, expired, or already used code (not distinguished) |
| 401 | `invalid_proof` | unknown or expired challenge, or wrong launcher proof |
| 403 | `origin_not_allowed` | `Origin` present and not exactly `http://<Host>` (includes `null`) |
| 403 | `fetch_site_not_allowed` | `Sec-Fetch-Site` is `cross-site` or `same-site`, or `none` on a non-`GET`/`HEAD` |
| 403 | `cors_not_supported` | a CORS preflight |
| 403 | `csrf_invalid` | state-changing request without the right `X-Agentic-CSRF` |
| 405 | `method_not_allowed` | wrong method on a `/api/local/*` path |
| 415 | `unsupported_media_type` | `/api/local/*` POST without `Content-Type: application/json` (a cross-site form cannot send it) |
| 429 | `rate_limited` | the failure budget of that endpoint is spent (10 failures in 60 s) and the attempt did not present a correct credential; `Retry-After` seconds. See 4.7. |

Failures counted for the limit are only those that pass the Host/Origin/fetch-site checks, so a web page
cannot lock the user out by spamming the endpoint cross-origin.

### 4.7 Rate limits and what a launcher does on 429

Exactly two endpoints are rate limited, each with its own budget of 10 FAILED attempts per rolling 60 s
(global: loopback has one client address):

| Endpoint | Counts as a failure | When the budget is spent |
|---|---|---|
| `POST /api/local/session` (the UI script's exchange) | wrong/expired/used code, malformed body | a request with a correct, unexpired, unused code is still served; a failing one gets 429 (and is not counted again) until the window passes |
| `POST /api/local/launch-code` (the launcher's mint) | wrong proof, unknown/expired challenge, malformed body | a request whose proof is **correct** is still served; a failing one gets 429 (and is not counted again) |

`POST /api/local/handshake` is not rate limited (it has no secret to guess; its state is capped at 16
live challenges of 30 s, oldest evicted). The budgets are independent: noise on the exchange endpoint
cannot block the launcher. Successful requests never count. A launcher's own attempts count only when
they fail, so a launcher that follows 4.2 to 4.4 (fresh handshake immediately before each mint, proof
computed correctly) never trips the limit by itself. It can only see a 429 or a `401 invalid_proof` on the
mint when its challenge expired or was evicted (more than 30 s between handshake and mint, or another
local process flooded `/handshake`) or when something else is answering.

What the launcher does:

1. Trust comes only from verifying the HMAC in the handshake (4.2); a 429 is never evidence of an attack
   and never evidence of trust. Do not treat a 429 as "untrusted sidecar" and do not kill the sidecar for it.
2. On `429` or `401 invalid_proof` from `/api/local/launch-code` after a verified handshake: wait
   `min(Retry-After, 5)` seconds (or 0.25 s, 0.5 s, 1 s on a plain 401), then repeat the whole
   handshake (new `client_nonce`, verify the proof again) and mint. At most 3 attempts per user action.
3. If all attempts fail, report "the local service is busy" and do not open the browser; do not fall back
   to the first launch code, a URL without a code, or any unauthenticated path.
4. A handshake whose proof does not verify is different: that is the stale/foreign-process case (4.2) and
   is final for that process.
5. 429 on `/api/local/session` concerns the browser, not the launcher, and cannot lock the user out: a
   correct code is served even while the budget is spent, so other local processes cannot deny sign-in
   by sending bad codes (reviewer finding SR-04). The throttle only slows guessing, which is already
   infeasible against 256-bit codes.
6. Expiry (codes, challenges and sessions) is checked on two clocks, the monotonic clock and the wall
   clock, and a credential is dead as soon as either passes its deadline. The monotonic clock stops while
   a laptop sleeps (macOS), so the wall clock closes that gap; a backward wall-clock jump cannot extend a
   lifetime (the monotonic bound still holds); a forward jump only fails closed (open again from the
   launcher) (SR-08).

## 5. Standalone `serve` (no launcher)

Without `AGENTIC_OS_HANDOFF`, `serve` prints on stderr (ASCII only):

```
ReDevOps Projects - UI + API on http://127.0.0.1:<port>
Open this one-time URL in your browser (single use, expires in 300 s; the part after # never leaves your browser):
  http://127.0.0.1:<port>/#lc=<code>
```

The secret is not emitted in this mode. Opening `http://127.0.0.1:8787/` by hand without the code loads
the UI but every API call answers 401 (and a second tab or a bookmark of the printed URL has no session
either: the session lives in the first tab's `sessionStorage`); the shim shows a banner telling the user to use the printed URL.
This is a breaking change for people who started `serve` and typed the address. If a code expires,
restart `serve`.

Environment: `PROJECTS_HOST`, `PROJECTS_PORT` (`0` for a free port), `PROJECTS_BASE_PATH`,
`PROJECTS_ALLOWED_HOSTS` (extra exact `Host` values for a deliberate non-loopback or proxied deployment),
`AGENTIC_OS_LOCAL_CAPABILITY=off` (explicit opt-out for a server-hosted deployment with its own edge
controls; prints a warning; refused with `AGENTIC_OS_HANDOFF`; the wildcard CORS stays removed).

## 6. What was and was not changed in the vendored UI

The vendored bundle (`agentic_os/projects_ui/`) is **not modified**. The bundle has two `fetch` call
sites (a GET helper and a POST helper, both same-origin, no auth header, no `window.open`, no
`localStorage`). `index.html` is served by a route that injects one tag,
`<script src="/local-capability-shim.js"></script>`, as the first child of `<head>` (a plain script, so
it runs before the deferred module bundle). The shim (`agentic_os/local_capability_shim.js`, about 100
lines, no eval, no inline script) exchanges `#lc=` and wraps `window.fetch` for same-origin `/api/`
requests (waits for the exchange, keeps the session token in `sessionStorage`, adds `X-Agentic-Session` and `X-Agentic-CSRF`, sends no cookies). Because the
injection happens at serve time, `scripts/build_projects_ui.sh` and `$PROJECTS_UI_DIR` need no change.

The CSP applied to every response is `default-src 'none'; script-src 'self'; style-src 'self'
'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; base-uri 'none';
form-action 'self'; frame-ancestors 'none'; object-src 'none'` (with `X-Frame-Options: DENY`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy` and
`Cross-Origin-Resource-Policy: same-origin`). `style-src 'unsafe-inline'` is kept because the UI sets
inline styles; scripts have no `unsafe-inline` or `unsafe-eval`. Verified: static scan of the bundle (no
eval/inline script/external URL) and OBSERVED in headless Chromium 1248 (`chrome-headless-shell`) that
the UI loads, exchanges the code, calls the API and renders the project data with no CSP violation.
Side effect: the Swagger UI at `/docs` and ReDoc load scripts from a CDN, which this CSP blocks; they
are not usable in a browser (the JSON at `/openapi.json` is, with a session).

## 7. Residual risks and limits (read these)

1. **Same-user malware is out of scope.** A process running as the same OS user can read the launcher's
   memory (the secret), the browser's `sessionStorage` or memory (the session), or attach to the sidecar.
   The capability only raises the bar against web pages, other users, and stale processes.
2. **Session token in `sessionStorage` instead of a cookie (owner decision SR-02, implemented).** Cookies
   are not port-scoped: a browser sends a `127.0.0.1` cookie to every server on that host, whatever the
   port, so a hostile local server on another port that the user browses to received the session cookie
   and could replay it (the previous design, reproduced by the reviewer in a real browser). The session is
   now a header token that script in the UI origin keeps in `sessionStorage` (origin-scoped including the
   port) and no cookie exists. Costs: (a) the token is readable by script running in the UI origin (the
   CSP forbids inline script and the bundle has no app-level HTML injection, but an XSS in the UI would
   read it; with a cookie `HttpOnly` would have hidden it from script); (b) a reload of the same tab keeps
   the session, a new tab, a bookmarked URL or a restored browser session needs a fresh launch code from
   the launcher, and a standalone user re-opens the stderr URL; (c) browser address-bar navigation to
   `/docs` or `/openapi.json` gets 401. Revert path: restore the `Set-Cookie`/cookie read in
   `local_capability.py` (the previous commit), accepting the cross-port cookie exposure.
3. **Plain HTTP on loopback.** The session token and the CSRF token travel over plain HTTP on the
   loopback interface; a local user with packet-capture privileges on loopback (root) could read them.
   Not mitigated.
4. **An authenticated session is fully powerful (KC-03 residual, accepted for R0-prime; reviewer finding
   SR-05).** The gate decides who may call the API; it does not restrict what an authorised call does.
   `POST /api/sources/confirm` walks any folder the sidecar user can read (`os.walk`, up to 100,000
   files per call, `~` expanded) for a caller holding a session. Observed behaviour in the frozen build
   (reviewer, with a session): it returns the folder name, whether the path is a folder, the count of
   discovered and eligible files (pdf, docx, md, text, csv) and a hash of the sorted (relative path, size)
   set; no file names and no contents (the indexer is a counting stub and `redevops-rag` is not
   included); `/etc` reported 235 files; `kind=database` answers "pending" and `kind=cloud` fails with
   500. So it is an existence and size-profile oracle and an I/O and CPU lever, not a content leak today;
   the risk grows if a real indexer or connector is enabled. Upstream's later KC-03 added an
   allow-listed-roots rule (`PROJECTS_SOURCE_ROOTS`); it is NOT part of R0-prime and no such mechanism
   exists here. A session is obtainable only by the paths in this document (launcher, launch code), not
   by a web page.
5. **The hosted-OAuth callback cannot work through the gate.** `GET /api/apps/connect/callback` is
   reached by a cross-site redirect from the provider and cannot carry the session header, and
   `Sec-Fetch-Site` is `cross-site`, so it answers 401/403. Not exercised in R0-prime (no hosted OAuth
   app is configured in the sample project).
6. **The agent gateway needs a session too.** An external agent client cannot use the gateway
   (`AGENT_GATEWAY_TOKEN`) on a gated sidecar without a session; the gateway bearer alone is not enough.
7. **Launch codes.** In the URL fragment the code is visible to the page that loads and to a browser
   extension before the script removes it (a few ms), and the used (dead) URL may remain in browser
   history or session-restore. It is single use and expires; the script removes it from the address bar
   immediately. The launcher's own hand-off of the code to the browser is the weak point on a shared
   machine: see the Another-OS-user row in section 2 and the redirect file in 4.3.
8. **Availability, not secrecy.** A local process cannot lock the user out of signing in (a correct code
   is served while the failure budget is spent, 4.7) and cannot block the launcher's mint (4.7), but it
   can flood `/handshake` to evict a live challenge, which the launcher handles by retrying (4.7). A
   client that stalls mid-request is cut off: a request body that stops for 10 s is aborted, `serve` waits
   at most 2 s for in-flight requests on shutdown (`timeout_graceful_shutdown`, so the launcher's 3 s grace
   is enough and the PyInstaller bootloader is not left to a SIGKILL), idle or half-sent connections are
   closed after 5 s (`timeout_keep_alive`), and the soft descriptor limit is raised towards 4096 at start so
   a few hundred stalled connections cannot exhaust a 256-descriptor process (SR-06). A determined local
   flooder can still consume CPU and descriptors; this is not a defence against a local denial of service.
9. **Ungated surfaces have NO Host check (SR-09).** The module-level `app` (`uvicorn
   agentic_os.projects_api:app`), a bare `create_app()` and `AGENTIC_OS_LOCAL_CAPABILITY=off` build the app
   without the gate, and therefore without the Host allowlist, the Origin check and the session
   requirement. They are for hosted deployments that put their own controls in front (their own
   authentication and Host/TLS handling) and MUST NOT be used on a desktop: on a desktop a DNS-rebinding
   page can read and write everything on them (reviewer: `Host: evil.example` answers 200, and a
   same-origin JSON POST reaches `/api/sources/confirm`). Wildcard CORS is gone there too, which stops the
   classic cross-site simple-request attack but not rebinding. The frozen sidecar's only server path is
   `serve()` (gated); the launcher strips the variable and `serve()` refuses `off` together with the
   hand-off.
10. **Standalone and Docker (SR-10).** The one-time URL, which contains the launch code, is printed on
    stderr, so it lands in terminals, supervisors and `docker logs`; it is dead after one use or 300 s.
    Standalone `serve` binds the fixed default port 8787, which a web page can probe (launcher mode uses an
    OS-chosen port). `Dockerfile.projects` (unchanged) runs `agentic-os-projects` on `0.0.0.0`: the gate is
    on, but `Host` must be `localhost` or `127.0.0.1` with the real in-container port, so a `-p 9000:8787`
    mapping or a host name needs `PROJECTS_ALLOWED_HOSTS`, the traffic is plain HTTP, and once the 300 s
    code is gone the only way to sign in is to restart the container. This is operationally breaking
    rather than a vulnerability.

## 8. Evidence levels

| Claim | Level |
|---|---|
| The anonymous cross-origin POST walks a folder on feeca065 | OBSERVED (audit; and in headless Chromium here) |
| The same page against the fixed sidecar is refused (preflight 403, POST 403) | OBSERVED (headless Chromium, real sockets) |
| Every route, docs, redoc, openapi, gateway needs the session; no CORS headers; Host/Origin/fetch-site/CSRF rules; single-use, expiry on two clocks, rate limit with correct-code bypass, concurrency; timing-safe primitives are used | OBSERVED by the test suite (`tests/test_local_capability.py`; real `serve` subprocesses for standalone and hand-off) |
| A hostile server on another 127.0.0.1 port receives nothing replayable (no cookie exists) | OBSERVED by test (simulated browser jar); a real-browser run is in the stage 3 report |
| A stalled request body does not block a graceful exit; 320 stalled connections do not break a 256-descriptor sidecar | OBSERVED by test on macOS arm64 (source run, not the frozen binary) |
| The UI works under the CSP (loads, exchanges, renders data) | OBSERVED in headless Chromium for the load path; the Sidekick POST path with the CSRF header is covered by the node-VM shim test and the TestClient flow, not by a real-browser click |
| macOS sleep behaviour of the two-clock expiry | STATIC plus fake-clock tests (no real sleep was performed) |
| The Windows socket option (`SO_EXCLUSIVEADDRUSE`) and Windows/WebView2/WKWebView behaviour of `sessionStorage` and the fragment | UNVERIFIED (not run) |
| The launcher opener (4.3): no credential in any argv; redirect file 0600 in a 0700 non-symlink, owner-checked directory, random 128-bit name, deleted after 60 s / swept after 120 s / on exit; `legacy_service_warning` | OBSERVED by `cargo test` on macOS arm64 (`opener.rs`: argv carries only the path, a `ps` poller finds no code, modes, symlink refusal, injection refusal, sweep, timers, exit hook; `launch.rs`: the legacy probe) and, in a headless browser, the redirect landing with the fragment intact (security stage 3 report). The real launcher GUI was not launched |
| Open items of the opener: Firefox, real desktop Safari / WKWebView, snap- or flatpak-confined browsers (they may not be able to read a `file:` page in the user's cache directory, or may rewrite the URL), Windows (file and directory ACL inheritance of `%LOCALAPPDATA%`, `cmd /C start` with a path, WebView2) | UNVERIFIED |
| Timing safety beyond the use of `hmac.compare_digest` over fixed-length digests | STATIC (no timing measurement) |

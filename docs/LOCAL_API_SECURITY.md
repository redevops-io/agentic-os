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
| Other local web app on another port of 127.0.0.1/localhost (same-site) | Browser sends cookies to it; it can make requests to us | Yes for requests (Origin, `Sec-Fetch-Site`, CSRF header); see cookie scope in section 7 |
| Another OS user on the same machine (shared host) | Connect to the loopback port | Yes (no credential, no secret on disk or argv); not against a user who can read the victim's browser cookies |
| A stale or foreign process holding the sidecar's port | Be mistaken for the sidecar by the launcher | Yes (HMAC handshake, section 5) |
| Malware running as the same OS user | Read launcher memory, the browser cookie jar, the process list, attach a debugger | NO. Out of scope. |
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
class (session endpoints, or session cookie and, for non-`GET`/`HEAD` methods, the CSRF header).

No CORS: no `Access-Control-*` header is ever emitted (any the app produced is stripped); `OPTIONS`
requests carrying `Access-Control-Request-Method` get 403. `create_app()` no longer defaults to a
wildcard; `allow_origins` is an explicit opt-in and is rejected together with the gate.

## 4. Wire contract (for the launcher)

### 4.1 Process start and the hand-off record

Environment: `AGENTIC_OS_HANDOFF=stdout` or `AGENTIC_OS_HANDOFF=fd:<n>` (n is an inherited, writable
file descriptor), `PROJECTS_PORT=0` (recommended: the sidecar binds first and reports the real port;
the default without it is 8787), optional `PROJECTS_BASE_PATH`.

The sidecar binds `127.0.0.1` only (a non-loopback `PROJECTS_HOST` is refused in this mode), starts
listening, then writes exactly one ASCII JSON line, terminated by `\n`, to the channel:

```json
{"v":1,"proto":"agentic-os-local-capability/1","event":"ready","host":"127.0.0.1","port":50123,"pid":4242,"base_path":"","secret":"<43 chars base64url>","launch_code":"<43 chars base64url>","launch_code_ttl":60}
```

* `secret`: 32 random bytes, base64url without padding. The launch secret. Held in memory by the sidecar
  and the launcher only. It never appears on a command line, in a URL, in a log, on disk, or in an
  HTTP message (the handshake proves possession with an HMAC).
* `launch_code`: 32 random bytes, base64url, single use, valid `launch_code_ttl` seconds (60) from the
  moment the line is written.
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

The launcher navigates the webview or browser to

```
http://127.0.0.1:<port><base_path>/#lc=<launch_code>
```

The code is in the URL fragment: the browser never sends it to the server, and it is not in access logs
or `Referer`. The page's script removes the fragment from the address bar immediately and exchanges it
(section 4.5). The webview must load this URL itself (same origin as the API); an app-bundled page on
another origin (for example a `tauri://` origin) cannot call the API, by design.

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
| `POST /api/local/session`, `Content-Type: application/json`, `{"code": "<launch code>"}` | 200 `{"v":1,"csrf_token":"...","csrf_header":"X-Agentic-CSRF","expires_in":86400}` and `Set-Cookie` |
| `GET /api/local/session` (cookie) | 200 `{"v":1,"csrf_token":"...","expires_in":N}` (so a reload recovers the CSRF token; the cross-origin same-origin policy keeps it from other pages) |
| `DELETE /api/local/session` (cookie + CSRF header) | 200, cookie cleared, session dropped |

Cookie: `agentic_os_session=<43 chars>; Path=<base_path or />; HttpOnly; SameSite=Strict`. No `Domain`
(host-only), no `Max-Age`/`Expires` (a session cookie; the server also expires it after 24 h), not
`Secure` (the origin is plain-HTTP loopback). The value is 256 random bits and is stored server-side as
a SHA-256 digest only.

Every state-changing request (any method other than `GET`/`HEAD`) must carry `X-Agentic-CSRF: <csrf_token>`.
The UI script adds it to all same-origin `/api/` fetches. At most 16 sessions and 8 outstanding launch
codes exist at a time (oldest evicted).

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
| 429 | `rate_limited` | 10 failed code or proof attempts in 60 s; `Retry-After` seconds. While limited even a valid code is refused. |

Failures counted for the limit are only those that pass the Host/Origin/fetch-site checks, so a web page
cannot lock the user out by spamming the endpoint cross-origin.

## 5. Standalone `serve` (no launcher)

Without `AGENTIC_OS_HANDOFF`, `serve` prints on stderr (ASCII only):

```
ReDevOps Projects - UI + API on http://127.0.0.1:<port>
Open this one-time URL in your browser (single use, expires in 300 s; the part after # never leaves your browser):
  http://127.0.0.1:<port>/#lc=<code>
```

The secret is not emitted in this mode. Opening `http://127.0.0.1:8787/` by hand without the code loads
the UI but every API call answers 401; the shim shows a banner telling the user to use the printed URL.
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
it runs before the deferred module bundle). The shim (`agentic_os/local_capability_shim.js`, about 70
lines, no eval, no inline script) exchanges `#lc=` and wraps `window.fetch` for same-origin `/api/`
requests (waits for the exchange, adds `X-Agentic-CSRF`, forces `credentials: same-origin`). Because the
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
   memory (the secret), the browser or webview cookie jar (the session), or attach to the sidecar. The
   capability only raises the bar against web pages, other users, and stale processes.
2. **Cookies are not port-scoped.** A cookie set for `127.0.0.1` is sent by the browser to every
   local server on that host, whatever the port. A hostile local server on another port that the user
   browses to would receive the session cookie and could replay it (it would also need to read the CSRF
   token, which a cookie holder can fetch with `GET /api/local/session`). The Strict/Host/Origin checks
   stop other web pages, not a local server that already holds the cookie. A bearer token held only in
   page memory would avoid this at the cost of losing the session on reload (owner decision, not taken).
3. **Plain HTTP on loopback.** The session cookie is not `Secure`. A local user with packet capture
   privileges on loopback (root) could read it. Not mitigated.
4. **An authenticated session is fully powerful.** The gate decides who may call the API; it does not
   restrict what an authorised call does. `/api/sources/confirm` still walks any folder the sidecar user
   can read for a caller holding a session (upstream's later KC-03 added an allow-listed-roots rule; not
   part of this change).
5. **The hosted-OAuth callback cannot work through the gate.** `GET /api/apps/connect/callback` is
   reached by a cross-site redirect from the provider; `SameSite=Strict` omits the cookie and
   `Sec-Fetch-Site` is `cross-site`, so it answers 401/403. Not exercised in R0-prime (no hosted OAuth
   app is configured in the sample project).
6. **The agent gateway needs a session too.** An external agent client cannot use the gateway
   (`AGENT_GATEWAY_TOKEN`) on a gated sidecar without a session; the gateway bearer alone is not enough.
7. **Launch codes in the URL fragment.** The code is in the fragment while the page loads: a user who
   copies the URL from the address bar before the script runs (a few ms), a browser extension, or
   session-restore history on some browsers could see it. It is single use and expires; the script
   removes it immediately. A launcher that keeps the code only in memory and opens a private webview
   keeps the exposure minimal.
8. **Availability, not secrecy:** a local process can exhaust the failure budget (10 per minute) and
   make the real exchange wait. The launcher can mint a fresh code after the window.
9. **The module-level `app` and a bare `create_app()` are not gated** (they remain the legacy open
   library/hosted-demo surface, now without CORS). Do not run them on a desktop; only `main()` / `serve`
   (what the sidecar executes) installs the gate.
10. **Docker self-host:** `Dockerfile.projects` runs `agentic-os-projects` with `PROJECTS_HOST=0.0.0.0`.
    It now prints the one-time URL in the container logs and requires `localhost`/`127.0.0.1` in `Host`
    (set `PROJECTS_ALLOWED_HOSTS` for other names). The Dockerfile is unchanged.

## 8. Evidence levels

| Claim | Level |
|---|---|
| The anonymous cross-origin POST walks a folder on feeca065 | OBSERVED (audit; and in headless Chromium here) |
| The same page against the fixed sidecar is refused (preflight 403, POST 403) | OBSERVED (headless Chromium, real sockets) |
| Every route, docs, redoc, openapi, gateway needs the session; no CORS headers; Host/Origin/fetch-site/CSRF rules; single-use, expiry, rate limit, concurrency; timing-safe primitives are used | OBSERVED by the test suite (`tests/test_local_capability.py`, 47 tests; real `serve` subprocesses for standalone and hand-off) |
| The UI works under the CSP (loads, exchanges, renders data) | OBSERVED in headless Chromium for the load path; the Sidekick POST path with the CSRF header is covered by the node-VM shim test and the TestClient flow, not by a real-browser click |
| The Windows socket option (`SO_EXCLUSIVEADDRUSE`) and Windows/WebView2/WKWebView behaviour of the `HttpOnly; SameSite=Strict` cookie and the fragment | UNVERIFIED (not run) |
| The Rust launcher implements 4.1 to 4.4 | NOT YET WRITTEN (contract only) |
| Timing safety beyond the use of `hmac.compare_digest` over fixed-length digests | STATIC (no timing measurement) |

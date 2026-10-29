# Release notes: permissions and authorization hardening

Written for: deployers and operators of agentic-os. These notes carry sections 28 to 33 of `docs/PERMISSIONS_MIGRATION.md` verbatim (a test keeps them equal); the migration guide has the rest
(before/after examples, the impact table for images and `docker run` examples, the execution invariant, the idempotency cache order, the threat model).

**Patched version:** 0.4.0. The app Dockerfiles and `apps/infra/requirements.txt` pin the kernel as `v0.4.0` (`KERNEL_REF`); that tag is created when 0.4.0 is released, and until it exists those builds cannot resolve the pin.

**Claim of this release:** The permissions plane and the app execution paths were reviewed in a multi-stage, AI-assisted security review with separate reviewer agents working from the code diff; this is not an external audit or third-party certification. Deployment configuration may still run unenforced when the setting is absent.

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

**The control plane seeds two example grants.** On first start of an empty store the control plane (`agentic_os/control_plane.py`) seeds, once, `seed-support-crm-customers` (app `support`, table `crm.customers`, read,
rows `region = us`, `email` masked) and `seed-finance-crm-customers` (role `finance`, same table, read, `notes` masked). They are examples for the grant-admin page and preview. They are `table` grants, so the apps'
tool gate (resource kind `tool`) never matches them and they widen no tool access; they do sit in the store the control plane administers. Remove them with `DELETE /api/permissions/grants/<id>`.

## 33. The control plane's fleet reads need the fleet credential

Five read routes of the control plane (`agentic_os/control_plane.py`) used to answer anyone who could reach the port, and a GET is not safe for that reason:

| Route | What it returns |
|---|---|
| `GET /approvals` | every pending approval with its payload: the request text an approver is asked to sign off |
| `GET /jobs/{job_id}` | the status and the result text or error of a background dispatch; the ids are sequential (`job-00001`) |
| `GET /api/fleet` | per module: the repository, ports, approval-required capability names, backend names, deploy kind and live health |
| `GET /status` | which modules are deployed (one health probe each) |
| `GET /notify/status` | which chat channels are wired and whether the inbound gateway runs |

They now take the same credential as the other fleet routes (`POST /up`, `/down`, `/dispatch`, `/approvals/{id}/{decision}`, `/openapi.json`): `Authorization: Bearer <AGENTIC_OS_CONTROL_TOKEN>` or
`X-API-Key: <AGENTIC_OS_API_KEY>`. A missing, empty, wrong or wrong-scheme credential answers 401; with neither variable set they answer 503 (never open). The permissions admin key
(`PERMISSIONS_ADMIN_KEY`) is a separate privilege and does not open them, and a credential in the query string is ignored (it would still appear in an access log). A refusal runs nothing (no job
lookup, no registry walk, no health probe) and returns no content. Names, repositories, pains, taglines and agents of the modules are still public through `GET /modules` and `/overview`.

**This release does not make the control plane as a whole protected. What is still open:**

* **The module proxy is anonymous, including POST.** `GET /m/<name>`, `GET /m/<name>/raw`, and `GET` and `POST /m/<name>/api/<path>` are reachable without any credential in every default deployment. They forward
  to the module's own service (its dashboard and its `/api/*`, which can include chat actions and data reads). The only restriction is `DEMO_READ_ONLY=1`, which no shipped configuration sets and which blocks
  only `POST`. This is pre-existing, is tracked as public issue #528, and **is not changed by this release.** Whether each app's own gate (`AGENTIC_OS_PERMISSIONS`) stops a request that arrives through
  the proxy has **not been verified.** **Operator mitigation:** do not expose the control plane's port to an untrusted network, and put an authenticating reverse proxy in front of it (or block `/m/` there).
* `GET /health` is public and includes a `security` block: the profile, whether authority and replay protection are durable, whether an egress proxy is configured, and the warnings. It is the readiness probe,
  so it was not changed.
* `GET /capabilities`, `GET /cockpit` and `GET /buildinfo` (mission routers) are public by design. `/capabilities` is the capability registry: names, operators, inputs and outputs, costs and approval flags.
* `GET /modules`, `/overview`, `/`, `/permissions` and `/assets` are public.

**Known limits of the dashboard credential.** The dashboard asks for the credential in a field (API key or control token, then Use). It is kept in that page's memory only, sent as a request header to the
dashboard's own origin, never stored or put in a URL or log, and dropped on Clear, on a refusal and when the page is closed or reloaded. But it is the full fleet key or control token (it can dispatch, start, stop and
approve), it is re-sent every 5 seconds while the tab is open, over whatever transport the deployment uses (the stock compose file serves plain HTTP on port 8080), and `AGENTIC_OS_API_KEY` has no minimum length and no
failed-attempt throttle. A read-only dashboard credential is not part of this release. `GET /` is served with `Cache-Control: no-store`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and a
Content-Security-Policy of `frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'; connect-src 'self'`. That stops framing, a `<base>` tag, a form posted elsewhere and plugins, and limits
`fetch`/XHR/WebSocket/EventSource/beacon connections to the page's own origin. It does not restrict images, links or navigation, and there is **no `script-src`**: it is not a defence against script injection (the
page builds its DOM with `textContent` and has no known injection sink). A nonce policy like the one on `/permissions` would need the page's inline styles and its one handler attribute reworked and a browser to
check it; that is left for a later change.

**Upgrade impact.**

* Anything that called these five routes without a credential now gets 401, or 503 on a deployment that sets neither `AGENTIC_OS_API_KEY` nor `AGENTIC_OS_CONTROL_TOKEN`. `deploy/docker-compose.yml` passes both
  variables through, empty by default, and the root `docker-compose.yml` now does too (this release adds the two lines; before, the container never saw them, so a key could not be set without editing the
  file). A stock compose deployment answers 503 on these routes until one of them is set, for example `AGENTIC_OS_API_KEY=$(openssl rand -hex 24) docker compose ... up`, and then
  `curl -H "X-API-Key: $AGENTIC_OS_API_KEY" http://localhost:8080/api/fleet`. The Helm runtime chart deploys the published runtime image, not this control plane, and is not affected.
* The dashboard shows no fleet and no approvals until the credential is entered, and says what it needs; with no credential configured on the server it says the views are disabled. Because a page cannot share the
  credential with another page without storing it, `/overview` and `/m/<name>` (which also poll `GET /api/fleet` for their live health dots) hold none: they ask once, and after the first 401 or 503 stop polling and show
  one line ("credential required" or "no fleet credential configured on the server"); their health dots stay grey. Behind a reverse proxy that adds the credential they keep working.
* The 503 body for these routes now reads "the fleet endpoints are disabled on this server" (it said "mutating endpoints").
* On the public demo, an anonymous visitor would see the prompt instead of the module cards, since the cards come from `GET /api/fleet`.

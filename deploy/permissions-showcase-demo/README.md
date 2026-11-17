# Permissions showcase (demo.redevops.io/permissions)

Two separate interfaces (see `docs/SECURITY_MODEL.md`, "Endpoints — the ADMIN API contract", and `docs/PERMISSIONS_MIGRATION.md` section 13).

**Public demo API (no credential, synthetic).** `GET /api/demo/scenarios`, `GET /api/demo/scenarios/{id}`: a fixed list of scenarios evaluated over a
built-in synthetic table. A visitor can only choose a scenario id (`[a-z0-9][a-z0-9-]{0,39}`, allowlisted: 400 malformed, 404 unknown). It never returns a
real grant, principal, row predicate, decision or store status, and keeps no per-visitor state. `GET /healthz` is liveness only.

**Build identity (no credential).** `GET /api/version` answers four fields — `version`, `revision`, `build_time`, `revision_source` — so a probe or an operator can
name the commit that is answering. `deploy.sh` stamps the revision at build time from the checkout it rsyncs and marks a dirty tree `-dirty`; an unstamped build
says `unknown` rather than naming a commit it may not match. Nothing else is exposed: no config, no paths, no counts (`tests/test_ops_surface.py` pins the field
set, because a version endpoint that grows becomes an enumeration surface).

**Admin API (credential required on EVERY route).** `/api/permissions/{status,grants,preview,authorize}`: `X-API-Key` or `Authorization: Bearer`, compared in
constant time with `PERMISSIONS_ADMIN_KEY` (at least 24 characters). No usable key configured (unset or shorter): 503 (disabled, never open). Missing, empty or wrong credential: 401. A credential in the query
string: 400. Failed attempts are throttled per client (10 wrong credentials in 5 minutes, then 429 with `Retry-After`); set `PERMISSIONS_TRUSTED_PROXIES` to your proxy address so the client is taken from `X-Forwarded-For`, otherwise every client behind the proxy shares one lockout. The 503 body names no variable (the fix is in the server log). The page's admin panel is inert until a key is typed in; the key is held in memory only.

**The same variable decides the scheme.** Unset, the container starts `--no-proxy-headers` (RT-03), so `X-Forwarded-Proto` is ignored and any URL this process builds describes the plaintext hop rather than the visitor's TLS connection; set, headers are honoured **only from that address** — a value that does not match the peer the proxy really
connects from disables both silently. Note also that this app answers a wrong trailing slash with a 404, never a redirect (`redirect_slashes=False`): an `http://` `Location`
on a path this unit does not route was emitted in front of it, and belongs to that front's configuration, not to this app.

**The public demo needs no key.** The page and `/api/demo/*` start and serve with no `PERMISSIONS_KEY` and no `PERMISSIONS_ADMIN_KEY`; the real grant store is built on the first authenticated admin request only. The page is an illustration with synthetic data, not a running enforcement point. `/docs`, `/redoc` and `/openapi.json` are off; a trailing slash is a 404; pages and the admin API carry a strict CSP (per-response nonce), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, and the admin JSON `Cache-Control: no-store`.

**Container.** Unprivileged (uid 10001), read-only root filesystem, no capabilities, `no-new-privileges`, pinned dependencies, bound to ONE address (`PERM_BIND_HOST`, default `127.0.0.1`; `deploy.sh` uses the host address cloudflared reaches). `seccomp=unconfined` / `apparmor=unconfined` stay only because this host's default profile blocks `socketpair()`; delete them where that limit does not exist. A volume created by the earlier root image needs one `chown -R 10001:10001` (see the Dockerfile).

**Configuration** lives on the host in `permissions.env` (0600; `PERMISSIONS_KEY` for the grant store, `PERMISSIONS_ADMIN_KEY` for the admin API) and is read by
`docker compose --env-file permissions.env`. `deploy.sh --init-key` generates the key on the host once; `deploy.sh` never creates, overwrites or deletes it.

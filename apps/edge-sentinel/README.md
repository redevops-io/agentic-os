> ### Reference application for [Context Runtime](https://github.com/redevops-io/context-runtime)
>
> A focused AI system for **security operations**: CrowdSec decisions and alerts, read and explained by an agent, with a human approval step before any block.
>
> ```
> Context Runtime  →  ReDevOps RAG  →  Sidekick  →  Application logic
> ```
> One of the [ReDevOps](https://github.com/redevops-io) reference applications built on Context Runtime.

---

# edge-sentinel — agent layer + SOC dashboard over a real CrowdSec core

A sibling of [`apps/billing`](../billing) (the reference vertical slice), same pattern,
new core. It wraps the running self-hosted **CrowdSec** instance (the open-source
detection & decision engine) with:

- an **agent layer** that reads REAL CrowdSec data — live decisions over the LAPI bouncer
  API + alerts via `cscli ... -o json`, and
- an **MD3 SOC dashboard** rendered from the data CrowdSec holds (no mock data on the CrowdSec panels; the network and backup panels show labelled SAMPLE DATA until their token is set),

for the organisation named in `SENTINEL_TENANT` (no name is shown when it is unset).

```
CrowdSec (OSS core, LAPI :8086) ──GET /v1/decisions (X-Api-Key)──┐
                                ──cscli alerts list -o json───────┤
                                                                  ▼
                                       app.py (FastAPI, :8203) ──▶ MD3 SOC dashboard
                                                                   + /api/activity
                                                                   + /agent/run
        ▲                                          agentic actions: block_ip (approval-gated)
        └── seed.py: cscli bouncers add + cscli decisions add (idempotent)   approve_block · triage
```

## Files

| File | Purpose |
|------|---------|
| `seed.py` | Bootstrap + seed via `cscli` (docker exec): adds the `meridian-agent` bouncer, captures its API key, injects 6 varied decisions/alerts, writes `.env`. Idempotent. |
| `app.py` | FastAPI service (port 8203): `/health`, `/api/activity`, `/` SOC dashboard, `/agent/run`. |
| `requirements.txt` | fastapi, uvicorn, httpx. |
| `Dockerfile` | slim-python image running `uvicorn app:app --port 8203`. |
| `.env` | Written by `seed.py`: `CROWDSEC_LAPI_URL`, `CROWDSEC_BOUNCER_KEY`, `CROWDSEC_CONTAINER`, `CROWDSEC_FRONT_URL`. |

## CrowdSec bootstrap method (the one that worked)

CrowdSec has **no rich web UI** and is **CLI-managed** via `cscli`. There are two read
paths, both driven from the running container `agentic-cores-crowdsec-1`:

1. **Decisions** — the bouncer/LAPI path. Create a bouncer credential and read live
   decisions over the Local API:
   ```bash
   KEY=$(sudo docker exec agentic-cores-crowdsec-1 cscli bouncers add meridian-agent -o raw)
   curl -s -H "X-Api-Key: $KEY" http://localhost:8086/v1/decisions
   ```
2. **Alerts** — LAPI alert auth needs a machine login, so the simplest reliable path is
   to shell out to `cscli` (which is already authenticated inside the container):
   ```bash
   sudo docker exec agentic-cores-crowdsec-1 cscli alerts list -o json
   ```

`seed.py` automates step 1 (and re-creates the bouncer if it already exists so the key is
always usable), injects the seed activity, and writes `.env`.

Key facts for CrowdSec **v1.7.8** (discovered on this host):

- The container is **`agentic-cores-crowdsec-1`**; LAPI is on **http://localhost:8086**.
- `cscli decisions add` **also creates a matching alert**, so seeding decisions populates
  both `/v1/decisions` and `cscli alerts list`.
- LAPI has no public `/health`; a TCP+HTTP response (even `404` on `/`) means the service
  is up — `crowdsec_connected()` treats any `< 500` as connected.
- A decision's `duration` counts down (e.g. `3h59m59s`); `scope` is `Ip` or `Range`.

## Seed + run

```bash
cd apps/edge-sentinel

# 1. Bootstrap + seed CrowdSec (idempotent — writes .env with the live bouncer key)
python3 seed.py
#   → BOUNCER_KEY=<key>
#   → SEED_OK bouncer=meridian-agent added=6 decisions=6 alerts=6
#   → Wrote .env

# 2. Install and run (P1: the app is a package — the console script is the quickstart)
python3 -m pip install .                 # from this directory; add --break-system-packages on PEP-668 hosts
edge-sentinel --help                     # runs from any cwd, including this one
edge-sentinel serve --port 8203
#   `serve` binds 127.0.0.1 by default and needs the agentic-os kernel installed (documented refusal
#   otherwise — the kernel seam is audit item P2, counsel-gated). It never binds a reachable
#   interface unless asked: see security.py / SENTINEL_ALLOW_EXPOSURE below.
#   Do NOT run `python3 -m uvicorn app:app` from this directory: `-m` puts it ahead of the standard
#   library, where operator.py shadows the stdlib `operator` and breaks Python before the app starts.
#   app.py auto-loads .env, so CROWDSEC_BOUNCER_KEY is picked up with no manual copy.

# Or with Docker (point the LAPI URL at the CrowdSec service; the socket is NOT mounted, see "Docker access" below):
docker build -t edge-sentinel .
docker run -e AGENTIC_OS_PERMISSIONS=off --rm -p 8203:8203 \
  -e SENTINEL_ALLOW_EXPOSURE=1 -e SENTINEL_BASIC_AUTH_USER=<operator> -e SENTINEL_BASIC_AUTH_PASS=<secret> \
  -e CROWDSEC_LAPI_URL=http://host.docker.internal:8086 \
  -e CROWDSEC_BOUNCER_KEY=<key from .env> \
  edge-sentinel
#   A `-p` publish is a deployment decision made twice over (P9): the image declares its bind, but it
#   refuses to come up without SENTINEL_ALLOW_EXPOSURE=1 plus an operator credential — or the named
#   read-only-demo opt-out SENTINEL_ALLOW_UNAUTHENTICATED_EXPOSURE=1 (execution stays authenticated, dry-run).
```

## Environment variables

| Var | Default | Meaning |
|-----|---------|---------|
| `CROWDSEC_LAPI_URL` | `http://localhost:8086` | CrowdSec Local API base (`/v1/decisions`). |
| `CROWDSEC_BOUNCER_KEY` | _(from .env)_ | `X-Api-Key` for reading decisions — the key from `cscli bouncers add`. |
| `CROWDSEC_CONTAINER` | `agentic-cores-crowdsec-1` | Container name for `cscli` (alerts, ban enforcement). |
| `CROWDSEC_FRONT_URL` | `http://localhost:8086` | Link for the "Open CrowdSec metrics ↗" button (CrowdSec is CLI-managed; this is the LAPI endpoint). |
| `PORT` | `8203` | uvicorn bind port. |
| `SENTINEL_BIND_HOST` | `127.0.0.1` | Bind interface (P9). A reachable bind needs `SENTINEL_ALLOW_EXPOSURE=1` **and** an operator credential (`SENTINEL_BASIC_AUTH_USER`/`_PASS`, or the shared `PARTNERS_BASIC_AUTH_*` pair) or the named `SENTINEL_ALLOW_UNAUTHENTICATED_EXPOSURE=1`; the startup guard refuses otherwise. |
| `WAZUH_TLS_VERIFY` | on | Certificate verification for the Wazuh API (P9). Set to a CA bundle path for a private CA; `off` skips verification and is labelled as such wherever an operator reads it. |
| `EDGE_SENTINEL_DEMO` | _(unset)_ | Opt-in fixture core (P4). Reads come from synthetic RFC 5737 data and no external side effect is performed: CrowdSec bans/unbans and `cscli` calls are refused at `core`, and NetBird, Portabase, deploy-scan, Wazuh, ClamAV and the model are held back too — in demo mode the app reads no external service and changes none, so a demo cannot create a live NetBird policy, launch a scan or walk a host filesystem. `/health` reports the scope as `demo.scopes`. It grants no exposure and no exemption from `SENTINEL_BIND_HOST` / `SENTINEL_ALLOW_EXPOSURE` / credential rules: a demo container that would otherwise bind reachable still refuses to start. The dashboard carries a visible **DEMO / SYNTHETIC DATA** banner. |
| `EDGE_SENTINEL_DEMO_SCENARIO` | `mixed` | Which fixture story to serve: `mixed` (benign, repeated, ambiguous and approval-required stories together) or `unavailable`, where the LAPI reads fail so every KPI reports `not read` instead of zero. Anything else falls back to `mixed`. |
| `REDEVOPS_LLM_BASE_URL` | _(unset)_ | Optional one-sentence narration on `triage`, inherited from the kernel. Unset — or a host the kernel will not treat as in-boundary — means **no model call at all**; the deterministic summary stands. Model id `REDEVOPS_LLM_MODEL`, bearer `OPENAI_API_KEY`. Nothing here reads an Anthropic key, and no model vendor is named by this app — see [Alert triage and the model boundary](#alert-triage-and-the-model-boundary). |

| `SENTINEL_PROTECTED_IPS`, `SENTINEL_HOST_IPS` | _(empty)_ | Comma-separated addresses/CIDRs that are never banned (in addition to this host, its gateway, the LAPI host and the requesting operator). |
| `SENTINEL_BLOCK_ALLOW_NETWORKS` | _(empty)_ | CIDRs inside which private/reserved space may be banned. Loopback, link-local, multicast and unspecified space is never bannable. |
| `SENTINEL_BLOCK_ALLOW_WIDE_CIDR` | _(off)_ | Allow ranges wider than /24 (IPv4) or /64 (IPv6). |
| `SENTINEL_BLOCK_MAX_HOURS` | `168` | Cap for a ban duration. |
| `SENTINEL_STATE_DIR` | _(unset)_ | Directory for the durable approval state (atomic, private file). Unset keeps state in memory only; an unreadable file makes decisions and executions fail closed. |
| `CROWDSEC_MACHINE_ID`, `CROWDSEC_MACHINE_PASSWORD` | _(unset)_ | Optional CrowdSec watcher (machine) login. When both are set, alerts are read from the LAPI (`GET /v1/alerts`) and bans are written to it (`POST /v1/alerts` carrying the decision, `DELETE /v1/decisions` to lift) with a cached JWT, so no docker access is needed and a real LAPI accepts the write. Unset keeps the `cscli` alert path and the bouncer-key write path. |
| `SENTINEL_REQUIRE_AUTH` | _(unset)_ | Standalone profile. When truthy, every route except `/health` and `/readyz` needs the operator credential (HTTP Basic, 401 otherwise) and the app refuses to start without one. `/invoke` is not covered by this switch: the operator router authenticates it with `AGENTIC_OS_CONTROL_TOKEN` (503 when unset). Unset keeps the reference behaviour (reads open, execution authenticated). |
| `SENTINEL_UI` | _(unset)_ | When truthy, `GET /` redirects to the browser console at `/ui/` (framework-free static files, a strict Content-Security-Policy, no third-party asset). The standalone image sets it. Unset keeps the legacy dashboard unchanged. |
| `SENTINEL_TENANT` | _(unset)_ | The organisation name shown in the dashboard header, title and footer (80 characters at most). Unset shows no name: there is no default company. |
| `SENTINEL_REQUIRE_TWO_PERSON` | _(unset)_ | When truthy, the person who approved an action may not be the one who executes it (`/api/v1`; the reference routes keep their single-operator behaviour). |
### Alert triage and the model boundary

`triage` is deterministic: it counts active decisions and alerts, breaks them down by severity and names
the top scenarios. That output is complete by itself, and every executable action (`block_ip`,
`approve_block`) is a CrowdSec/`cscli` call that never passes through a model.

The optional one-sentence narration follows one path, and only one:

```
triage → core.triage(blurb=_llm_blurb) → agentic_os.app.llm.governed_text(
             classifications=(CUSTOMER_CONFIDENTIAL,))
         → the single endpoint derived from REDEVOPS_LLM_BASE_URL
```

- **No vendor key, no vendor name.** The app reads no `ANTHROPIC_API_KEY` and contains no provider
  endpoint of its own; an earlier helper that posted to a vendor API directly was replaced by the
  governed route, and `apps/edge-sentinel/test_llm_egress.py` fails if a direct model `POST`
  reappears. `OPENAI_API_KEY` is the bearer token sent to *your* endpoint because the wire format is
  OpenAI-compatible — it is not a subscription to that vendor.
- **One candidate, not a pool.** `governed_text` registers exactly one endpoint, labelled
  `self-hosted` / `IN_BOUNDARY`, so there is no external endpoint for this call to fall back to.
  `REDEVOPS_PRIVACY_MODE` still governs the rest of the kernel, but it cannot widen this call.
- **Fail-closed, in every direction.** Unset `REDEVOPS_LLM_BASE_URL`, a host that is not private,
  loopback or link-local (and not in `REDEVOPS_LLM_IN_BOUNDARY_HOSTS`), a refused route, a timeout or
  a transport error all return no narration; the summary stands and nothing is retried elsewhere. The
  kernel refuses to *label* a public host as in-boundary rather than issue a receipt that would claim it.
- **What actually goes out, when you enable it:** a one-sentence instruction plus up to six active
  decision values with their scenario and severity — real source addresses from your CrowdSec store,
  classified `CUSTOMER_CONFIDENTIAL`. That is why the routing label is verified rather than trusted.
- **Model identity.** `REDEVOPS_LLM_MODEL` (default `DeepSeek-V4-Flash`) is the identifier your
  endpoint is asked for. It names the request; it does not establish which model answered. This
  README makes no claim about any specific model or vendor.
- **What this does *not* promise.** Enabling narration sends data out of this process, over the network,
  to the endpoint you configured. Nothing here asserts that it stays on one machine, one datacentre or
  one jurisdiction — the boundary is *your* network boundary, and how that endpoint terminates TLS,
  authenticates, logs or retains what it receives is yours, not something this repository can verify.
  If you cannot route alert data there, leave `REDEVOPS_LLM_BASE_URL` unset: the app loses a sentence of
  prose and nothing else.

`REDEVOPS_LLM_SETTINGS_FILE` can hold the endpoint, model and a non-secret key reference instead of the
environment. The narration is read-only: it cannot block, unban or reach the Docker socket, and P9's
exposure and credential rules apply to it exactly as to every other route.



### Approval workflow

Anonymous visitors can preview a decision (`POST /api/cases/{id}/respond`): the result is simulated and nothing is recorded. A real decision needs the operator credentials, and the recorded actor is the authenticated principal. A request must belong to the case, an action can be decided only while awaiting approval (an approved action can still be withdrawn), and `POST /api/cases/{id}/execute` runs only an approved action, once. A failed execution can be retried with backoff up to five times; after that an operator re-approval resets the budget.

### Deployment note

With a machine (watcher) login configured (`CROWDSEC_MACHINE_ID` and `CROWDSEC_MACHINE_PASSWORD`) the sentinel reads alerts from the CrowdSec LAPI and writes bans to it over the LAPI, with no docker access; without one it shells out to `cscli` for alerts and writes bans with the bouncer key as before. See `/health` for the state-store status and `/readyz` for readiness.

## Endpoints

- `GET /health` → `{"status":"ok","core":"crowdsec","connected": <bool>}`
- `GET /readyz` → `{"ready": <bool>, "checks": {...}}`, 200 when the instance can serve and record decisions, 503 otherwise (unusable `SENTINEL_STATE_DIR`; in the production profile no `AGENTIC_OS_AUTHORITY_DB`).
- `GET /api/activity` → KPIs read from CrowdSec (active decisions, alerts 24h, unique source IPs, last
  event, plus the time of the read) + active decisions + alert feed + top scenarios, all from CrowdSec. Cached 10s.
- `GET /` → the MD3 SOC dashboard from the live data: a **status banner** first (a neutral "CrowdSec: N active decisions and M alerts, read at <time>" statement, or red when CrowdSec holds active decisions or is unreachable; it never claims all is well), **KPI tiles**, an
  **alert feed grouped by severity** with color pills, a **scenario bar meter**, and an
  **attack-sources / active-decisions table**. Header shows the `SENTINEL_TENANT` name when set, a
  "CrowdSec LAPI reachable" pill, a "read from the CrowdSec LAPI at <time>" badge, and an "Open CrowdSec metrics ↗" link.
- `POST /agent/run` with `{"action": ...}`:
  - `"block_ip"` `{ip}` → the **approval-gated** sensitive action. Returns
    `{"status":"pending_approval","summary":"block <ip>"}` — never auto-bans.
  - `"approve_block"` `{ip, duration?, reason?}` → the approved path: actually runs
    `cscli decisions add` to enforce the ban, then refreshes the dashboard cache. Without
    `SENTINEL_BLOCK_ENABLED` this route returns a **dry run**: the response says `Dry run: nothing was blocked`.
    Executing a real block from this route needs an operator credential and that enable flag.
  - `"triage"` → summarizes active decisions + alerts (severity mix, top scenarios); the summary is
    deterministic and complete on its own, and a one-sentence narration is appended only when a
    governed in-boundary model is configured (see below) — never an extra call to a vendor API.

## Docker access (least privilege)

Edge Sentinel does not need unrestricted Docker access, and the example above does not mount the Docker socket.

- **Decisions** are read over the CrowdSec LAPI with a bouncer key: no Docker access.
- **Alerts**: in this tree the alert feed is read with `cscli alerts list` through `docker exec` into the CrowdSec container (`core.fetch_alerts`). Without Docker access that read fails and the dashboard says "ALERT FEED UNAVAILABLE"; it is never shown as a quiet reading. Reading alerts from the LAPI with a watcher (machine) login needs no Docker access: set `CROWDSEC_MACHINE_ID` and `CROWDSEC_MACHINE_PASSWORD`.
- **Image and deployment scanning** from the chat is not available in the demo and is planned.
- If an operator turns on a container feature: never mount the raw socket into the app. Either run the scanners in a separate, isolated worker that holds the access and returns results, or put a restricted socket proxy in front of the daemon. The proxy is **operator-supplied**: Edge Sentinel does not ship or support one, and this describes a pattern. It must allow-list by method and path; for the features above that is `GET /_ping`; for the `cscli` alert read `GET /containers/<crowdsec container>/json`, `POST /containers/<crowdsec container>/exec`, `POST /exec/<id>/start`, `GET /exec/<id>/json` and `POST /exec/<id>/resize`; for listing containers `GET /containers/json` and `GET /containers/<name>/json`; for images `GET /images/<ref>/json` and, for scanners that read an image through the daemon, `GET /images/<ref>/get` (image export). Know the limits: a path allow-list does not inspect request bodies, so the exec endpoints let the caller run any command in that container (this is not read-only), `GET /containers/<name>/json` returns the container's environment, which may hold secrets, and `GET /images/<ref>/get` exports every layer of an image, which may include baked-in secrets.

## Authorization note

Note: basic permissions authorise the app INSTANCE, not callers: an instance with write grants lets every caller who can reach it write. See `docs/PERMISSIONS_MIGRATION.md` section 29.

## Standalone deployment

A reproducible multi-architecture image (`Dockerfile.standalone`), Docker Compose and Kubernetes/k3s manifests (`deploy/standalone/edge-sentinel`) and the install, authentication, backup and upgrade guide ([docs/standalone/edge-sentinel.md](../../docs/standalone/edge-sentinel.md)) run Edge Sentinel on its own, with sign-in required on every route. The reference `Dockerfile` in this directory is unchanged.

## The product API and local users (`/api/v1`)

`/api/v1/*` is the one API the browser UI, Projects/Sidekick and scripts use: events, decisions, cases with their evidence and replay proof, the action queue, attributable approvals bound to the action digest, execution receipts with the persisted read-back, a change feed, a history, and local users. Every route needs a credential in every profile. Users, roles (`viewer` < `operator` < `admin`) and sessions live in the same SQLite file as the history (`SENTINEL_STATE_DIR/sentinel.db`); the operator credential the deployment ships with becomes the first admin; `edge-sentinel users add|list|set-password|disable|enable` manages users from the command line. See [docs/standalone/edge-sentinel.md](../../docs/standalone/edge-sentinel.md).

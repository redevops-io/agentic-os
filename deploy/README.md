# Agentic Business OS — self-contained demo

This brings up the **control plane** plus a stand-in service for nine of the
`deploy: compose` modules, so those cards light up in the dashboard end-to-end —
without depending on any individual module repo's own Dockerfile.

> This is the **legacy compose fleet dashboard** (`:8080`). The current end-user UI is the
> **Projects + Sidekick** app — `pip install 'agentic-os[projects]'` then `agentic-os-projects`
> (served at `demo.redevops.io/projects`). See the repo [README](../README.md).

## Run

From the repository root:

```bash
docker compose -f deploy/docker-compose.yml up --build
```

Then open the dashboard:

- **Dashboard (primary UI):** http://localhost:8080/
- Fleet JSON API: http://localhost:8080/api/fleet (needs the fleet credential, see Auth; the dashboard has a field for it)
- Control-plane health: http://localhost:8080/health

The dashboard polls `/api/fleet` every 5 seconds. Each module card shows its pain
label, tagline, agent chips, any approval-gated actions, and a live status dot
(green = up, red = down, grey = n/a). The summary line reads `N/15 modules up`
(the fleet has 15 agent modules; this self-contained demo stands up nine of them, so it reads `9/15`).

## What's running

| Service              | Internal port | Role                                  |
| -------------------- | ------------- | ------------------------------------- |
| `control-plane`      | 8080          | FastAPI dashboard + fleet aggregation |
| `edge-sentinel`      | 8101          | module stand-in                       |
| `agentic-support`    | 8102          | module stand-in                       |
| `agentic-billing`    | 8103          | module stand-in                       |
| `agentic-books`      | 8104          | module stand-in                       |
| `agentic-compliance` | 8105          | module stand-in                       |
| `control-tower`      | 8106          | module stand-in                       |
| `market-radar`       | 8107          | module stand-in                       |
| `growth-engine`      | 8108          | module stand-in                       |
| `social-autopilot`   | 8109          | module stand-in                       |
| `traefik` (optional) | 80            | routes `/` and `/m/{module}`          |

The control plane resolves each module by its compose **service name**
(`http://edge-sentinel:8101/health`, etc.), which matches the module name in
`modules.yaml`.

### Optional Traefik routing

A Traefik v3 service is included as polish. With it up:

- http://localhost/ → control-plane dashboard
- http://localhost/m/edge-sentinel → the edge-sentinel stand-in (and likewise for
  each module)

The control-plane dashboard on **port 8080 is the primary UI**; Traefik is optional.

## Auth

Mutating endpoints (`/up`, `/down`, `/approvals/{id}/{decision}`) and the reads of non-public state (`GET /approvals`, `/jobs/{id}`, `/api/fleet`, `/status`, `/notify/status`) honor
`AGENTIC_OS_API_KEY` (or a bearer `AGENTIC_OS_CONTROL_TOKEN`). Generate a value, set it, and send it as an `X-API-Key` header:

```bash
export AGENTIC_OS_API_KEY="$(openssl rand -hex 24)"
docker compose -f deploy/docker-compose.yml up --build
curl -H "X-API-Key: $AGENTIC_OS_API_KEY" http://localhost:8080/api/fleet
```

The fleet key has no minimum length and no failed-attempt throttle, so use a long random value and keep the port off untrusted networks. The module proxy (`/m/<name>`) is not covered by this
credential and is open (public issue #528): put an authenticating proxy in front of the control plane if it is reachable by anyone you do not trust. If neither `AGENTIC_OS_API_KEY` nor `AGENTIC_OS_CONTROL_TOKEN` is set, these endpoints answer 503: they are never open. A missing or wrong credential answers 401.

## Note on the stand-in

`deploy/module_service.py` is a **demo stand-in**: a single-file FastAPI app that
serves the `GET /health` contract the control plane probes, plus `/info` and a
minimal `/` page. Each real redevops.io module repo replaces this stand-in with its
own full stack — the contract (a `/health` returning 2xx on the module's port) stays
the same, so the fleet view keeps working unchanged.

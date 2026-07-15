# Agentic Business OS — self-contained demo

This brings up the **control plane** plus a stand-in service for each of the nine
`deploy: compose` modules, so the dashboard's fleet lights up green end-to-end —
without depending on any individual module repo's own Dockerfile.

## Run

From the repository root:

```bash
docker compose -f deploy/docker-compose.yml up --build
```

Then open the dashboard:

- **Dashboard (primary UI):** http://localhost:8080/
- Fleet JSON API: http://localhost:8080/api/fleet
- Control-plane health: http://localhost:8080/health

The dashboard polls `/api/fleet` every 5 seconds. Each module card shows its pain
label, tagline, agent chips, any approval-gated actions, and a live status dot
(green = up, red = down, grey = n/a). The summary line reads `N/10 modules up`
(sidekick is a `deploy: tool`, so 9 services back the compose fleet).

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

Mutating endpoints (`/up`, `/down`, `/approvals/{id}/{decision}`) honor
`AGENTIC_OS_API_KEY`. Set it to require an `X-API-Key` header:

```bash
AGENTIC_OS_API_KEY=secret docker compose -f deploy/docker-compose.yml up --build
```

If unset, auth is disabled (fine for a local demo).

## Note on the stand-in

`deploy/module_service.py` is a **demo stand-in**: a single-file FastAPI app that
serves the `GET /health` contract the control plane probes, plus `/info` and a
minimal `/` page. Each real redevops.io module repo replaces this stand-in with its
own full stack — the contract (a `/health` returning 2xx on the module's port) stays
the same, so the fleet view keeps working unchanged.

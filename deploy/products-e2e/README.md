# Products end-to-end rig

Real processes, no mocks: a Projects container built from this checkout, a standalone Edge Sentinel stack (Compose with
real CrowdSec, from `deploy/standalone/edge-sentinel`) and Playwright driving the shipped Projects UI. Used for the
Projects + Sidekick verification described in `docs/products/README.md`; not part of the required CI.

1. Bring up Edge Sentinel (unique project name and port) with `sentinel-override/docker-compose.override.yml` added to its
   `COMPOSE_FILE`, then `bootstrap.sh`. The override passes the service token and the shared approval secret.
2. Create a named Sentinel user and link the Projects principal to it (`POST /api/v1/actor-links`, for example
   `projects:operator`).
3. Copy `projects/products.yaml.example`, put the three secret files next to it (service token, approval secret, operator
   Basic credential `user:password`), `docker build -f Dockerfile.projects -t projects:products .` and `docker compose up -d`
   in `projects/` (the compose file joins the Sentinel network by name).
4. `python3 ui_flow.py <projects url> <sentinel url> <outdir> --ip <ip of a seeded alert> --basic user:pass --sentinel-basic user:pass [--live]`
   drives Sidekick, Attention, Today approve, execute and Missions, then compares the product's own records.
   `ui_negative.py` covers the Sidekick refusal, an unlinked principal and a stopped product.
5. `sidecar_e2e.py` and `desktop_inspect.sh` do the same against a locally built, unsigned desktop sidecar.

Seed an alert with `cscli decisions add --ip <ip> --duration 1h ...` followed by `cscli decisions delete --ip <ip>`; the
alert stays and becomes a case with a proposal. TEST-NET addresses are refused by the ban guard unless
`SENTINEL_BLOCK_ALLOW_NETWORKS` allows them.

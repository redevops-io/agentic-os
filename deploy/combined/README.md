# Combined install: Projects + redevops-qa + Edge Sentinel

One Docker host, three products, wired through the **supported** activation: Projects starts with
`AGENTIC_OS_PRODUCTS` pointing at a registry file, and registers exactly what that file lists — no code
flags, no per-product forks. The Sentinel + CrowdSec services are the published standalone stack
(`deploy/standalone/edge-sentinel`, brought up by its own documented bootstrap); this directory adds only
the two consumers and the identity wiring between them.

```
┌ Projects ────────────┐        ┌ Edge Sentinel ─┐   ┌ CrowdSec ┐
│  UI · Sidekick ·      │  GETs  │  cases · bans   │──▶  LAPI    │
│  approval cards       │───────▶│  approvals      │   └──────────┘
└─────────┬─────────────┘        └─────────────────┘
          │  runs · evidence · approval cards (bearer)
          ▼
┌ redevops-qa ──────────┐
│  plans · bundles      │
└───────────────────────┘
```

## Install

Prerequisites: Docker with Compose v2 (Docker Desktop on macOS works, incl. Apple Silicon — see
Limitations), `curl`, `python3` on the host, and ~4 GB free RAM. From a checkout of this repository:

```sh
cd deploy/combined
./bootstrap.sh      # idempotent; generates every secret, builds Projects, brings all four services up,
                    # links the Projects operator to a named Sentinel user, and ends with a health pass
./smoke.sh          # checks every claim below against the running stack (one demo QA run; read-only otherwise)
```

To run two combined installs side by side, give each its own `SENTINEL_PROJECT`/`SENTINEL_NETWORK`/ports in
`.env`.

## What you get

| Surface | Where | Sign-in |
|---|---|---|
| Projects UI (approval cards: Today, Missions) | `http://127.0.0.1:8787/` | `operator` / password in `secrets/operator_basic` after the colon |
| Sidekick chat (product tools listed for the session) | Projects UI → chat | needs a model endpoint: `REDEVOPS_LLM_BASE_URL` / `REDEVOPS_LLM_MODEL` in the Projects environment |
| Edge Sentinel UI | `http://127.0.0.1:8203/` | `operator` / `../standalone/edge-sentinel/secrets/operator_password` |
| redevops-qa UI | `http://127.0.0.1:8080/ui/` | `admin` / password in `secrets/qa_admin_password` (more users: `docker compose exec redevops-qa redevops-qa-service users add`) |
| Product links inside Projects | each product's `ui_url` | deep links are built by the adapters from the registry origin |

Health of the wiring: `curl -u operator:… http://127.0.0.1:8787/api/products` lists both products with
`ready`, `capabilities` and `approval_secret` — that endpoint is what `bootstrap.sh` waits on and what
`smoke.sh` asserts.

## Restart · teardown

```sh
docker compose restart projects        # everything else is stateful and stays up
docker compose down                    # stop; data stays (qa-data volume, sentinel volumes)
./smoke.sh --teardown                  # stop BOTH stacks and remove their volumes
rm -rf secrets .env products.yaml      # forget the install (secrets never leave this directory)
```

## How the products are bound (nothing here is magic)

- **`products.yaml`** (copied from `products.yaml.example`) is the only activation input; bootstrap validates
  it inside the projects image before starting anything (`agentic_os.products.registry.load_registry`).
- **Shared approval secret**: the same value sits in Projects, QA and Sentinel (bootstrap copies, never
  re-rolls it), because a per-click approval token is only as good as the signature all three verify with.
  A mismatch is not fatal — approvals simply stay staged (fail-closed).
- **Actor identity**: the browser operator (`projects:<user>`) is linked to a **named** Sentinel user, so a
  ban approved "in Projects" is auditable as a person in Sentinel's own records. Approvals remain gated:
  bans are dry-run until `SENTINEL_BLOCK_ENABLED=1` is set upstream in the standalone stack.
- **Service credentials** are files under `./secrets/` (0600), mounted read-only; QA's service token lives in
  `.env` per its published compose contract.

## Limitations (stated, not smoothed over)

- **macOS / Docker Desktop (incl. Apple Silicon)**: QA publishes **linux/amd64 lanes only** — on arm64 hosts
  every lane is refused with `ARCHITECTURE_UNSUPPORTED`, even under emulation, so this compose pins
  `platform: linux/amd64`. Lanes therefore run under Rosetta/QEMU: slower, same evidence format, and **not**
  native-ARM64 evidence — **no native ARM64 QA browser lane exists**; do not present an emulated pass as one.
  The **approval round trip QA → Sentinel** is exercised on Linux installs only: the callback routing that
  scope needs is not satisfied by Docker Desktop's bridge networking on macOS. Everything `smoke.sh` checks
  was verified working on macOS Docker Desktop (Apple Silicon) and gates on Linux CI.
- The QA capabilities reachable here are exactly what the published service advertises; where it answers
  "unavailable" (e.g. the ui-audit profile on 0.6.3), Projects shows it as unavailable — by design.
- This is a **loopback demo-class** topology like the standalone pages: publish only to networks you control.

## Release gate

`.github/workflows/combined-install-e2e.yml` runs this exact `bootstrap.sh` + `smoke.sh` path on a clean
runner (Linux) for every change under `deploy/` or `Dockerfile.projects`, nightly, and on demand. A failure
there means the combined claim is false and the claim, not the test, gets withdrawn.
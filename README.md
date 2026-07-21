<div align="center">

# agentic-os

**The control plane for the redevops.io Agentic Business OS.**

*Run your whole business as a fleet of agents — on hardware you own, with the cheapest model that's good enough for each task.*

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)
&nbsp;·&nbsp; self-hosted &nbsp;·&nbsp; no lock-in &nbsp;·&nbsp; open-core

</div>

---

## What this is

The reference [apps](apps/) each solve one business pain — [payments](apps/billing),
[support](apps/support), [books](apps/books), [compliance](apps/compliance),
[analytics](apps/control-tower), [marketing](apps/growth-engine), [social](apps/social-autopilot),
[competitive intel](apps/market-radar), [security](apps/edge-sentinel) — and more under [`apps/`](apps/).

**`agentic-os` is the layer that runs them all as one coordinated fleet.** It is the *operating system* of the
business: a registry of modules, an orchestrator that gives each one its own agents, a cost-aware LLM router, a
shared business context, and a single control plane with human-in-the-loop approval for anything that moves money,
touches compliance, or changes infrastructure.

> **`control-tower` is the dashboard you look at. `agentic-os` is the kernel that runs underneath it.**

It's also how redevops.io runs *itself* — headcount-light, agent-heavy — which is the proof that it scales.

## Why it exists

A small company today glues together five disconnected AI SaaS tools, each a silo with its own data and its own
premium API bill. `agentic-os` replaces that with **one fleet, your data, your hardware**:

- **One control plane** — deploy, start, stop, and observe every module from one place.
- **Cost-aware routing** — every agent task is sent to the cheapest model that can do it well: a local
  `llama.cpp`/`ollama` model on hardware you own for the bulk of the work, a premium API only for the
  hard 5%. That per-task routing *is* the product.
- **Cross-module workflows** — a new signup isn't five disconnected events; it's one workflow where billing sets up
  the subscription, support sends onboarding, books records the entry, and compliance files the consent record.
- **Human-in-the-loop where it matters** — money, compliance, and infra actions pause for one-click approval.
- **Fine-grained access control** — a permissions plane grants each app/role/user read/write on a database, table
  or corpus, sliced by **row scope** and **column mask**; set it up and preview it live at `/permissions` (grants
  AES-GCM–encrypted at rest, gated write API). See [docs/permissions.md](docs/permissions.md).

## Architecture

```
                                ┌──────────────────────────────────────┐
                                │            control plane             │
   you / your team  ───────────│   FastAPI + CLI: deploy · start ·     │
   (approvals, status)         │   stop · health · approvals · audit  │
                                └───────────────┬──────────────────────┘
                                                │
        ┌───────────────────────┬───────────────┼───────────────────────┐
        │                       │               │                       │
 ┌──────▼──────┐        ┌───────▼──────┐  ┌──────▼───────┐       ┌───────▼───────┐
 │  registry   │        │     fleet    │  │   router     │       │    context    │
 │ modules.yaml│        │ orchestrator │  │ cheapest LLM │       │ shared business│
 │ (catalog)   │        │ (per-module  │  │ that's good  │       │ knowledge +    │
 │             │        │  agents)     │  │ enough/task  │       │ approvals log  │
 └─────────────┘        └──────┬───────┘  └──────────────┘       └────────────────┘
                               │
        ┌──────────────────────┼───────────────────────────────────────────┐
        ▼                      ▼                       ▼                     ▼
      billing              support                compliance          edge-sentinel  …
  (+ books, control-tower, market-radar, growth-engine, social-autopilot — see apps/)
```

- **`registry`** ([`modules.yaml`](modules.yaml)) — the declarative catalog: each module's repo, how to deploy it
  (compose), which agents it runs, and which tasks need approval.
- **`fleet`** — the orchestrator. Brings modules up, gives each its agents, runs them on a schedule,
  and drives cross-module workflows.
- **`router`** — picks the model per task from a tiered policy (local → cheap API → premium), with a hard budget.
- **`context`** — the shared business profile + an append-only approvals/audit log.
- **`control_plane`** — the FastAPI service + CLI you drive it with.

## Quickstart

```bash
git clone https://github.com/redevops-io/agentic-os && cd agentic-os
cp .env.example .env          # set your model endpoints + (optional) premium keys
make install                  # uv sync
agentic-os modules            # list the module catalog
agentic-os up control-tower agentic-billing   # bring up a couple of modules + their agents
agentic-os status             # fleet health
agentic-os approvals          # review anything waiting on you
```

Or run the whole control plane in a container:

```bash
docker compose up -d          # control plane on :8080
curl localhost:8080/health
```

## How model routing works (the cost engine)

```yaml
# .env / config: tiers tried in order; first that meets the task's required capability wins
router:
  tiers:
    - name: local      # ~free; bulk of the work
      base_url: http://localhost:11434/v1      # ollama / llama.cpp on your own box
      model: qwen3.5-122b
      good_for: [draft, classify, summarize, route, extract, code]
    - name: cheap      # cents; harder reasoning
      base_url: https://api.moonshot.ai/v1
      model: kimi-k2.7-code
      good_for: [reason, plan, review]
    - name: premium    # the hard 5%
      base_url: https://api.openai.com/v1
      model: gpt-5-codex
      good_for: [hard-reason, adversarial-review]
  monthly_budget_usd: 200
```

Each agent task declares the capability it needs; the router sends it to the cheapest tier that provides it and
falls back up the tiers on failure — keeping >90% of work on local hardware.

## Repo layout

| Path | What |
|---|---|
| [`agentic_os/registry.py`](agentic_os/registry.py) | load + validate the module catalog |
| [`agentic_os/router.py`](agentic_os/router.py) | cost-aware LLM router (tiered, budgeted) |
| [`agentic_os/fleet.py`](agentic_os/fleet.py) | orchestrator: deploy modules, run their agents |
| [`agentic_os/workflows.py`](agentic_os/workflows.py) | cross-module workflows (e.g. new-customer onboarding) |
| [`agentic_os/context.py`](agentic_os/context.py) | shared business context + approvals/audit log |
| [`agentic_os/control_plane.py`](agentic_os/control_plane.py) | FastAPI control plane |
| [`agentic_os/mission/`](agentic_os/mission/) | Mission Runtime — the operator/mission engine the reference apps run on (operators, planes, compiler; Go port in [`go/mission/`](go/mission/)) |
| [`agentic_os/cli.py`](agentic_os/cli.py) | `agentic-os` CLI |
| [`modules.yaml`](modules.yaml) | the module catalog |
| [`docs/`](docs/) | architecture + operations |

## License

AGPL-3.0-or-later. Self-host freely; redevops.io monetizes setup + optional managed cloud. See [LICENSE](LICENSE).

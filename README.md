<div align="center">

# agentic-os

**The integrated control plane and Mission kernel of ReDevOps Runtime.**

[![License: AGPL-3.0 + Commons Clause](https://img.shields.io/badge/License-AGPL--3.0%20%2B%20Commons%20Clause-blue.svg)](LICENSE.md) ![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg) ![Go](https://img.shields.io/badge/Go-1.22%2B-00ADD8.svg) ![maturity](https://img.shields.io/badge/maturity-alpha%20%2F%20reference-orange.svg) [![NVIDIA Inception](https://img.shields.io/badge/NVIDIA-Inception%20Program%20Member-76B900.svg)](https://www.nvidia.com/en-us/startups/)
&nbsp;·&nbsp; self-hosted &nbsp;·&nbsp; source-available (see [LICENSE.md](LICENSE.md))

</div>

> **NVIDIA Inception Program Member** — ReDevOps is a member of the NVIDIA Inception Program, supporting startups advancing AI and accelerated computing. Membership provides access to NVIDIA technology, technical resources, and the startup ecosystem. It does not imply product endorsement by NVIDIA.

---

**What it does.** Runs a catalog of reference business apps as one fleet behind a single control plane: a cost-aware
LLM router, a permissions plane, human approval gates, and the **Mission kernel** (Python) that executes governed multi-step plans with audit and replay.

**Who it's for.** Teams who want to self-host and study a governed agent runtime and its reference apps, and are
comfortable with alpha software.

**Maturity: alpha, reference implementation; deployed internally (self-reported).** Version `0.3.34`. CI runs the
kernel and per-app test suites. Nothing is independently validated and "production-ready" is not claimed. See
[STATUS.md](STATUS.md).

## 3-minute example

Needs Python 3.11+, [`uv`](https://docs.astral.sh/uv/) and `git`. No model key is needed to list the catalog.

```bash
git clone https://github.com/redevops-io/agentic-os && cd agentic-os
git checkout v0.3.34                  # install from a tag; no ReDevOps package is published on PyPI
uv sync                               # or: pip install -e .
uv run agentic-os modules             # lists the 21 modules in modules.yaml (no network, no key)
```

Next steps (need Docker and your own model endpoints): `cp .env.example .env`, `docker compose up -d`, then
`curl localhost:8080/health`. Details in [Quickstart](#quickstart) below. The 21-module output above was
observed on 2026-10-15; the compose path was **not** run for this documentation change.

## Evidence

- CI (`tests` workflow): full kernel suite including the N-invariant conformance gate, then each app's suite.
  See [STATUS.md](STATUS.md) for the last local run.
- Cards for counts and for the one performance thesis in this README: [docs/evidence/](docs/evidence/).
  Each is labelled *Illustrative/synthetic*, *Internally reproduced*, *Externally reproduced* or *Field validated*;
  none is externally reproduced or field validated.

## Limitations

Alpha. Single maintainer organization; no independent validation. The ">90% on local hardware" figure is a design
thesis, not a measurement. Example router config is illustrative. The hosted demo is a demo, not a production
claim. This repository is source-available under a licence that restricts selling it; it is not "open source".
Known gaps: [STATUS.md](STATUS.md).

## Naming

One public product, **ReDevOps Runtime**: *Mission* (governed execution, implemented here), *Context* (context
selection/optimization, [`context-runtime`](https://github.com/redevops-io/context-runtime)) and *Discovery*
(finding/prioritizing opportunities, [`discovery-runtime`](https://github.com/redevops-io/discovery-runtime)).
*Agentic Apps* are applications using the runtime; *Agentic OS* is this integrated control-plane/deployment
surface, not a fourth runtime. Full glossary: [docs/GLOSSARY.md](docs/GLOSSARY.md).

## Docs

[STATUS](STATUS.md) · [ARCHITECTURE](ARCHITECTURE.md) · [GLOSSARY](docs/GLOSSARY.md) · [docs/](docs/) ·
[CONTROL_PLANE_SECURITY](docs/SECURITY_MODEL.md#control-plane-auth-idempotency-cache-and-multi-replica-limits) · [SECURITY](SECURITY.md) · [CONTRIBUTING](CONTRIBUTING.md) ·
[CHANGELOG](CHANGELOG.md) · [LICENSE.md](LICENSE.md)

---

# Reference

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

The maintainers say redevops.io runs on it internally ("headcount-light, agent-heavy"). That is a self-report: no public artifact supports it, and it is **not** evidence that the system scales.

## Why it exists

A small company today glues together five disconnected AI SaaS tools, each a silo with its own data and its own
premium API bill. `agentic-os` replaces that with **one fleet, your data, your hardware**:

- **One control plane** — deploy, start, stop, and observe every module from one place.
- **Cost-aware routing** — every agent task is sent to the cheapest model that can do it well: a local
  `llama.cpp`/`ollama` model on hardware you own for the bulk of the work, a premium API only for the
  hard minority of tasks ("hard 5%" is a design assumption, not a measured share). That per-task routing is the
  core idea.
- **Cross-module workflows** — a new signup isn't five disconnected events; it's one workflow where billing sets up
  the subscription, support sends onboarding, books records the entry, and compliance files the consent record.
- **Human-in-the-loop where it matters** — money, compliance, and infra actions pause for one-click approval.
- **Fine-grained access control** — a permissions plane grants each app/role/user read/write on a database, table
  or corpus, sliced by **row scope** and **column mask**; set it up and preview it live at `/permissions` (grants
  AES-GCM–encrypted at rest, gated write API). See [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md#permissions-plane--access-control-admin).

## Beyond the fleet: Projects, Integrations, Sidekick & the Agent Gateway

The fleet/router/control-plane above is the kernel. (In the `agentic-os-stack` deployment the fleet controller image is replaced by
[`context-runtime`](https://github.com/redevops-io/context-runtime)'s control plane; this repo remains the Mission kernel and the
Projects API.) On top of it the current product surface adds:

- **Projects + Sidekick** — a self-host web app ([`agentic_os/projects_api.py`](agentic_os/projects_api.py)
  serves the UI + API on one origin). **Projects** is the control plane (missions · workflows · attention ·
  approvals · audit); **Sidekick** is the conversational Mission Supervisor *and* an in-product Q&A expert that
  answers, from a curated + sourced knowledge base with a grounded local-model fallback, how credentials are
  handled, where data is processed, how execution is governed, and how the components work
  ([`sidekick_assistant.py`](agentic_os/sidekick_assistant.py) ·
  [`stack_knowledge.py`](agentic_os/stack_knowledge.py); `GET /api/sidekick/help`). A deployment can offer just a
  subset of apps via `$PROJECTS_APPS`.
- **Integration Plane + Connect Compiler** — describe an integration in plain language; the wizard interprets, you
  confirm, the runtime compiles it into a governed connector ([`agentic_os/integrations/`](agentic_os/integrations/)).
  Connector adapters live in [`redevops-connectors`](https://github.com/redevops-io/redevops-connectors) (the provider
  list is maintained in that repo's README; this page names no count). Credentials
  follow a 3-owner boundary — the OAuth *app secret* stays in the connect layer, the end-user *token* lives in the
  CredentialBroker, and a Mission/model only ever sees an opaque reference resolved at the moment of use.
- **Productivity plane** — Google Workspace, Microsoft 365, LibreOffice, and raw file formats (CSV/MD/XLSX/DOCX)
  behind one logical surface, with a 4-way physical strategy (cloud API vs local file / headless / desktop) so
  local work stays on your machine. See [`PRODUCTIVITY_PLANE.md`](agentic_os/integrations/PRODUCTIVITY_PLANE.md).
- **Governed Agent Gateway** — the governed *northbound* path that lets an external agent (Claude, ChatGPT, Cursor,
  your own) invoke ReDevOps capabilities without an ungoverned backdoor ([`agentic_os/agent_gateway/`](agentic_os/agent_gateway/)).
  MCP is its first protocol adapter — not the product. Every call runs the one path: identity → permissions → risk →
  approval → GovernedEnvelope → invoke (a governed capability, or a delegated Mission) → egress policy → audit.
  See [GOVERNED_AGENT_GATEWAY_IMPLEMENTATION_PLAN.md](GOVERNED_AGENT_GATEWAY_IMPLEMENTATION_PLAN.md); to connect a client, [docs/AGENT_GATEWAY_QUICKSTART.md](docs/AGENT_GATEWAY_QUICKSTART.md).

## Architecture (overview diagram)

Ownership of each concern (interfaces, persistence, authorization, model routing, retrieval, receipts) is in
[ARCHITECTURE.md](ARCHITECTURE.md).

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

Or self-host just the **Projects + Sidekick** app (UI + API on one origin — needs Python 3.11+ and Git):

```bash
# Not on PyPI: no ReDevOps package is published there. Install from a git tag (needs git on PATH):
pip install 'agentic-os[projects] @ git+https://github.com/redevops-io/agentic-os.git@v0.3.34'   # add [office] for the productivity plane
agentic-os-projects                   # serves the Projects UI + API on :8787
# hosted reference: https://demo.redevops.io/projects  (a public demo; not verified here)
```

> PyPI has unrelated third-party projects named `sidekick` and `agent-harness`. `pip install sidekick` or
> `pip install agent-harness` installs someone else's code, not ReDevOps software.

## How model routing works (the cost engine)

The config below is an **illustrative example**: model names and the budget are placeholders and were not
checked against current provider catalogues.

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
falls back up the tiers on failure. The design thesis is that most work can stay on local hardware (the original
target was ">90%"); that share has **not been measured**. See
[docs/evidence/aos_local_share_thesis.yaml](docs/evidence/aos_local_share_thesis.yaml).

## Repo layout

| Path | What |
|---|---|
| [`agentic_os/registry.py`](agentic_os/registry.py) | load + validate the module catalog |
| [`agentic_os/router.py`](agentic_os/router.py) | cost-aware LLM router (tiered, budgeted) |
| [`agentic_os/fleet.py`](agentic_os/fleet.py) | orchestrator: deploy modules, run their agents |
| [`agentic_os/workflows.py`](agentic_os/workflows.py) | cross-module workflows (e.g. new-customer onboarding) |
| [`agentic_os/context.py`](agentic_os/context.py) | shared business context + approvals/audit log |
| [`agentic_os/control_plane.py`](agentic_os/control_plane.py) | FastAPI control plane |
| [`agentic_os/mission/`](agentic_os/mission/) | Mission Runtime — the operator/mission engine the reference apps run on (operators, planes, compiler) |
| [`agentic_os/integrations/`](agentic_os/integrations/) | Integration Plane + Connect Compiler contracts, hosted OAuth, the productivity plane, and adapter execution |
| [`agentic_os/intelligence/`](agentic_os/intelligence/) · [`agentic_os/legal/`](agentic_os/legal/) | External & Professional Intelligence layer — apps ask for a **capability**, a gate decides whether to acquire, adapters (open + BYO paid + legal + email) serve it, and value accounting learns what's worth buying ([docs/INTELLIGENCE.md](docs/INTELLIGENCE.md)) |
| [`agentic_os/agent_gateway/`](agentic_os/agent_gateway/) | Governed Agent Gateway — the governed northbound path for external agents (MCP first) |
| [`agentic_os/projects_api.py`](agentic_os/projects_api.py) | Projects UI + API (one origin) |
| [`agentic_os/sidekick_assistant.py`](agentic_os/sidekick_assistant.py) · [`stack_knowledge.py`](agentic_os/stack_knowledge.py) | Sidekick's grounded LLM fallback + curated stack Q&A knowledge base |
| [`agentic_os/sources.py`](agentic_os/sources.py) + `sources_*.py` | Source connectors (LocalFiles, Google Drive, OneDrive, Postgres, RAG) |
| [`agentic_os/permissions.py`](agentic_os/permissions.py) | fine-grained access control (row scope + column mask) |
| [`agentic_os/cli.py`](agentic_os/cli.py) | `agentic-os` CLI |
| [`modules.yaml`](modules.yaml) | the module catalog |
| [`docs/`](docs/) | architecture + operations; [platform secrets](docs/SECRETS.md) (auto-generated, `agentic-os secrets init`) |

## License

**Source-available, not open source** (the wording of [LICENSE.md](LICENSE.md)): AGPL-3.0-or-later **WITH** the Commons
Clause. `LICENSE.md` is the licence this README quotes. Self-host freely, including inside a company for its own business. What
the Clause removes is the right to *sell it* — to charge third parties for the
software, for hosting it, or for services whose value derives substantially
from it. That is the part redevops.io reserves.

Releases up to and including `v0.1.0` remain available under AGPL-3.0-or-later
alone; this is not retroactive. See [LICENSE.md](LICENSE.md) for the reasoning,
the effective commit, and what it means for anything depending on this package.

## Control-plane security

Mutating control endpoints (`/up`, `/down`, `/dispatch`, approvals, `/api/bridge/invoke`, mission approve) require
`Authorization: Bearer $AGENTIC_OS_CONTROL_TOKEN` and return 503 when it is unset; read-only endpoints stay open.
The `/missions` API is default-deny: every non-GET route AND every read except the static shell/catalogue needs a bearer
token (optionally tenant-scoped via `AGENTIC_OS_TENANT_TOKENS`, optional distinct approvers via
`AGENTIC_OS_APPROVER_TOKENS`; `AGENTIC_OS_GATE_READS=0` opts out of read gating, unsafe off loopback).
Operator `POST /invoke` takes the same bearer credentials (admin or tenant; approver tokens are refused) and returns 503
when none is configured; `AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS=1` opens it for local development only (ignored once any
token is set). The runtime's HTTP operator client sends `AGENTIC_OS_CONTROL_TOKEN` automatically.
The Projects API is same-origin by default (`PROJECTS_CORS_ORIGINS` opts in). See
[docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md#control-plane-auth-idempotency-cache-and-multi-replica-limits).

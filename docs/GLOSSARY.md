# Glossary

One public product name: **ReDevOps Runtime**. This page is the canonical glossary for the core repositories;
other READMEs link here instead of redefining terms. Wording follows the 2026-10-15 public-claim inventory.

| Term | Use it for | Do not use it for |
|---|---|---|
| **ReDevOps Runtime** | The one public product: a self-hostable runtime layer made of three parts, Mission, Context, Discovery. | A single repository, or "the thing in `agentic-os`". |
| **Mission** (Mission Runtime) | **Governed execution**: a typed plan (`MissionProgram`) run step by step behind approval gates, with audit and replay. Implemented in `agentic_os/mission/`; front door: [`mission-sdk`](https://github.com/redevops-io/mission-sdk); wire types: [`runtime-contracts`](https://github.com/redevops-io/runtime-contracts). | A proposal record, a sub-step, or an "integration mission". Say *proposal* or *step* for those. |
| **Context** (Context Runtime) | **Context selection and optimization**: deciding what a model sees, with an inspectable plan (`explain`) and learning from outcomes. Repo: [`context-runtime`](https://github.com/redevops-io/context-runtime). | The shared business profile in `agentic_os/context.py` (call it the *business context store*), or live repository context (sidekick). |
| **Discovery** (Discovery Runtime) | **Finding and prioritizing opportunities**; in code today, turning a requester's words into a sealed `VerifiedIntent` and a `learn` lifecycle. Repo: [`discovery-runtime`](https://github.com/redevops-io/discovery-runtime). | Generic monitoring. (The repo's GitHub description describes a broader "proactive layer" than the README implements; the README is the accurate one.) |
| **Agentic Apps** | Applications that use the runtime. The reference apps under [`apps/`](../apps/README.md) are examples. | The runtime itself. |
| **Agentic OS** | The **integrated control-plane and deployment surface** in this repository (`agentic-os`): registry, fleet, router, Projects UI, permissions plane, Agent Gateway. | A fourth runtime. Legacy phrase "Agentic Business OS" appears in the CLI help and package description; treat it as the same thing. |
| **Control plane** | The FastAPI service and CLI in this repo that deploy, observe and approve. | The Mission kernel. |
| **Projects** | The human workspace UI (`agentic_os/projects_api.py`, demo at `/projects`). The separate [`redevops-projects`](https://github.com/redevops-io/redevops-projects) repo is the P1 front-end shell only. | The whole product. |
| **Sidekick** | In this repo: the conversational Mission Supervisor / Q&A assistant inside Projects. Other, unrelated uses exist: the `sidekick` repo (a local coding-agent orchestrator), a Context Runtime example tenant, and the deploy "sidekick-devops". | A PyPI package. PyPI `sidekick` is an unrelated third-party project. |
| **Receipt** | A record built from persisted events: the mission **Action Receipt** (`agentic_os/mission/receipt.py`), inference receipts (`governance/receipt.py`), email delivery receipts. In `mission-sdk` examples "receipt" informally means the sealed, verifiable case bundle. | A payment receipt. |
| **SDK** | `mission-sdk` (`redevops-mission`), the developer front door. | `redevops-connectors` (a connector contract and adapters), `agent-harness` (a toolkit), `runtime-contracts` (schemas). |

## Evidence labels

Quantitative statements use exactly one of: **Illustrative/synthetic** (simulators, constructed data),
**Internally reproduced** (a committed script or artifact re-run by the maintainers), **Externally reproduced**
(re-run by an independent party), **Field validated** (observed in real use by independent parties).
At 2026-10-15 nothing in the core repositories is *Externally reproduced* or *Field validated*.

## Maturity labels

alpha · reference implementation · simulation-only · deployed internally · internally reproduced.
"Production-ready" is not claimed for any component.

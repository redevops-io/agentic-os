# Architecture

`agentic-os` is deliberately thin: it **orchestrates** the redevops.io modules, it does not
re-implement them. Five pieces:

| Component | File | Responsibility |
|---|---|---|
| Registry | `agentic_os/registry.py` | load + validate `modules.yaml` (the module catalog) |
| Router | `agentic_os/router.py` | send each task to the cheapest model tier that can do it |
| Context | `agentic_os/context.py` | shared business profile + append-only approvals/audit log |
| Fleet | `agentic_os/fleet.py` | deploy modules (their own compose) + dispatch their agents |
| Control plane | `agentic_os/control_plane.py` | FastAPI surface; `cli.py` is the terminal equivalent |

Alongside this control-plane kernel, [`agentic_os/mission/`](../agentic_os/mission/) is the **Mission
Runtime** — the operator + mission engine the [reference apps](../apps/README.md) run on (typed
operators exposing capabilities/invoke, mission planes, and the compiler). The control plane
orchestrates the fleet; the Mission Runtime executes governed missions on it (Go port in
[`go/mission/`](../go/mission/)). Within a mission, the executor dispatches each ready wave's
independent nodes concurrently through a bounded pool and commits results serially in deterministic
node order, so the event-sourced world and replay identity are unchanged — only wall-clock. It is
opt-in and default-serial: env `AGENTIC_OS_MISSION_CONCURRENCY` (default 1 = the historical serial
drain).

**Evidence-native mission spine (v0.2.x).** A mission binds to a **ContextEpoch** — a content-addressed
`ContextView` ([`mission/context_view.py`](../agentic_os/mission/context_view.py)) that pins the exact
evidence a decision used. A mission carries `intent_content_hash` / `evidence_refs` / `context_epoch_id`,
and `create_mission(verified_intent=…)` carries the sealed intent across the Discovery→Mission boundary
(previously dropped). `rehydrate()` is now **exact replay** — it recompiles pinned to the original
evidence and verifies the sealed plan fingerprint + epoch, failing closed (`ReplayError`) on drift;
`re_evaluate()` is the explicit re-planning against *current* evidence. A typed `EVIDENCE_CHANGE` event
lets Governance correlate evidence deltas against action trajectories.

## Planes on top of the kernel

Beyond the control-plane kernel and Mission Runtime, several planes make up the current surface:

| Plane | File(s) | Responsibility |
|---|---|---|
| Integration Plane + Connect Compiler | [`agentic_os/integrations/`](../agentic_os/integrations/) | NL "describe an integration → confirm → compile to a governed connector" wizard; capability manifest; hosted OAuth; the productivity plane (Google Workspace / Microsoft 365 / LibreOffice / file formats); adapter execution under a `GovernedEnvelope`. Connector adapters live in `redevops-connectors`. |
| Governed Agent Gateway | [`agentic_os/agent_gateway/`](../agentic_os/agent_gateway/) | the governed **northbound** path for *external* agents (Claude/ChatGPT/Cursor/custom). MCP is the first protocol adapter. One path: identity → permissions → risk → approval → `GovernedEnvelope` → invoke (a governed capability, or a delegated Mission) → egress policy → audit. |
| Projects + Sidekick | [`agentic_os/projects_api.py`](../agentic_os/projects_api.py), [`sidekick_assistant.py`](../agentic_os/sidekick_assistant.py), [`stack_knowledge.py`](../agentic_os/stack_knowledge.py) | the human control plane (missions/workflows/attention/approvals/audit) + the conversational Mission Supervisor and in-product Q&A expert (curated + sourced KB with a grounded local-model fallback). |
| Sources | [`agentic_os/sources.py`](../agentic_os/sources.py) + `sources_*.py` | governed source connectors (LocalFiles, Google Drive, OneDrive, Postgres, RAG) — the read/ingest side of the Integration Plane. |
| Permissions | [`agentic_os/permissions.py`](../agentic_os/permissions.py) | fine-grained access control (row scope + column mask); see [permissions.md](permissions.md). |

The public-facing runtime taxonomy on redevops.io — **Discovery Runtime · Execution Planner · Mission Runtime · Context Runtime · Governance Plane · agent-harness · ReDevOps RAG** — is the same machinery described from the operator's point of view; the Mission Runtime + planner + context/router pieces above are where it lives in this repo.

## Request flow

1. An event (new signup, security alert, scheduled tick) starts a **workflow** (`workflows.py`)
   — an ordered set of agent dispatches across modules.
2. For each step the **Fleet** asks: does this module mark this action `approval_required`?
   - **Yes** → record a PENDING approval in **Context**; stop short of executing.
   - **No** → build the agent's system prompt and hand the task to the **Router**.
3. The **Router** picks the cheapest tier whose `good_for` includes the task's capability,
   calls its OpenAI-compatible endpoint, accounts the cost against the monthly budget, and
   falls back up the tiers on transport failure.
4. A human approves/rejects pending actions via the control plane; approved actions execute.

## Why a router, not a model

The thesis is cost: keep >90% of agent work on local hardware (an EVO-X2 / proxmox box running
`llama.cpp`/`ollama`) and pay for premium tokens only on the hard minority. The router makes
that policy declarative (`config.yaml`) and per-task (`Task.capability`).

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

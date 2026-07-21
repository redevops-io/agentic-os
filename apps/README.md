# Reference apps

The reference application implementations that run on the Mission Runtime — one focused,
open-source agentic system per business function. Each app is the *implementation* (agents +
operators + core logic + seed data); the **deployment** is kept separate — a self-contained compose
demo lives in [`../deploy/`](../deploy/), while the full Terraform / Ansible / k3s machinery is
maintained separately — so you can run these apps under your own infrastructure.

Each app follows the v6 operator pattern:

| File | Role |
|---|---|
| `core.py` | pure business logic, no side effects — unit-testable |
| `operator.py` | the policy-guarded, gated mutation surface (a Mission Runtime operator) |
| `app.py` | the service entrypoint (conversational agent: Context Runtime + redevops-rag grounding) |
| `seed.py` / `seed.rb` | demo fixtures |
| `test_operator.py` | operator tests |

The catalog that binds these to the fleet — repo, agents, and which tasks need approval — is
[`../modules.yaml`](../modules.yaml). The kernel they run on is [`../agentic_os/mission/`](../agentic_os/mission/)
(Python) and [`../go/mission/`](../go/mission/) (Go).

## Apps

`billing` · `books` · `compliance` · `support` · `control-tower` · `edge-sentinel` ·
`growth-engine` · `growth-assistant` · `market-radar` · `social-autopilot` · `outreach-engine` ·
`lifecycle` · `guide` · `agentic-crm` · `agentic-privacy` — plus `infra` (deploy / teardown /
cost-audit / drift) and `sky` (SkyPilot elastic-compute), the operators that turn a deployment
into a governed mission.

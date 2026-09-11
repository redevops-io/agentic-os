# ReDevOps Governed Agent Gateway — Implementation Plan

> **Status (2026-09-10).** Phase 0 (contracts + policy-scoped registry + the single governed
> invocation path) is landed in [`agentic_os/agent_gateway/`](agentic_os/agent_gateway/). Phase 1
> (governed MCP read path: `auth.py` token→principal seam + `protocols/mcp.py` bridge + read
> capabilities) and Phase 2 (mission delegation via `mission_adapter.py`) are in review. Later
> phases — governed writes, data-egress enforcement, OAuth 2.1 + PKCE authorization server + the
> FastMCP transport shell, approval cards + undo, the action sandbox — are as described below.

## Goal
Productize a secure northbound interface that lets external agents (Claude, ChatGPT, Cursor, VS Code, custom agents) invoke ReDevOps capabilities without bypassing permissions, policy, approval, evidence, audit, or Mission Runtime.

## 1. Product decision
Build a **Governed Agent Gateway**, not “an MCP server” as the product.

MCP is the first protocol adapter.

```text
External Agent
    │
    ├─ MCP
    ├─ REST
    ├─ future agent protocols
    └─ Sidekick-native
    │
    ▼
Governed Agent Gateway
    │
    ├─ identity
    ├─ tenant/workspace scope
    ├─ permissions / ABAC
    ├─ risk classification
    ├─ budgets / rate limits
    ├─ approvals
    ├─ evidence
    ├─ output filtering / data-egress policy
    ├─ idempotency
    └─ audit
    │
    ├──────────────► Direct governed capability invocation
    │
    └──────────────► Mission Runtime delegation
                         │
                         ▼
                 Context Runtime / Planner
                         │
                 Apps / Plugins / Humans
```

Product claim:

> **Bring any agent. ReDevOps governs what it can do inside your systems.**

Do not claim that ReDevOps governs the external agent itself. We cannot control its hidden context, reasoning, other tools, or later actions. We govern the capability boundary and all operations performed through it.

## 2. What to expose
Expose semantic capabilities, not raw CRUD endpoints.

Bad:
- crm_create_contact
- crm_update_contact
- crm_delete_contact
- mission_insert
- mission_patch

Better:
- crm.research_lead
- crm.qualify_lead
- crm.prepare_outreach
- crm.request_outreach_send
- projects.create_project
- projects.summarize_project
- missions.start
- missions.status
- missions.pause
- missions.cancel
- missions.explain
- sources.search
- sources.retrieve_evidence

Flagship capability:

```text
missions.delegate_goal
```

External agents should delegate goals; ReDevOps should retain control of execution planning.

## 3. Required new components

### 3.1 Agent Gateway service
Create:

```text
agentic_os/agent_gateway/
  service.py
  auth.py
  principal.py
  registry.py
  policy.py
  envelope.py
  approvals.py
  egress.py
  audit.py
  idempotency.py
  rate_limit.py
  protocols/
      mcp.py
      rest.py
```

Responsibilities:
- protocol termination
- OAuth/token handling
- tenant/workspace resolution
- principal construction
- capability discovery
- permission filtering
- governed invocation
- approval orchestration
- response filtering
- audit
- telemetry
- policy-safe error handling

### 3.2 OAuth 2.1 + PKCE
Implement:
- OAuth 2.1
- PKCE
- Dynamic Client Registration where needed
- short-lived access tokens
- refresh-token rotation
- per-workspace/per-tenant scopes
- audience restriction
- revocation
- audit of token grants

Fine-grained permissions remain invocation-time decisions in the existing permissions plane.

### 3.3 Policy-scoped capability registry

```text
Fleet Capability Registry
        ↓
Principal / tenant / workspace
        ↓
Policy filter
        ↓
Agent-visible capability registry
```

Each capability should declare:
- name
- description
- typed inputs/outputs
- permissions
- risk tier
- side-effecting flag
- approval policy
- cost hint
- idempotency
- data classes
- egress class

The MCP tool list must be generated from the filtered registry, never from raw app routes.

### 3.4 Governed invocation path
Every protocol request must use the same path:

```text
Protocol Request
  ↓
Authenticated Principal
  ↓
Capability lookup
  ↓
Permission / ABAC check
  ↓
Risk classification
  ↓
Budget / rate-limit check
  ↓
Guardrail / policy check
  ↓
GovernedEnvelope
  ↓
Approval if required
  ↓
App or Mission Runtime
  ↓
Verification
  ↓
Output / egress policy
  ↓
Audit + evidence
  ↓
Protocol response
```

There must be no MCP-only bypass.

## 4. Mission delegation
Minimum gateway-facing mission capabilities:

- missions.delegate_goal
- missions.get
- missions.list
- missions.cancel
- missions.pause
- missions.resume
- missions.explain
- missions.list_pending_approvals
- missions.submit_approval

This should be the preferred path for complex work because it preserves:
- planner control
- permissions
- human gates
- verification
- retries
- idempotency
- sagas
- learning
- mission-level EXPLAIN

## 5. Direct capability invocation
Keep a second path for bounded, low-risk operations:
- sources.search
- crm.lookup_account
- projects.get_status
- support.get_ticket

For writes, prefer:
- prepare_* for non-side-effecting work
- request_* for governed side effects

Avoid exposing unrestricted mutation primitives.

## 6. Data-egress policy
Because external agents are outside the trust boundary, add an output policy layer.

Actions:
- ALLOW
- REDACT
- SUMMARIZE
- TOKENIZE
- DENY
- REQUIRE_APPROVAL

Examples:
- allow summary but not raw customer rows
- allow aggregate analytics but not PII
- redact secrets
- block sensitive data classes
- require approval before exporting artifacts

Every response should carry internal metadata:
- data classes
- source refs
- principal
- tenant
- client_id
- egress decision
- policy rule IDs

## 7. Security model
Treat the external agent as **untrusted but authenticated**.

Trust only:
- verified identity
- explicit scopes
- gateway policy engine
- internal services
- audit/event store

Do not trust:
- external model reasoning
- external agent memory
- client-side confirmation
- external prompt safety

### Least privilege
Each mission node should receive only the grants required by its capability, not the union of all mission permissions.

### Side-effect tiers
- Tier 0 — read-only, non-sensitive
- Tier 1 — bounded/reversible write
- Tier 2 — consequential/external
- Tier 3 — financial, destructive, legal, security-sensitive

Suggested policy:
- Tier 0: automatic
- Tier 1: automatic if policy allows
- Tier 2: approval by default
- Tier 3: mandatory approval + stronger verifier + evidence pack

### Idempotency
Every side-effecting capability must accept an `idempotency_key` and dedupe server-side.

## 8. Action sandbox — separate but composable
Do not claim the gateway sandboxes external agents.

Build sandboxing as a separate internal capability:

```text
External Agent
  ↓
Gateway
  ↓
Mission Runtime
  ↓
sandbox.execute
  ↓
Isolated runtime
```

Requirements:
- ephemeral container or microVM
- deny-all egress default
- explicit allowlist
- no ambient credentials
- scoped ephemeral secrets
- CPU/memory/wall-time limits
- filesystem quota
- action→observation contract
- complete event log
- governed artifact export

## 9. Observability / EXPLAIN
Record for every gateway call:
- external client
- principal
- tenant/workspace
- requested capability
- why it was exposed
- permission decision
- policy decision
- risk tier
- approval decision
- cost
- latency
- evidence refs
- egress decision
- verification result
- side effects
- idempotency key
- mission/node IDs

Add:
- `GET /agent-gateway/explain/{request_id}`
- gateway trace linkage inside `GET /missions/{id}/explain`

## 10. MCP implementation
Ship:
- Streamable HTTP
- OAuth-based remote access
- optional stdio for local development

Initial MCP tools:
- missions_delegate_goal
- missions_get
- missions_explain
- missions_cancel
- projects_get
- projects_create
- crm_lookup
- crm_research_lead
- crm_prepare_outreach
- sources_search
- sources_get_evidence

Do not auto-wrap every REST route.

Test against:
- Claude
- ChatGPT
- Cursor
- VS Code
- MCP Inspector
- generic SDK client

## 11. Approval UX
Implement:
```text
ApprovalRequested
  ↓
Control Plane / Sidekick inbox
  ↓
Approve / Reject / Edit
  ↓
Mission resumes
```

For reversible actions, optionally add an undo window backed by declared compensation capabilities.

## 12. Open-core placement

### Open source
- MCP/Agent Gateway protocol layer
- capability manifest schema
- filtered capability registry
- governed invocation interface
- local OAuth dev mode
- request tracing
- reference app bindings
- MCP conformance tests

### Enterprise/private
- advanced multi-tenant identity
- ABAC/fine-grained permissions
- row/object-level access policies
- policy administration
- data-egress controls
- approval routing
- compliance retention
- break-glass
- enterprise SSO
- governance analytics

## 13. Implementation phases

### Phase 0 — Contracts
Ship schemas for capability manifests, principals, requests, decisions, results, risk tiers, egress decisions, and idempotency.

### Phase 1 — Governed MCP read path
Expose read-only Projects, Missions, CRM, and Sources over MCP with OAuth and policy filtering.

Acceptance:
- external client connects
- sees only allowed capabilities
- cannot enumerate hidden capabilities
- all requests audit
- tenant-boundary tests pass

### Phase 2 — Mission delegation
Expose:
- missions.delegate_goal
- missions.status
- missions.explain
- missions.cancel

Acceptance:
- external agent delegates a mission
- Mission Runtime executes internally
- policy cannot be bypassed
- mission can pause/resume for approval
- full EXPLAIN exists

### Phase 3 — Governed writes
Add bounded write capabilities such as:
- crm.prepare_outreach
- crm.request_outreach_send
- projects.request_change

Acceptance:
- risk tiers work
- approvals work
- retries are idempotent
- rejection produces no side effect
- audit includes evidence and policy provenance

### Phase 4 — Data-egress enforcement
Add response classification and policy.

Acceptance:
- PII can be redacted or denied
- aggregates can be allowed while rows are blocked
- EXPLAIN shows the egress decision
- restricted output never reaches the client

### Phase 5 — Approval cards + undo
Add Sidekick/control-plane inbox and compensation-backed undo.

### Phase 6 — Action sandbox
Promote Agent Harness into a real isolated execution runtime.

### Phase 7 — Protocol expansion
Only after MCP usage is validated:
- REST delegation API
- additional agent protocol adapters
- event/webhook triggers
- agent federation

## 14. Testing
Cover:
- OAuth/PKCE
- token revocation/expiry
- wrong audience
- cross-tenant attempts
- hidden capability enumeration
- denied capability invocation
- row/object permissions
- retry/idempotency
- saga compensation
- PII redaction/denial
- mission persistence across disconnect/restart
- approval pause/resume
- malformed MCP clients

## 15. Flagship demo
### Bring Your Own Agent

Prompt:

> Find five high-fit pilot prospects, research them, prepare outreach, and request approval before sending anything.

External agent calls:
```text
missions.delegate_goal
```

ReDevOps internally:
```text
Discovery
→ CRM
→ Sources
→ Context Runtime
→ Planner
→ verification
→ approval
→ outreach
→ outcome learning
```

Show:
- capability filtering
- plan
- approval
- EXPLAIN
- audit
- outcome

## 16. Messaging
Avoid:
> MCP server for Agentic Apps

Prefer:
> **Governed Agent Gateway**

Supporting line:
> Connect Claude, ChatGPT, Cursor, or your own agents to enterprise apps without giving them an ungoverned backdoor.

Technical line:
> MCP is the first protocol. Every operation still runs through ReDevOps identity, permissions, policy, approval, verification, evidence, and audit.

Mission-level differentiator:
> External agents can delegate goals. ReDevOps decides how those goals execute.

## 17. Priority
1. Governed Agent Gateway (MCP first)
2. Mission delegation through the gateway
3. Autonomous enrichment/research
4. Action sandbox
5. Support-autonomy pack
6. Additional protocol adapters

## 18. Non-goals
Do not:
- claim to govern an external agent's hidden reasoning
- expose raw DB access through MCP
- auto-wrap all REST endpoints as tools
- make MCP the internal app-to-app protocol
- bypass Mission Runtime for complex work
- rely on client-side confirmation as authorization
- treat sandboxing as an MCP feature
- leak hidden capabilities through errors
- expose sensitive raw data merely because a high-level capability is allowed

## 19. First implementation slice

```text
External MCP client
    ↓ OAuth 2.1 / PKCE
Agent Gateway
    ↓
policy-scoped capability registry
    ↓
missions.delegate_goal
    ↓
Mission Runtime
    ↓
CRM + Sources + Projects
    ↓
approval if side-effecting
    ↓
EXPLAIN + audit
```

Start with only:
- Missions
- CRM
- Sources

**Acceptance criterion:** An external agent can discover a small principal-specific capability set, delegate a real multi-step mission, receive a mission ID, inspect status, and request/submit approvals while every internal action remains subject to the same permissions, policy, verification, idempotency, evidence, and audit guarantees as Sidekick.

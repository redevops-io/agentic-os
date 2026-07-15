# agentic-os/go — Mission Runtime (Go)

A Go port of the Python **v5 Mission Runtime** kernel (`agentic_os/mission`). Same event-sourced,
resumable design; same layered architecture (plan → simulate → run → gate → compensate → learn).
The point of the port: a Go runtime can orchestrate the existing Python agentic apps unchanged,
because the runtime↔app boundary is the language-agnostic **HTTP `/invoke` operator contract**.

```
Mission → ExecutionPlan (revisions) → ExecutionGraph
        executed over an event-sourced world-state blackboard
```

## Package `mission`

Foundation
- `types.go` — enums + the data model (Mission / Node / ExecutionPlan / Graph / CapabilitySpec /
  IntentStep / Budget / Belief / evidence + verification + approval records), json-tagged.
- `store.go` — `EventStore` (append-only + subscribers + JSONL persistence/reload), `WorldState`
  (observe → fused-belief blackboard), `MissionRepository` (state/node/pending-human folds).

Planning + execution
- `belief.go` `cost.go` `registry.go` — sensor-fusion, cost model, capability discovery + policy scoping.
- `compiler.go` `scheduler.go` `simulator.go` `planner.go` `templates.go` — logical→physical
  compile (fail-closed), cost-aware Kahn scheduling, dry-run projection, template/model planners.
  `templates.go` carries the full v6 set at parity with Python: `onboarding · invoice_recovery ·
  deploy_app · teardown_app · cost_audit · revenue_rescue · product_launch`.
- `executor.go` `operators.go` `operator_sdk.go` `httpclient.go` — the executor + saga compensation,
  `HTTPOperatorClient`/`LocalOperatorClient`, and the `Operator` SDK exposing **GET /capabilities**
  + **POST /invoke** (server-side idempotency).

Planes (whitepaper P5, P7–P11)
- `learning.go` — four learning loops + reflection.
- `evidence.go` `verify.go` — P7 evidence ledger + acceptance verification (accept/reject/escalate).
- `policy_decision.go` — P8 multi-factor approval decision + evidence-backed packet.
- `plan_select.go` — P9a active plan selection (bounded candidates → simulate → score → select).
- `learners.go` — P9b routing/model/plan recommenders behind a promotion gate.
- `governance.go` — P10 policy-version + interventions read-model.
- `resource_scheduler.go` — P11 fair-share cross-mission admission control.
- `runtime.go` — `MissionRuntime`: the resumable run loop that ties it together (verification gate,
  approval gate, disambiguation, sagas, EXPLAIN, mid-flight policy edits, rehydrate/restart).

## Driving Python apps from a Go runtime

The runtime never imports the apps — it dispatches over `OperatorClient.Invoke`. Use
`HTTPOperatorClient` (POST `/invoke`) for remote apps or `LocalOperatorClient` for co-located Go
operators. A Python app becomes an operator by exposing the same `/invoke` contract; the Go runtime
is oblivious to the language on the other side of the hop.

```bash
go test ./...
```

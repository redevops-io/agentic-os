# Security profile and the operator authority boundary

Authentication says who the caller is. It does not say what that caller may do. This page describes how the platform
keeps those apart.

## Profiles (`AGENTIC_OS_PROFILE`)

| Profile | Meaning |
|---|---|
| `production` | A durable authority store, guard and ledger are required; the mission runtime, gateway and Fleet refuse to start without one. Every mission node is checked against the owner's envelope. Operators refuse a side-effecting call that has no approval or execution authorization, and require a durable, shared replay store. |
| `development` | Permissive local mode. Explicit and logged. |
| `test` | Injected guards and clocks; durability not required. |

An **unset** profile behaves as `development` and logs a warning (once per component), so an install without an envelope
database keeps working. `AGENTIC_OS_REQUIRE_AUTHORITY=1` is kept as an alias for `production`. Any other value is an error.
Official production deployments must set `AGENTIC_OS_PROFILE=production` explicitly; the Compose file passes the variable
through to every service that decides authority (empty means development).

**See the posture:** `GET /health` on the control plane and the suite server include `security`; every operator serves
`GET /_security`. A deployment that is in development mode, or whose guard or replay store is not durable, shows
`"ok": false` with the reasons: wire that into readiness checks and Edge Sentinel.

**Check before rollout:** `python -m agentic_os.mission.profile --require-production` exits 1 and lists every missing
requirement (profile, `AGENTIC_OS_AUTHORITY_DB`, `AGENTIC_OS_REPLAY_DB`, a signing secret of 32+ characters, an operator
token, no anonymous operator access).

## The operator boundary

In production an operator's `POST /invoke` runs a **side-effecting** capability only if the call carries one of:

1. a **human approval token** (`approval.py`): unchanged, bound to operator, capability, mission, node and inputs, single-use; or
2. an **execution authorization** (`execution_auth.py`): issued only after the owner's envelope and policy were evaluated.

A valid operator or tenant token alone is refused with 403 *before* the handler runs (it neither executes nor stages).
Reads need only authentication.

An execution authorization is bound to: operator, capability, tenant, mission, node, attempt, reservation (lease), envelope
digest, idempotency key and the canonical effective inputs. Change a recipient, an amount, the capability or the key and it no
longer verifies. The mission identity the handler sees comes from the **signed claims**, never from a `mission_id` in the body.
It is short-lived and **single-use**: the nonce is consumed through the replay store, which in production must be durable and
shared between replicas. It is signed with a key derived separately from the approval key, so an authorization can never pass
as an approval or the reverse. An authorization can never satisfy a capability that declares a mandatory approval.

**Issuers (the only two):**
* the mission runtime, at dispatch, only for the one caller that wins the reservation's RESERVED to DISPATCHED transition;
* the direct-action authorizer (`POST /actions/authorize`, mounted when the runtime has a guard), for an authenticated user acting
  without a mission. It evaluates the same envelope and limits, never authorizes a mandatory-approval capability, and leaves the
  reservation RESERVED: the operator performs the dispatch when it consumes the token, so an unused authorization costs nothing
  once it expires and can be swept, and a swept or already-used reservation can never be dispatched.

**After a timeout:** the reservation becomes UNKNOWN and stays counted. Because the dispatch transition can be won only once, no
fresh authorization exists for that attempt, so the effect cannot be retried by minting another token. Reconcile it
(`AuthorityGuard.reconcile(lease, executed)`) after an independent check.

Operators that share the runtime's `AGENTIC_OS_AUTHORITY_DB` settle (or mark UNKNOWN) the reservation behind an authorization they
consumed; required for the direct-action flow, redundant with the runtime for mission calls.

## The apps' own REST routes

Every state-changing route an app serves declares a policy in `apps/<app>/route_policy.py`; one middleware
(`agentic_os/app_kit/route_authz.py`) enforces it. In production an unauthorised call is refused before the handler runs and a write
request matching no policy is refused.

| Class | Meaning |
|---|---|
| `read_only` | a POST used for a query |
| `public_intake` | anonymous by design, with a written rationale (the privacy request forms) |
| `stages_only` | the core stages unless an approval marker is injected; the marker comes only from an authenticated context |
| `authority_required` | a human holding the permission, an agent authorization, or a service capability |
| `human_only` | a person holding the permission: agents and services are refused |
| `operator_boundary` | the operator's `/invoke` (enforced by the operator) |

Contexts: **human** (operator credential, admin token, or a tenant token that holds the route's permission; a tenant token alone is not enough),
**agent** (`X-Execution-Authorization`, bound to app, capability, tenant, idempotency key and the exact request; single-use), **service**
(`X-Service-Token` with explicit capabilities). A request carrying an authorization is an agent request and gets no human privileges from any other header.
On a multiplexed `/agent/run` route an agent earns an approval marker only for actions listed in the route's `agent_actions` (none by default).

`approval_required` is enforced on every operator `/invoke` in every profile: without a verified human approval the handler does not run
(`AGENTIC_OS_ALLOW_UNAPPROVED_INVOKE=1` is a non-production testing override).

Adding a write route without a policy fails CI. See `audit/operator_capability_inventory.md` (generated by `scripts/audit_operator_capabilities.py`)
and findings F20 to F22 in `audit/AUTONOMY_AUTHORITY_AUDIT.md`, including what this does NOT cover (the kernel services, and network egress).

## The network boundary

Authorization decides who may do what through our APIs; it cannot constrain a process that opens its own socket or shells out. The apps stack can
run behind an allowlist egress proxy on an internal network (`deploy/apps/compose.egress.yml`, or `deploy/k8s/networkpolicy-egress.yaml`):
no route out except the proxy, which denies by default, refuses private/loopback/metadata destinations unless a rule says `private`, connects only to
the address it checked, and logs every decision. It is **opt-in** (the default install is unchanged), its allowlist is **global**, and it does not
inspect payloads. `profile.status()` reports `egress_proxy_configured` as an advisory, and `--require-egress` makes the production preflight fail without one.
Full guarantees and limits: `deploy/egress/README.md`.

## Unknown outcomes: park, reconcile, resume

When a dispatched node's outcome is unknown (the process died, or a side-effecting call errored after it may have been sent), the mission is parked in
`WAITING_RECONCILIATION`, durably, and nothing more is dispatched. A person with an approver or admin credential reconciles it with evidence:

```
POST /missions/{id}/reconcile
{"node_id": "...", "outcome": "not_executed" | "executed", "evidence": {"kind": "provider_record", "reference": "..."},
 "reason": "...", "result": {...only when later steps read what this node produced...}}
```

`not_executed` releases the reservation and the same mission re-runs the node once; `executed` settles it and marks the node done from the evidence. Completed work is never
rerun; a requester credential can never reconcile; the actor is the credential; the evidence and reason are on the mission log. See `audit/AUTONOMY_AUTHORITY_AUDIT.md` step 8.

## The reconciliation supervisor

A parked unknown outcome is probed on a durable schedule by `ReconciliationSupervisor`: only the read-only probe repeats (bounded, with backoff), the business action never does while the
outcome is unknown, and exhausting the budget escalates to a person instead of resending. Only positive execution evidence or an authoritative negative resolves it; a generic 404 or any
non-authoritative "not found" is not observed, not confirmed absent. Independent branches of the mission keep running; only the node and its dependents wait. How to write a probe and what
"authoritative" requires: `docs/RECONCILIATION_PROBES.md`. No provider adapters ship yet, so today every unknown outcome escalates to a person.

## Hard limits and reconciliation

The serial mission loop enforces `deadline`, `wall_clock_s` (active run time of one driver segment), `usd` and `tokens`: a reached ceiling stops the mission with a
`HardLimitReached` event and a reason. A reservation that may already have produced an effect is never sent again: the runtime refuses a second dispatch in every
profile, and `mission/reconcile.py` settles or releases it only when an independent probe proves the outcome, otherwise it stays counted and is flagged for a person
(`python -m agentic_os.mission.reconcile list|run|resolve`). No provider probes ship yet, so today unknown outcomes need a person.

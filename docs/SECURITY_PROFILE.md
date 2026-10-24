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

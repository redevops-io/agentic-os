# Writing a reconciliation probe

When a dispatched action's outcome is unknown, the supervisor asks the provider, read-only, whether the effect exists. **You write the
adapter; the supervisor decides what to believe.** Read this before you write one.

## The one rule

Only positive execution evidence or an authoritative negative resolves an unknown outcome. Repeating your read-only probe is fine.
Repeating the business action while the outcome is unknown never is, and running out of probe retries never changes that.

## What you return

A `ProbeResult`, never a boolean:

| `outcome` | Meaning | What the supervisor does |
|---|---|---|
| `CONFIRMED_EXECUTED` | The effect exists. Attach `evidence` (kind + a checkable reference), the `provider_ref`, and the real `result` | settles the reservation, restores the result, resumes the mission |
| `CONFIRMED_NOT_EXECUTED` | The effect provably does not exist | accepted **only** if you qualified the adapter for it (below); then releases, re-gates and retries the action once |
| `INCONCLUSIVE` | Not observed / cannot tell | retries the probe with backoff |
| `TRANSIENT_ERROR` | The lookup failed (timeout, 429, 5xx). Set `retry_after_s` if the provider sent Retry-After | retries the probe with backoff, honouring Retry-After (bounded) |
| `UNSUPPORTED` | This provider cannot be reconciled reliably | escalates to a person immediately |

A generic HTTP 404 is `INCONCLUSIVE`: "not found" means "not observed". Use `from_http_status(status)` for the standard mapping.

## What you declare: `Qualification`

```python
Qualification(
    name="ledgerco",
    eventually_consistent=True,      # a fresh effect may not be visible yet (the default: assume it is)
    visibility_window_s=600,         # the provider's documented maximum delay before a fresh effect is visible (None = unknown)
    authoritative_negative=False,    # may an absence from THIS lookup prove the effect did not happen? (the default: no)
)
```

A `CONFIRMED_NOT_EXECUTED` is accepted only when **all** hold, enforced by the supervisor and not by you:

1. the adapter was registered with `authoritative_negative=True`;
2. the result itself says `authoritative_negative=True` (necessary, never sufficient);
3. evidence is attached;
4. if the provider is eventually consistent: it documents a `visibility_window_s` and that window has elapsed since the dispatch.

Anything else is downgraded to `INCONCLUSIVE` and retried. Say `authoritative_negative=True` only for a **strongly consistent lookup by the idempotency
key we sent** (so "zero results" really means "never created"). A search index, a list endpoint with paging, or anything that can lag is not.

If you document a visibility window, no probe is scheduled before it: probes before the window cannot say anything useful.

## Registering

```python
registry = ProbeRegistry()
registry.register("billing.*", my_probe, Qualification("lago", eventually_consistent=False, authoritative_negative=True))
supervisor = ReconciliationSupervisor(runtime, ReconcileJobs(path), registry, policy=PRESETS["default"], notifier=page_someone)
supervisor.tick()        # call it on a schedule; every transition is durable
```

Two adapters for one capability must agree: one saying executed and another saying not executed is conflicting evidence and escalates immediately.

## Retry policy

`RetryPolicy(max_probes=5, initial_s=30, max_s=300, factor=2, jitter=0.2)`: probes at about 30 s, 1 m, 2 m, 4 m, 5 m, with jitter. These are starting values, not
provider guarantees; presets `fast`, `default` and `slow` are provided. Exhausting the budget escalates to a person; it does not resend.

## Escalation and notification

An escalation is recorded once (durably) and the notifier is called with a stable idempotency key (`escalation:<job>:<time>`). Delivery is retried a bounded
number of times if the notifier fails; a receiver should dedupe on the key, because a crash between a successful send and recording it can send once more.

## Fork branches

A fork branch follows the same contract as a plan node. If its call may have been sent and its outcome is unknown (an ambiguous error on a side-effecting capability, or a driver lost with the call in flight), it writes the same `ReconciliationRequired` record (with `fork_id` and `branch_id`), its reservation
stays UNKNOWN, and the mission parks in `WAITING_RECONCILIATION`. Your probe, the supervisor, `reconcile_node` and the inbox need nothing fork-specific: the node id is the branch node id (`<mission>:fork:<fork>:<branch>`), and `ProbeContext.lease` is the branch attempt's reservation.

What resolution does to the fork: **executed** completes the branch from the evidence (return the provider's real `result`; a branch that `produces` a fact requires it) and the same fork carries on, without rerunning branches that succeeded; **authoritative not-executed** makes the next attempt a
new one under a fresh reservation if the branch has attempts left, otherwise the branch fails cleanly (not as an unknown effect). After a process restart the owner re-supplies the fork spec (`forks.register_spec`, then `run_fork`), because branch inputs are not in the log; until then the mission stays PAUSED.

# Replay authorization and evidence queries

Purpose and implementation. Licence wording: source-available (family licence: AGPL-3.0-or-later with the Commons Clause). Not a production-readiness claim.

## What it does

A mission that is rebuilt from the event log (`MissionRuntime.rehydrate`, including the restore at start-up) is **reconstructed**: it can be read
(state, plan, timeline) but not executed. `run`, `approve`, `resume`, `suspend`, `run_batch`, saga and compensation, a `change_policy` that widens
authority (grant, constraints, budget, retroactive revoke), `re_evaluate` and any tool call raise `ResumeNotAuthorized` (HTTP 403 on the approve
routes) until `MissionRuntime.grant_resume` succeeds. The check sits at the lowest layer (`_executor_for`, the single door every tool call uses) as
well as at the public entry points. A plain `revoke` only tightens and stays allowed.

`grant_resume(mission_id, principal=..., ceiling=..., scope=..., action="resume"|"reexecute")` asks the permission plane whether the principal holds
`evidence.<scope>.<action>` (resource kind `tool`, action `read`) and opens a 15-minute grant on that mission; refusals are logged as
`ReplayRefused`, grants as `ReplayAuthorized`. `reexecute` (re-plan and run) is a separate permission from `resume`. The grant is not an approval:
a gated node still needs a currently valid approval.

## Approvals on replay

`approve` records `binding` (sha256 over plan fingerprint, ContextEpoch, intent hash, evidence refs, node and capability), `granted_at` and
`expires_at`. On a reconstructed mission only an approval whose binding equals the current one and which has not expired is honoured; anything else
is logged once as `ApprovalNotHonoured` (`unbound`, `binding_mismatch`, `expired`, `invalid_expiry`) and the node returns to its gate. An
`expires_at` that is missing, in the past, NaN, infinite or not a number is never honoured. The expiry is enforced when an approval is replayed, not on a live run;
a live approval is limited only by the gate TTL (`AGENTIC_OS_APPROVAL_TTL_S`, the age of the parked request). The replay expiry equals
that TTL, or 24 hours when it is unset.

## Unattended resume

There is no default. To let start-up restore and goal loops resume reconstructed missions without a person, set
`AGENTIC_OS_RESUME_SERVICE_PRINCIPAL=<id>` and give that principal the grant `evidence.<scope>.resume` (scope from
`AGENTIC_OS_RESUME_SERVICE_SCOPE`, default `missions`) in the permission plane. `reexecute` is never granted this way. Without it, restored missions
stay read-only (approving one returns 403) and goal loops wait (`waiting_human`).

This is a behaviour change from earlier versions, where a restart left in-flight missions approvable.

## Evidence queries (`agentic_os/evidence_query`)

An optional module for read-only SQL over runtime-registered datasets and artifact files: scope catalog, permission-plane authorization
(`evidence.<scope>.inspect|reconstruct|resume|reexecute`), a statement guard, one sandboxed worker process per query (empty environment, no external
access, locked configuration, resource limits, timeout) and hash-chained audit receipts. It needs `duckdb==1.5.5` (the `duckdb` extra), which neither
release image installs; nothing in the control plane or the Projects server imports it at start-up (the replay gate imports only `authz`).
Tests: `tests/test_evidence_query_boundary.py`, `tests/test_evidence_query_artifacts.py`, `tests/test_evidence_replay_authorization.py`,
`tests/test_replay_residuals.py`.

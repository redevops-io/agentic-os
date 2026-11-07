# Evidence and replay boundary (D-1)

> PRIVATE until the fix ships: this document describes a vulnerability class and its containment. It lives on the local branch
> `post-rc/evidence-boundary` only. Licence wording: source-available (family licence: AGPL-3.0-or-later with the Commons Clause).
> This is not a production-readiness claim.

> Related runtime authority work (see [SECURITY_MODEL.md](SECURITY_MODEL.md), "Execution authority in every profile, operator principals and
> integrity records"): a mission rebuilt from the log stays read-only until `grant_resume`, and that is unchanged. In addition, `grant_resume`
> and `ServiceResume` now require an authenticated principal and the grant belongs to the principal that obtained it; `rehydrate` refuses a log
> that fails its tail-anchor or hash-chain check; and a resumed mission still passes the current-authority check immediately before each side
> effect, so a resume grant and a valid approval do not outlive a revoked or narrowed envelope.

## 1. Purpose

DuckDB stays a core component of Agentic OS audit, evidence retrieval and replay, and expressive SQL over authorized runtime
artifacts is preserved. This design makes that safe: **unauthenticated or insufficiently scoped query execution must never reach
anything outside the artifacts the caller is authorized to inspect**, and **inspection must be a different privilege from replay**.

Three operations are supported:

1. **Artifact inspection** (execution traces, tool outputs, context snapshots, evidence): authorized read access through scoped SQL.
2. **Audit and investigation** (correlate events, reconstruct decisions, inspect historical state): permission-aware queries.
3. **Recovery and replay** (resume from a checkpoint with its context and execution state): authorized replay with controlled
   side effects.

## 2. The defect and the redefined D-1 acceptance criteria

Reproducer (observed on loopback by track 1): `POST /api/evidence/query` of `deploy/evidence-explorer-demo/app.py` ran
`read_text('/etc/hosts')` and `glob(...)` with no authentication. The route used one shared in-process DuckDB connection with
default (external access on) settings; the only control was a regular expression ("starts with select/with", forbidden-word list),
which does not know DuckDB table functions.

D-1 is closed only when **all** of these hold (each has tests, section 9):

| # | Criterion |
|---|---|
| A1 | The query route is fail-closed: no credential, wrong credential, or a credential without the scope's `inspect` permission gets 401/404 and no query runs. The authenticated profile refuses to start without a token. |
| A2 | Authorization precedes data resolution: a worker is built only from the artifacts of the scope the caller may inspect (SQL filtering is not a boundary). |
| A3 | Host files, the network, the environment, extensions and the DuckDB configuration are unreachable from query execution **even if the statement guard is bypassed** (sandbox is the boundary; guard is defence in depth). |
| A4 | Expressive read-only SQL (joins, CTEs incl. recursive, windows, aggregates, UNNEST, JSON functions, views) still works for authorized users. |
| A5 | Two scopes with distinct data do not leak through SQL, introspection (`information_schema`, `duckdb_*`), error messages, or the process (separate worker per query). |
| A6 | Artifacts (ledger files, checkpoints, manifests, hashes) cannot be modified through any DuckDB route; tampering is detected by the hash check; symlink and `..` escapes from artifact roots fail. |
| A7 | Resource limits hold: timeout (process-group kill), memory, row cap, result-size cap, input cap. |
| A8 | Every request (allowed or refused) writes an audit receipt (who, scope, statement hash, row count, outcome); an unwritable audit log fails the request closed. |
| A9 | Inspection and reconstruction perform no side effect (asserted with a recording tool). Resume and re-execution need their own permission **and** a currently valid approval bound to the same plan/evidence/intent, enforced in the execution layer, for every entry point. A historical approval receipt never authorizes a new side effect. |
| A10 | Replay correctness is preserved: evidence hashes, checkpoint lineage, context epoch, tool-call receipts and approval decisions stay available and verifiable. |
| A11 | The anonymous demo exists only as a separate deployment profile (own entrypoint and image target, synthetic allowlisted dataset, no replay/approval/operational routes), not as a switch on the normal deployment. |

## 3. Architecture

```
 authenticated user / Sidekick / agent
        |  credential (bearer token in the AGPL core; Enterprise IdP behind the same protocol)
        v
 identity + session + permission context       agentic_os.overlays.Principal  +  Ceiling (IdentityCeiling | GrantsCeiling)
        v
 Evidence and Replay API                        agentic_os.evidence_query.EvidenceQueryService / ReplayController
   1. authenticate           (HTTP layer; the service refuses a missing principal)
   2. authorize              grant evidence.<scope>.<action>   BEFORE anything is resolved
   3. resolve artifact scope ScopeCatalog.load_scope(scope)    only this scope's datasets, hash-verified, confined paths
   4. audit the request      hash-chained receipt (fails closed)
        v
 DuckDB query execution                         one short-lived worker process per query (_worker.py)
   scoped views/tables only, in-memory, enable_external_access=false, lock_configuration=true,
   no extensions, empty env, private scratch cwd, rlimits + RSS watchdog + timeout, OS sandbox where present
        v
 runtime artifact store                         traces, ledger, checkpoints, receipts, context, replay manifests
                                                (read by the parent through ArtifactRoot: O_RDONLY|O_NOFOLLOW, sha256; the worker gets rows, never a path)
```

Replay (operations 3) does not go through SQL. `ReplayController.reconstruct|resume|reexecute` call `MissionRuntime`, whose
execution layer enforces the replay rules (section 7).

## 4. Inventory at `rel/pkg-rc3` (18b95ce)

"Auth" is what protects the entry point in the shipped code. "RC" says what ships in the release candidates.

| Item | DuckDB / evidence use | Caller-supplied SQL or paths | Host file reads | Replay / resume / re-execute | Auth | Ships in RC? |
|---|---|---|---|---|---|---|
| `deploy/evidence-explorer-demo/app.py` | one shared in-memory DuckDB over the synthetic corpus | **SQL string from the HTTP body** (`POST /api/evidence/query`), regex guard only | **yes** (`read_text`, `glob`, ... observed) | none | **none** | **No**: demo only (Dockerfile + compose + deploy.sh; routed by an `echo` ingress hint to demo.redevops.io). Fixed by this branch. |
| `agentic_os/mission/event_backends.py` `DuckDBEventStore` | embedded ledger file | none: fixed parametrised SQL; file path from `MISSION_EVENT_PATH` / ctor (operator config) | only its own ledger file | none (it is the log that replay folds) | n/a (library) | **Library bundled**: desktop sidecar spec collects `duckdb` (`projects,duckdb` extras; lock pins 1.5.5). No shipped route uses it: `projects_api.py` and `local_serve.py` import no mission runtime or event backend. OCI images: `pip install .` / `.[projects]` do not install duckdb (static reading; no image rebuilt here), so the backend raises ImportError there. |
| `mission/factory.py`, `production.py`, `service.py` | choose the backend from `MISSION_EVENT_BACKEND` | none | none | `service.mount` adds `/missions` routers to the control plane | control plane: `AGENTIC_OS_API_KEY` guards only `/up /down /dispatch /approvals/*` (track 1 D-2) | **OCI `redevops-runtime` image is the control plane** and mounts `/missions` (see below) |
| `agentic_os/sources_rag.py` `RagIndexer` | `redevops-rag` index per source: `<db_dir>/<project_id>_<source_id>.duckdb` | retrieval takes a search string (not SQL); ids unvalidated in the filename (ids believed server-generated: UNVERIFIED) | indexes the folder the owner connected (the feature) | none | Projects session gate | **No**: the `rag` extra is excluded from the built set (`BUILT_EXTRAS = projects, duckdb`) |
| `historical_replay.py`, `trend_backtest.py`, `a0_evaluation.py` | none (pure functions over in-memory observation lists: "replay" = leakage-safe backtest) | none | none | no side effects | n/a | library only |
| `mission/bootstrap.py`, `governed_install.py`, `security_monitor.py`, `context.py` | prose mentions; state is a fold over the event log; monitor emits events | none | none | `governed_install` "resumes at the same gate" by fold (no tool call without the gate) | n/a | library |
| `runtime_contracts` `EvidenceStore` seam | `FileEvidenceStore` (JSONL), `InMemoryEvidenceStore`; envelopes carry `content_hash` and `verify_payload()` | none | its own file | none | n/a | used by the explorer demo only (`agentic_os` imports it nowhere else) |
| `MissionRuntime.rehydrate` / `run` / `approve` / `resume` / `re_evaluate` / `run_batch` / `_saga` | event-log fold | none | none | **yes**: `rehydrate` reconstructs (no tool call); `run`/`approve`/`resume` execute under the original gates; cached approvals were honoured by node id + capability only (no evidence/plan binding, no expiry) | in-process | the runtime is in the control plane (OCI) and unified server; **not** in the desktop sidecar's Projects API |
| `mission/api.py` router (`POST /missions`, `/missions/{id}/approve`) | - | `policy_refs` defaults to `["*"]` (noted, not changed here) | none | create+run, approve -> run | control plane key only if configured | OCI control plane (open by default: track 1 D-2) |
| `mission/unified_server.py` (`/api/missions/{id}/approve`) | - | none | none | approve -> run | none | not in compose/chart; desktop slice |
| `agent_gateway` `missions.resume` | - | none | none | `rt.resume` of a paused mission | gateway capability grants (`missions.control`) | optional `mcp` extra, not built |
| `sidekick-devops/monitor.py` | - | none | none | `runtime.run` on missions it creates | none | deploy tree, not an RC artifact |
| `agentic_os/cli.py` | - | none | none | fleet approvals only (`approve`/`reject` of a pending fleet action), no mission resume | control plane key | CLI |
| `world/enrichment.py:213` | none (SQL text for a third-party API) | an unescaped domain in a PDL SQL string | none | none | n/a | noted, out of scope |

**What ships in the RC artifacts.** The desktop sidecar (dmg/deb/msi/snap) bundles the `duckdb` wheel as an embedded ledger
backend, and serves the Projects API behind its launch-code/session gate. No Projects route accepts SQL, a file path to read, or
triggers replay/resume/re-execution (the Projects mission views are sample data; `projects_api.py` states that writes and mission
delegation are not exposed). The OCI `redevops-runtime` image is the control plane: it exposes `/missions` (create+run, approve)
unauthenticated by default (track 1 D-2, separate from D-1), has no DuckDB installed by default, and no route in it accepts SQL or
paths. **The query and file-read defect existed only in the demo.** Release impact: section 10.

## 5. Threat model

Assets: host files and secrets of the process; other scopes' evidence; the integrity of ledgers, checkpoints and manifests; external
systems reachable by tools; the availability of the host.

Actors: anonymous network client (public demo), authenticated user with a narrower scope, a compromised or prompt-injected agent
that can author SQL, an insider with inspect but not replay rights.

| Threat | Control |
|---|---|
| SQL reads host files / lists directories (`read_text`, `read_csv`, `read_parquet`, `read_json`, `read_blob`, `glob`, replacement scans) | worker: `enable_external_access=false`; OS sandbox denies reads outside the interpreter; guard rejects non-allowlisted table functions and unknown tables |
| SQL writes files (`COPY ... TO`, `EXPORT DATABASE`, `ATTACH` a file, temp dir, `CREATE TABLE AS` to a database file) | external access off; `RLIMIT_FSIZE=0`; OS sandbox denies writes outside the scratch dir; artifacts never mounted into the worker |
| Network (`httpfs`, URL reads, `INSTALL`, `postgres_scan`, secrets) | external access off; extension install/load/autoload/community/unsigned disabled; Python network modules blocked in the worker; OS sandbox denies network where available |
| Relax settings (`SET`, `PRAGMA`, `RESET`, `lock_configuration`) | configuration locked after data load; guard rejects non-SELECT |
| Read environment / secrets (`getenv`, `current_setting`) | worker env is empty; `getenv` does not exist in 1.5.5; guard denies both |
| Introspection leaks (`information_schema`, `duckdb_tables()`, `duckdb_views()`, `duckdb_settings()`, view SQL) | the worker contains only the caller's scope (nothing else exists to leak); guard rejects qualified/unknown tables and `duckdb_*`/`pragma_*` |
| Error-message leaks (paths, other scopes) | fixed messages for guard refusals; worker errors keep the first line with absolute paths replaced by `<path>` |
| Multi-statement / prepared-statement smuggling | exactly one statement (parser statement count + type); `PREPARE/EXECUTE/CALL` are not SELECT |
| UDF / macro / view persistence | non-SELECT rejected; and every worker is discarded after one query |
| Cross-scope / cross-tenant access | per-scope authorization before any load; per-query worker built only from that scope; same refusal for unknown and unauthorized |
| Artifact tampering / symlink / `..` escape | `ArtifactRoot`: `O_RDONLY\|O_NOFOLLOW` per component relative to a dir fd, no `..`/absolute/NUL, regular files only, size cap; sha256 recorded at registration and re-checked on every load |
| Resource exhaustion (huge joins, memory, infinite loops, giant results) | wall-clock timeout with process-group kill, DuckDB `memory_limit` + RSS watchdog (+ `RLIMIT_AS` on Linux), `RLIMIT_CPU/FSIZE/NOFILE/CORE`, row/byte/input caps, one thread, no temp spill |
| Replay used as a side-effect backdoor | section 7 |
| Anonymous demo abused | separate profile, synthetic allowlisted scope only, rate limit, short timeouts, no other routes, container hardening |

DuckDB escape vectors exercised by `tests/test_evidence_query_boundary.py` (about 100 statements, each run through the service
**and** through the worker with the guard disabled, and a subset with the guard and the OS sandbox both disabled):
`read_text read_csv read_csv_auto read_parquet read_json read_json_auto read_blob read_ndjson glob` (absolute and `/etc`),
replacement scans (`FROM '/path'`, `.csv`), `COPY TO/FROM`, `EXPORT/IMPORT DATABASE`, `ATTACH` (new file and the canary file,
READ_ONLY), `DETACH`, `INSTALL/FORCE INSTALL/LOAD` (name and path), `SET` of `enable_external_access`, `lock_configuration`,
`temp_directory`, `home_directory`, `allowed_directories`, `autoload_known_extensions`, `PRAGMA` (read and set), `pragma_database_list`,
`RESET`, `getenv`, `current_setting` (home and temp), `duckdb_settings/tables/views/extensions/databases/functions/secrets`,
`information_schema.tables/columns`, `sqlite_master`, URL reads (`http`, `https`, `s3`) against a loopback listener that must
record zero hits, `PREPARE ...; EXECUTE`, `;`-chained statements and comment/unicode-space tricks, `CREATE MACRO/FUNCTION/VIEW/TEMP VIEW/
TABLE/TABLE AS/SEQUENCE/SECRET`, `INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE`, `CALL`, `EXPLAIN`, `DESCRIBE`, `SUMMARIZE`, `SHOW`,
`SET VARIABLE`, `CHECKPOINT`, `USE`, `VACUUM`, `query()`, `query_table()`, cross-catalog names (`memory.main.t`, `system.main.f`),
`sniff_csv`, `parquet_metadata`, `read_xlsx`, `sqlite_scan`, `postgres_scan`, `LATERAL`, CTE, subquery and set-operation placements,
mixed-case and quoted function names, `PIVOT`, `write_log`.

Observed behaviour with the guard disabled (worker only): every host-file, network, install/load, export, attach, settings-change
and secret statement fails with a permission/invalid-input error; the introspection functions and in-memory DDL/DML **succeed
inside the throw-away worker** but see only that scope's tables and are discarded with the process. That is why the guard exists
(clean refusal, no surprise), and why it is not the boundary.

## 6. DuckDB settings and what depends on the DuckDB version

Pinned: `duckdb==1.5.5` (locks `packaging/locks/requirements-{macos,linux,windows}.lock`; the demo Dockerfile now pins the same).
Tests ran on macOS arm64, CPython 3.12.10, duckdb 1.5.5.

| Setting | Value | Notes |
|---|---|---|
| database | `:memory:` | nothing persistent to modify; per-query process |
| `enable_external_access` | `false` | one-way; also restricts `allowed_directories` to the temp dir |
| `lock_configuration` | `true` (after loading data and views) | `SET`/`PRAGMA`/`RESET` of any option then fails |
| `autoinstall_known_extensions`, `autoload_known_extensions` | `false` | |
| `allow_community_extensions`, `allow_unsigned_extensions` | `false` | |
| `threads` | `1` | |
| `memory_limit` | `memory_mb` (default 256 MB) | |
| `max_temp_directory_size` | `0B` | no spill; `temp_directory` is a path inside the private scratch dir |
| `home_directory`, `secret_directory` | empty | |

Version-dependent protections (re-verify when the pin moves; the test-suite does):
`enable_external_access=false` covering every file-reading function and `COPY/EXPORT/ATTACH` (the sandbox backstops it);
`lock_configuration` semantics; the existence/absence of `getenv`; `json_serialize_sql` and `extract_statements` as the parser
interface (a syntax the serializer cannot handle is rejected, i.e. fail-closed); the exact error texts; statically linked
extensions (json, parquet, icu) remaining usable offline. Python-side: the parent passes data as JSON, so DuckDB's Python
replacement scans and UDF registration are never reachable from SQL.

## 7. Inspection versus replay: privileges and enforcement

Permission names are ordinary grants on the repo's permission plane: resource kind `tool`, action `read`, resource
`evidence.<scope>.<action>`. A dotted prefix covers longer names (`evidence.alpha` covers all four actions of scope `alpha`;
`evidence` covers every scope); disjoint action names keep `inspect` from implying the others. Scope ids may not contain dots.

| Operation | Permission | Side effects | What is enforced, and where |
|---|---|---|---|
| Inspect historical execution | `evidence.<scope>.inspect` | none | `EvidenceQueryService.query`: authorize, scoped worker, audit |
| Reconstruct a historical state | `evidence.<scope>.reconstruct` | **none (asserted)** | `ReplayController.reconstruct` -> `MissionRuntime.rehydrate` (fingerprint, ContextEpoch and signature must reproduce; fails closed). The mission is marked reconstructed: read-only |
| Resume a failed/paused workflow | `evidence.<scope>.resume` | only tool calls the original gates allow | `MissionRuntime.grant_resume` (permission plane, logged `ReplayAuthorized`/`ReplayRefused`); a short-lived grant (default 15 min); the original approval and policy gates run as in the first execution; cached approvals count only if bound and unexpired |
| Re-execute external operations (re-plan and run) | `evidence.<scope>.reexecute` | tool calls through the new revision's gates | `grant_resume(action="reexecute")` is required by `re_evaluate`; node ids change with the revision so no old approval matches |

**A historical approval receipt is evidence, never authority.** `ApprovalGranted` now records
`binding = sha256(mission, plan fingerprint, ContextEpoch, intent content hash, evidence refs, node, capability)`,
`granted_at` and `expires_at` (default TTL 24 h). On a reconstructed mission only approvals whose binding equals the **current**
binding and which are unexpired are honoured; everything else is logged once as `ApprovalNotHonoured` (`unbound`,
`binding_mismatch`, `expired`) and the node goes back to its gate. Receipts written before this change have no binding and
therefore cannot authorize a replayed side effect. (In a never-reconstructed, same-process run behaviour is unchanged.)

**Enforcement is in the execution layer, not only on routes.** `MissionRuntime._require_execution_authority` is called at the top
of `run`, `approve`, `resume`, `run_batch` (all-or-nothing), `change_policy` (retroactive revoke) and `re_evaluate` (needs the
re-execute grant), and, last line of defence, in `_executor_for`, the single function every tool invocation passes through (serial,
concurrent waves, saga compensation: `_saga` has no check of its own because it reaches tools only through `_executor_for`; a mutant
that removed a separate `_saga` check survived, which is how that check was found to be redundant and deleted).

Entry points enumerated at `rel/pkg-rc3`, with the test that drives each against a recording fake tool (zero calls, no events
written without a valid grant and bound approval):

| Entry point | Reaches execution via | Test |
|---|---|---|
| Python API `rt.run`, `rt.approve` (approve and reject), `rt.resume`, `rt.run_batch`, `rt.re_evaluate`, `rt.change_policy` | direct | `test_entry_*` in `tests/test_evidence_replay_authorization.py` |
| saga / compensation, executor choke point | `_saga`, `_executor_for` | `test_entry_saga_compensation_and_executor_choke_point` |
| HTTP `POST /missions/{id}/approve` (`mission/api.py`, mounted in the control plane) | `approve` | `test_entry_http_mission_router` (403) |
| HTTP `POST /api/missions/{id}/approve` (`mission/unified_server.py`) | `approve` | `test_entry_http_unified_server` (403) |
| agent gateway / MCP tool `missions.resume` | `rt.resume` | `test_entry_agent_gateway_missions_resume` |
| scheduler / batch (`run_batch`), background monitor (`sidekick-devops`: only runs missions it created) | `run` | `test_entry_run_batch_*`; inventory test |
| CLI `agentic-os approve/reject` | fleet approvals only, no mission runtime | `test_entry_cli_has_no_mission_resume_surface` |
| any new call site of the verbs | the same layer | `test_entry_point_inventory_is_pinned` fails until the new site is reviewed |

Re-execution requires, in order: the `reexecute` permission, a reconstructed (hash-verified) mission, the original workflow's
gates for every node, an unexpired approval bound to the same evidence/plan/intent for each gated node, and an audit receipt
(`ReplayAuthorized` in the mission log, `replay.reexecute` in the audit sink with `side_effects` counted from dispatch events).
Idempotency and exactly-once remain the executor's job (deterministic node idempotency keys; the operator client dedupes); a restarted
process starts with an empty dedupe cache, so non-idempotent tools must not rely on it across a restart (open risk, section 11).

## 8. Deployment profiles of the evidence explorer

| | `app.py` authenticated (normal) | `public_app.py` public synthetic |
|---|---|---|
| Image target | `--target authenticated` | `--target public-synthetic` (its own copy of the code; no `app.py`) |
| Start | **refuses** without `EVIDENCE_EXPLORER_TOKEN` (>= 24 chars); no env var disables this | no token; never started implicitly. **Owner decision to deploy** |
| Principal / permission | owner token -> `evidence.<scope>.inspect` grants in the encrypted grant store | fixed anonymous principal with the single static grant `evidence.synthetic.inspect` |
| Scopes | `synthetic` + one per synthetic tenant | `synthetic` only; the request cannot name another scope |
| Routes | `/healthz`, `/evidence`, `/api/evidence/{summary,query,canned,scopes}` | the same minus `/scopes` |
| Replay / approvals / operational routes | absent | absent (route table asserted; static scan of the code) |
| Limits | 10 s, 256 MB, 500 rows | 5 s, 128 MB, 200 rows, 500 kB result, 2 kB SQL, per-client 20/min and global 120/min |
| Audit | hash-chained JSONL (`EVIDENCE_AUDIT_PATH`) | same |
| Container | read-only rootfs, caps dropped, no-new-privileges, tmpfs only | same plus mem/pids/cpu limits; **no volumes, no Docker socket, no secrets** in the compose file |

`/api/evidence/summary` returns counts only (the old `path` field is gone). `/docs`, `/openapi.json` and `/redoc` are off.
Network for the public worker: the OS sandbox denies it where present (macOS Seatbelt verified; Linux `bwrap` / `unshare --net`
implemented, **not verified on a Linux host**); everywhere the Python-level block and DuckDB's disabled external access apply, and
the tests assert no socket can be created in the worker. For a hosted deployment also run the container with no route to internal
networks (egress policy), which is outside this repository. `EVIDENCE_REQUIRE_OS_SANDBOX=1` makes the public profile refuse to
answer when no OS sandbox is usable.

## 9. Tests (all synthetic, loopback only)

* `tests/test_evidence_query_boundary.py`: expressive SQL, authn/authz and indistinguishable refusals, the vector table above
  (service, guard-off worker, guard-and-sandbox-off worker), guard unit cases, cross-scope isolation, worker environment/cwd/socket/
  OS-sandbox probes, resource limits, audit receipts and chain, the real DuckDB mission ledger as a scoped dataset.
* `tests/test_evidence_query_artifacts.py`: confinement (`..`, absolute, symlink file/dir/swapped, in-root symlink), integrity,
  byte/inode/mtime/mode-identical artifacts after every write route (guard on and off), read-only open flags.
* `tests/test_evidence_replay_authorization.py`: reconstruction without side effects, every entry point, bound/unbound/expired
  approvals, permission separation, controller receipts, lineage tamper detection.
* `tests/test_evidence_explorer_profiles.py`: both profiles in-process and as real uvicorn processes (negative security).
* Existing: `test_event_backends`, `test_replay_integrity`, `test_benchmark_a_replay`, `test_event_log_reproducibility`,
  `test_mission*`, `test_evidence_identity`, `test_unified_two_domain`, `test_agent_gateway_missions`. Two existing tests
  (`test_restart_resume_from_event_log`, `test_unified_two_domain`) and `mission/demo.py` now call `grant_resume` after
  `rehydrate`: the intended behaviour change.

Mutation check of the boundary (throw-away worktree, runner kept with the private reports): 52 mutants including the fail-open
variants (external access on, configuration unlocked, scope filter skipped, authorization/authentication skipped, guard removed,
worker environment leaked, symlink/`..` checks removed, integrity check off, OS-sandbox rules removed, timeout/row/byte/memory caps
removed, audit failure swallowed, each replay check removed, approval binding/expiry ignored, artifacts opened read-write, public
profile limiter/scope override removed). All 52 are killed by the tests at the final commit. Mutation also found three problems that
were fixed: a streaming row-cap test was missing, a view-validation test was missing, and a `_saga` check was redundant (deleted).
Removing the statement guard fails only the tests that expect a guard refusal; every host-file/network/extension vector still fails
in the worker.

Linux: the four new test files were also run in a disposable `python:3.12-slim` container (uid 1000, source mounted read-only,
duckdb 1.5.5): 149 passed, 2 skipped (the two OS-sandbox tests; no usable `bwrap`/`unshare` inside Docker Desktop, so the backend
probe falls back to `none`). The DuckDB-layer, guard, artifact, replay and profile tests therefore pass on Linux; the Linux OS
sandbox itself is unverified.

## 10. AGPL core versus Enterprise

AGPL core (built here): secure single-owner artifact inspection, audit, replay, integrity verification and governance. One owner
token; grants on the ordinary permission plane; scopes registered by the operator; per-query sandbox; audit chain; replay rules.

Enterprise (not built; the seam): multi-user identity (SSO/OIDC, per-user tokens, revocation) behind `Authenticator`; tenant
isolation and `overlays.set_identity_provider` / `set_tenancy_provider` replacing `IdentityCeiling`; row, column and document-level
authorization (`PermissionsPlane.authorize` already returns a `row_filter` and `masked_columns`; the catalog's column projection and
`ViewSpec` are where they would be applied **before** the worker is built); permission-aware retrieval; separation-of-duties for
approvers. Everything keys off `evidence.<scope>.<action>`, so no data migration is needed.

## 11. Residual risk and what is not verified

* A DuckDB parser or engine escape in a future version: contained by the worker process (empty env, no path, no network where the
  OS sandbox exists, write-denied, rlimits, discarded after one query). Not contained: a kernel-level escape from the worker.
* Without an OS sandbox (Linux hosts lacking `bwrap`/userns, containers that forbid it) the worker has no kernel-enforced file or
  network denial; DuckDB's own switches, the empty environment and the container hardening remain. Verified only on macOS here.
* The parent reads artifacts into memory (size-capped); very large ledgers need a cap/streaming policy (`max_input_rows`).
* Resume/re-execute across a restart loses the executor's in-memory idempotency cache; idempotency must be provided by the tool.
* `mission/api.py` still defaults `policy_refs` to `["*"]` and the control plane is open by default (track 1 D-2): separate fixes.
* The old approvals without binding are never honoured on replay (by design); a deployment replaying very old logs will see
  re-gating.
* Timing: unknown-scope and unauthorized refusals take the same code path, but no timing-equalisation test was run.

## On the converged trunk (origin/main + this branch)

* **Unattended resume is never a default.** `MissionRuntime(service_resume=None)` refuses to run any rehydrated mission. A process that
  must resume by itself (boot restore with approvals over HTTP, the goal loop, the revenue runner, the billing hand-off) is configured
  with an explicit `ServiceResume(principal, ceiling)`; its principal must hold `evidence.<tenant>.resume` on the permission plane, every use
  is on the log (`ReplayAuthorized` with the actor, or `ReplayRefused`), it never satisfies `reexecute`, and it does not stand in for an
  approval. Callers that rehydrate to go on running use `rehydrate_for_service`; without a configuration the mission is restored read-only
  and the goal loop waits for a human (`resume_not_authorized`). Consequence: after a restart a parked mission cannot be approved over
  HTTP (403) unless the process was started with a `service_resume` (`service.mount(app, service_resume=...)`).
* **Scope = tenant id.** `grant_resume` refuses (`ReplayRefused`, reason `scope_tenant_mismatch`) a scope that is not the mission's tenant.
* **Two different time limits.** The request TTL (`AGENTIC_OS_APPROVAL_TTL_S`, bounds how long an unanswered approval request stays answerable)
  is unchanged. The grant lifetime on replay is `approval_grant_ttl_s` (default 24 h; `approval_ttl_s` is a deprecated alias) and is what
  `expires_at` in `ApprovalGranted` carries.
* **`ApprovalGranted` payload** is a superset: `node_id, edit, approver, inputs_sha256, capability` (main) plus `binding, granted_at,
  expires_at`. `inputs_sha256` is what the approver was shown; `binding` is what plan/evidence/intent the approval was given for.
  Events written before the binding existed stay readable (they are "unbound" on a reconstructed mission, never an error).

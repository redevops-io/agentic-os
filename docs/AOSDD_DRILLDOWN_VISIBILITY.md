# AOS-DD missions in the Projects drill-down: tenant visibility

The Projects drill-down (`docs/PROJECTS_DRILLDOWN_MOUNT.md`) shows a mission only to a principal of the tenant the mission was recorded under (or the all-tenant
admin). Behind the desktop's local capability gate the principal is `local-owner`, and its tenant is `agentic_os.overlays.DEFAULT_TENANT`, `default`.
An AOS-DD mission is recorded under the `tenant` of its configuration. If that is not `default`, the mission is real and its receipt and ledger are complete, but
the drill-down answers "No recorded history to show". The drill-down rule is unchanged; the AOS-DD side is configured to match.

Keys:

| Where | Key | Value |
|---|---|---|
| AOS-DD config (`aosdd.config/1`, named by `AOSDD_CONFIG_FILE` or `--config`) | `tenant` | `default` |
| AOS-DD operator tokens (`aosdd.operators/1`) | `requester_tokens`, `approver_tokens` | keyed by `default` (the role principals are `requester:default`, `approver:default`) |
| Projects app environment | `PROJECTS_DRILLDOWN_EVENT_PATH` | the AOS-DD `events.jsonl` (a copy for a read-only view) |
| | `PROJECTS_DRILLDOWN_PROJECT` | the project id (permission scope), e.g. `customer-ops` |
| | `PROJECTS_DRILLDOWN_GRANTS_DIR`, `PROJECTS_DRILLDOWN_AUDIT_PATH` | the grant store and the audit log |
| | `PROJECTS_DRILLDOWN_AUTHORITY_DB` | optional: the AOS-DD `authority.db` (only reported by `GET /api/ui-config`) |
| Operator command | `agentic-os permissions allow-drilldown --project <project> --audit <audit> --dir <grants>` | gives `local-owner` `evidence.<project>.inspect` |

With any other tenant AOS-DD logs a WARNING when it starts (logger `agentic_os.integrations.aosdd`; the CLI prints `WARNING: ...` on stderr) and
`aosdd ... --config <config> verify` adds the same text under `warnings` (the report stays `ok`). It is never an error.

What the drill-down shows of an AOS-DD log today: the generic mission events are understood; the AOS-DD events (`Aosdd*`) and several base events of operator
interest (`StaleResultRecorded`, `ReconciliationRequired`, `Compensation*`) appear as "other" rows with only the event name, and nothing in the views flags an uncertain
step; the AOS-DD receipt (`aosdd ... reconstruct`) is where uncertainty, residual effects and the read-back are reported.

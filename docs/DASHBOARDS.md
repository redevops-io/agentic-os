# Dashboards

Analytical dashboards inside Projects, computed by the runtime over the systems a deployment already has connected.
Source: `agentic_os/dashboards/`. The Projects UI (redevops-projects) renders the projections verbatim under
**Work → Dashboards**.

## What is there

| Dashboard | Priority | Reads | Offered when |
|---|---|---|---|
| Portfolio health | P0 | missions, attention, QA, Edge Sentinel | any of them answers |
| QA and release readiness | P0 | redevops-qa runs, reported test results | either is present |
| Security and remediation | P0 | Edge Sentinel | it is registered |
| Missions and workflows | P0 | missions, attention | always |
| Revenue and sales | P1 | Twenty, Control Tower cards | either is enabled |
| Financial health | P1 | ERPNext, Lago | enabled |
| Market and conversion | P1 | Umami, Twenty | enabled |
| Customer support | P1 | Chatwoot | enabled |
| Suppliers and procurement | P1 | ERPNext | enabled |
| Marketing and social | P1 | Postiz, Listmonk | enabled |
| AI usage and cost | P1 | Mission Runtime step records (bound runtime, or a Mission Plane over HTTP) | a runtime is readable |
| Infrastructure and deployments | P1 | runtime health, product readiness, deployment inspection | always |
| Knowledge and data quality | P2 | context sources | a source is connected |

A dashboard whose application is not connected is not offered; the catalog says what would add it.

## Rules the numbers follow

* **A number equals its records.** Every metric names a drill key; `GET /api/dashboards/{id}/drill?key=…` lists exactly the
  records it counted, each with the owning system's own reference and a link into that system.
* **Unknown is not zero.** When a source does not answer, or the caller may not read it, the metric's `value` is `null`
  with a reason (`source_unavailable`, `unauthorized`, `not_connected`, `no_data`, `no_traceability`).
* **Ratios are ratios of sums.** History stores numerator and denominator per project; a portfolio figure and its trend
  are computed over the projects the caller may see, never by averaging percentages.
* **Money is never added across currencies.**
* **Freshness is shown.** Each source reports its state and when it was read; a read older than 15 minutes is `stale`.

## QA coverage

The unit is one test of one suite on one platform of one project. The planned inventory is what the catalog declares
plus what reported a result inside `qa.inventory_days`. The latest run in the selected range decides each test:
`passed`, `failed`, `unsupported` (the platform cannot run it), `blocked` (in the run, no verdict) or `unexecuted`
(planned, absent from the latest run). Coverage is `(passed + failed) / planned`; the pass rate is `passed / planned`.
Neither is computed from the executed tests alone.

redevops-qa run records carry a target and a profile, not a project, suite or requirement. Those identities live in the
dashboards file (`qa.suites`, `qa.requirements`): a suite maps targets/profiles to a project, and a test to the
requirements it covers. A requirement counts as covered only when every linked test passed; one with no test is
`untested` and counts against coverage.

A CI lane that does not run through redevops-qa reports its results:

```bash
DASHBOARDS_SERVICE_TOKEN=… python -m agentic_os.dashboards.report_junit results.xml \
  --url https://projects.example --project storefront --suite checkout --platform chromium --run-id "$RUN_ID"
```

The token is a service identity in `AGENTIC_OS_SERVICE_TOKENS` holding `dashboards.ingest_qa`; a token that does not
verify is refused and never falls back to another identity. `--only` / `--exclude` (regular expressions on the test id)
report one file once per platform, for a parametrised browser matrix. A skipped test is `unsupported` when its message
says it cannot run or does not apply there, otherwise `blocked`.

`redevops-io/agentic-tests` qualifies the dashboards themselves this way (`qualification/test_projects_dashboards.py`: the
UI Audit, the Playwright UI Test Agent's persona journeys, and accuracy checks against each application's own API), and
its JUnit file is reported into the same QA dashboard.

## Configuration

`AGENTIC_OS_DASHBOARDS` names a YAML or JSON file; unset is valid (one project, the always-on sources). The annotated
example is the docstring of `agentic_os/dashboards/config.py`. Business sources are read only when listed under
`sources`, with the connection each client already reads from the environment (`TWENTY_BASE_URL`/`TWENTY_API_KEY`,
`ERPNEXT_URL`/`ERPNEXT_API_KEY`/`ERPNEXT_API_SECRET`, `CHATWOOT_API_URL`/`CHATWOOT_API_TOKEN`,
`LAGO_API_URL`/`LAGO_API_KEY`, `UMAMI_URL`/`UMAMI_USERNAME`/`UMAMI_PASSWORD`, `POSTIZ_API_URL`/`POSTIZ_API_KEY`,
`LISTMONK_URL`/`LISTMONK_API_USER`/`LISTMONK_API_TOKEN`, `METABASE_API_URL`/`METABASE_ADMIN_EMAIL`/`METABASE_ADMIN_PASSWORD`).

State (metric history, saved views, reported results) is one sqlite file under `data_dir`, or `$AOS_DATA_DIR/dashboards`;
with neither it is kept in memory and the UI says history restarts with the process.

`sources.mission_plane` reads a deployed Mission Plane over its own API (`MISSION_PLANE_URL`, optional
`MISSION_PLANE_TOKEN`) for the Missions and AI-usage dashboards when no runtime is bound in-process. Step records carry
spend and tokens only if the runtime writes them; when it does not, those tiles say "not known" and the planner's
projection is shown beside them, labelled as a projection.

Metabase is optional. With `sources.metabase` the operator names saved questions (`cards: {revenue_by_month: 12}`) that
Metabase itself runs, and dashboards to link to (`dashboards: [{title, id, for: [revenue]}]`, `public_url`). Projects
sends no SQL of its own, and nothing here is required on a desktop or WASM installation.

## Access

No new authority. The caller is the principal Projects already resolves; a tenant bearer sees what
`AGENTIC_OS_TENANT_LIMITS` grants (`dashboards:<source>`, `project:<id>`, or `*`); an Enterprise overlay may answer
`provider.dashboard_access(principal)` instead; an `access` section in the dashboards file names what an anonymous caller
and each principal may see, and with it present anything not granted is refused. An aggregate covers only records the
caller may read, and a caller scoped to some projects is told the totals are partial.

Write routes (`PUT`/`DELETE /api/dashboards/views`, `POST /api/dashboards/{id}/track`, `POST /api/dashboards/qa/results`)
carry policies in `agentic_os/projects_route_policy.py`. Saved views belong to the signed-in person.

A deployment that enables business sources should not be reachable anonymously at all. `PROJECTS_OPERATOR_GATE=1`
(`agentic_os/projects/operator_gate.py`) puts a sign-in in front of the whole app: the operator's Basic credential or a
control-plane bearer on every request, 503 when none is configured. A public evaluation deployment keeps the gate off and
enables no business source.

## From a finding to an action

A drill-down returns actions. None executes anything by itself: *Ask Sidekick* answers from the evidence behind the
number; *Track in Today* adds a note with the record references to the intake; *Review in Today* opens the approval;
*Escalate to a security case* and *Request a re-test* call the registered product's existing human-only routes;
*Plan a fix as a Mission* hands the finding to Sidekick, which creates a Mission when a runtime is bound.

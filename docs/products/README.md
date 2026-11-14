# Products in Projects and Sidekick

Edge Sentinel and redevops-qa are separate, independently deployable applications with their own HTTP API and their own persisted records. The `agentic_os.products` package lets the Projects server and Sidekick show and drive those same records. It adds no store, no engine and no authentication system: every row Projects shows is read from the product's own API and carries the product's own ids (`case-…`, `act-…`, `rcpt-…`, `r…Z-…`), so the standalone UI and Projects always show the same records.

With `AGENTIC_OS_PRODUCTS` unset nothing in the Projects server changes.

## Register a product

1. Give each product a service token pair and a shared approval secret, as files (never inline, never in an image or a URL):

   | File | Used for |
   |---|---|
   | `service_token_file` | reads and staging proposals (`X-Control-Token`, or `Authorization: Bearer` for `scheme: bearer`) |
   | `decision_token_file` | optional second token for a person's decision or execute click; falls back to the service token. Edge Sentinel accepts its service credential plus the signed approval token on those routes |
   | `approval_secret_file` | `AGENTIC_OS_APPROVAL_SECRET`, shared with the product, signs the single-use approval token |

   The pair is dedicated per product: it is not the runtime admin token, so a leaked product token cannot call the runtime.
2. Copy [`products.example.yaml`](products.example.yaml), edit it, and start the Projects server with `AGENTIC_OS_PRODUCTS=/path/to/products.yaml`.
3. Check `GET /api/products`: each product shows its live health (from the product's public readiness probe), version, features and the reasons anything is unavailable.

The registry is operator configuration. A URL in it is never a request value; the client refuses redirects, caps responses at 2 MiB, requires https (plain http only to loopback or a name listed in `allow_private_http`), and never forwards the browser's credentials. A bad entry is skipped and logged; it never half-configures a product.

Where a product is not configured, not reachable, or gated, Projects shows an explicit unavailable row (Apps, Missions, Attention, Activity) with the reason and the remedy, never a fabricated mission.

## What appears in Projects

| View | Content |
|---|---|
| Apps | one row per product: health, capabilities, "open in product" link, why-unavailable steps |
| Missions | one mission per Edge Sentinel proposed action (observe, propose, approval, execute, read-back, from the product's own records) and per QA run |
| Attention | approvals waiting for a person, failed read-backs, unavailable products |
| Activity | the product's change feed |
| Today | an Edge Sentinel proposal is a Today item whose approve button carries the decision (see below) |
| Sidekick | typed tools, below |

By default product rows replace the sample demo rows in Missions, Attention and Activity; set `keep_sample_rows: true` to keep both.

## Sidekick tools

Tools are `SidekickCapability` descriptors in the domains `security` and `quality`, each with a closed JSON Schema (`additionalProperties: false`, validated before any HTTP call; see `agentic_os/products/schemas.py`). A tool never accepts a path, URL, command, plan body or credential.

| Tool | Class | What it does |
|---|---|---|
| `security.sentinel_posture` | read | active bans, alerts by severity, CrowdSec connection, dry-run/demo mode |
| `security.sentinel_list_cases`, `sentinel_get_case` | read | cases and one case with its evidence, actions, decisions, receipts |
| `security.sentinel_list_actions` | read | the approval queue and execution history |
| `security.sentinel_propose_ban`, `sentinel_propose_unban` | stage | creates a proposal that awaits a person; nothing is blocked; returns the approval card |
| `security.sentinel_request_decision` | present | shows the approval card of an existing proposal; makes no decision |
| `security.sentinel_read_back` | read | the post-action verification the product persisted |
| `quality.qa_*` (info, targets, profiles, estimate, start/cancel run, run, events, evidence, verify) | read, bounded write | redevops-qa, gated, see below |

Decide and execute are never tools. A message such as "approve act-…" is answered with the approval card and an explanation of who decides. A tool result never carries `executed: true` for a consequential action. A missing input becomes a question, not a guess ("Which IP address?").

## The approval flow

1. A CrowdSec alert becomes a case with a proposed action in Edge Sentinel, or Sidekick/the product UI stages one. The action is `AWAITING_APPROVAL`.
2. Projects shows it in Attention and Today with its consequence ("dry run: nothing will be blocked" or the live effect) and the action digest.
3. A person approves in Projects. Projects re-reads the action from the product, refuses a changed digest (`409 stale_approval`), then mints an approval token with `agentic_os.mission.approval` (HMAC, bound to the operator, the capability and the sha256 of `{case_id, request_id, approved, digest}`, 120 s, single use) whose marker is `{approved, by: "projects:<principal>", via: "projects"}`, and sends it with the decision token.
4. The product verifies the token and its replay guard, maps `by` to a user linked to that Projects principal (`actor_links`) and records the decision with that user as actor and `via: projects`. An unlinked principal is refused (`403 actor_not_linked`).
5. Execution is a second, separate human step, bound to a different digest so one click can never decide and execute. The product executes through its own mandatory approval and then re-reads the edge; the result (`verified`, `unverified`, `failed`, `dry_run`) is the product's persisted record, and Projects only displays it. While blocking is not enabled in the product (`SENTINEL_BLOCK_ENABLED` unset) an execution is a dry run: the product keeps the action `APPROVED` with a `DRY_RUN` receipt, and Projects shows it as a settled "Dry run" and offers no second execute until blocking is enabled. A ban the product's own guard refuses (reserved address space, protected addresses) is recorded by the product as `FAILED` with the reason, and Projects shows that failure.

The principal is never taken from a request body. It is the desktop session (`local-owner`), the operator's HTTP Basic credential that the shipped Projects server asks the browser for (`operator`), a control credential in `Authorization: Bearer` (`admin`, `approver:<tenant>`; a requester is refused while approver credentials exist), or the `principal.single_owner` the operator declared for a Projects only one person can reach. Link the principal that applies in the product once (for Edge Sentinel: `POST /api/v1/actor-links {"external_actor": "projects:operator", "user": "<name>"}`). Without a principal the decision is refused (401) and nothing is sent. The product's own UI always remains a way to decide.

## Gates

The redevops-qa tools, the QA projection and the QA catalog entry are enabled only when P2-1 acceptance criteria AC-1 to AC-12 pass in CI on the loopback Edge Sentinel fixture target (owner decision of 2026-11-11). The gate is the code constant `QA_EXTERNAL_TARGET_ACCEPTANCE_MET` in `agentic_os/products/gates.py`: no environment variable, registry key or request opens it. While it is false, every quality surface reports `unavailable: external-target acceptance not met`, the product is never called, and a test asserts `modules.yaml` lists `redevops-qa` only when the gate is open. The redevops-qa standalone UI for the bundled fixture target is not gated.

## Tier A and Tier B

Tier A is everything above: server-side projections, the Today approval control, Sidekick replies and app-directory links. It works with the shipped Projects UI bundles without a UI change, including the desktop bundle. Its limits come from that UI: the Today control is worded for messages ("Approve & send"), Missions has no decide control, and Sidekick shows text and navigation actions only. Tier B (purpose-built case and run pages with evidence tables and live progress inside Projects) needs new views in the public `redevops-projects` repository and is not claimed for the desktop bundle, whose UI source is unpublished.

## Release line: `/api/ui-config`

On the Projects line that serves `GET /api/ui-config`, the response carries an additive `products` section (`configured`, `basis: product_records`, the views fed) next to the existing `data_source` and `projections.basis`. The shipped desktop UI labels its data "Sample" at the top level unless the drill-down store is mounted; product rows are the products' own records and carry them in `source_refs`, but that chip is the UI's, not the products package's.

## Desktop bundle

`agentic_os/products` is part of the frozen sidecar (`collect_submodules("agentic_os")`; no data files, no new dependency). The product servers are not inside the sidecar: it reaches them as external URLs (a loopback Docker Compose or a remote deployment).

## Application directory

On the Projects line that ships `agentic_os/app_directory.py`, [`app-directory.example.yaml`](app-directory.example.yaml) adds the "Open app" and "Open record" links (path-based: `/ui/cases/<id>`, `/ui/actions/<id>`).

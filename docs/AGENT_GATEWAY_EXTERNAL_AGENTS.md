# Connecting an external agent to a Projects deployment

The Governed Agent Gateway gives an external agent (an MCP client, a script) a **read-only** slice of a deployment:
projects, missions, sources and dashboards. Every call runs one governed path: permissions, egress rules, audit. Writes, mission
delegation and the action sandbox are not exposed.

## Give each agent its own token

    python -m agentic_os.agent_gateway.agent_tokens issue  agents.json --id claude-laptop --grant projects.read --days 30
    python -m agentic_os.agent_gateway.agent_tokens list   agents.json
    python -m agentic_os.agent_gateway.agent_tokens revoke agents.json --id claude-laptop

`issue` prints the token once, on standard output; the file keeps only its SHA-256. Grants are `projects.read`,
`missions.read`, `sources.read` and, only when you name them, `dashboards.read` and the product grants (see below). Every token expires (at most 366 days). An id is never reused, so the audit stays
unambiguous. Start the Projects API with:

| Setting | Meaning |
|---|---|
| `AGENT_GATEWAY_AGENTS_FILE` | the file above; setting it mounts the gateway. Read again whenever it changes: a revocation applies to the next call |
| `AGENT_GATEWAY_AUDIT_PATH` | append every call to a hash-chained log (without it the audit is in memory) |
| `AGENT_GATEWAY_MCP=1` | also serve a real MCP server (needs `pip install 'agentic-os[mcp]'`; asking for it without the extra stops startup) |
| `AGENT_GATEWAY_MCP_ALLOWED_HOSTS` | comma-separated host names the MCP server is reached by, when it is not reached as localhost |

A file that cannot be read or does not validate authenticates **nobody**; one invalid entry is ignored and logged.

## Or let the agent ask: OAuth 2.1

With two settings an agent connects itself, and a person approves it. Nobody copies a token around.

| Setting | Meaning |
|---|---|
| `AGENTIC_OS_OAUTH_DB` | a private file (SQLite) where connections are kept; they survive restarts. Only hashes of codes and tokens are stored |
| `AGENTIC_OS_OAUTH_ISSUER` | the https URL this deployment is reached by, including its base path, for example `https://apps.example.com/projects` (http only for localhost) |

Optional: `AGENTIC_OS_OAUTH_ACCESS_TTL_S` (access token, default 900, 60 to 3600) and
`AGENTIC_OS_OAUTH_CONNECTION_TTL_S` (when a person must approve again, default 90 days, 60 seconds to 365 days).

Consent also needs `AGENTIC_OS_APPROVAL_SECRET` (it signs the consent ticket) and a way for a person to sign in.

1. An agent that calls the gateway without a token gets `401` with
   `WWW-Authenticate: Bearer resource_metadata="<issuer>/.well-known/oauth-protected-resource"`.
2. It registers itself (`POST /oauth/register`; public clients; redirect URIs must be https, or http to localhost) and
   sends the person to `/oauth/authorize` with PKCE (S256, required).
3. The signed-in person sees what it asks for, ticks what it gets (`dashboards.read` starts **unticked**, and is not
   offered where that person's tenant is granted no dashboard source), and allows or refuses. The agent's tenant is that person's tenant.
4. Access tokens last 15 minutes; refresh tokens rotate. A refresh token used twice ends the whole connection. A
   connection ends after 90 days whatever happens.
5. `GET /oauth/connections` lists connections; `POST /oauth/connections/{id}/revoke` (JSON) ends one at once. A tenant
   approver sees and ends only their tenant's. The operator is the deployment's owner and sees and ends every tenant's.

Each connection is one agent identity (`agent:conn-…`) across refreshes and restarts, named in the audit, limited to 60
calls a minute, scoped to its tenant (below) and, under the production profile, bounded by its own authority record
(`AGENTIC_OS_AUTHORITY_DB`). Reads only, like every agent here.

Only the five discovery, registration, token and revocation routes work without a sign-in. When the issuer has a path,
a client that only looks for discovery documents at the host's root (`/.well-known/oauth-authorization-server/<path>`)
needs that path routed to this app as well.

**Tested with real clients** over a loopback deployment, with consent given in a real browser: the official MCP Python SDK
2.3.0 (discovery, registration, consent, PKCE, typed tool calls, server restart, access-token and connection expiry,
revocation of a live session, two tenants). One client behaviour to know about: that SDK does not store when its access
token expires, so a NEW client process holding an expired one asks the person to approve again instead of refreshing
(the server accepts the refresh token). A longer `AGENTIC_OS_OAUTH_ACCESS_TTL_S` makes that rarer. Not tested: Claude
Code, Claude Desktop, MCP Inspector, a base-path issuer behind a proxy.

The official TypeScript SDK (1.32.2) was run separately over the same server (37 of 37 checks): unlike the Python SDK,
a new process with an expired access token refreshes with its refresh token and carries on.

**Consent in a browser.** A person following a client's link without a credential is offered the approval-page sign-in
(`/approvals/signin`) and brought back to the same consent request. Behind the operator gate with an operator password,
the browser still asks for that password first, and the page behind the prompt links to the sign-in; so a tenant approver, whose only credential is a
token a browser cannot send by itself, can consent. A request that carries `Authorization` is judged by that alone. An
answer that rests on the session must carry this deployment's `Origin`, and the consent ticket is single use and bound to
the person who saw the page. A deployment that declares a single owner treats whoever reaches it as that owner, as
before.

## Economic intelligence and reconciliation

Offered where ERPNext is connected. **Read-only.** Every tool needs `economics.read`, **and** the financial-data grant on
the agent and its tenant, **and** the tenant's `app:erpnext`.

| Capability | What it returns |
|---|---|
| `economics.discrepancies` | payments whose amount differs from the bank statement under the same reference, marked **ambiguous** where no single payment can be named (several payments or bank lines, different parties, or a withdrawal under the reference); payments already cancelled and entered again |
| `economics.explain_adjustment` | discounts, cancellation, credit notes and payment differences of one invoice; any approval claim in a comment, marked unverified |
| `economics.evidence` | messages and CRM notes that refer to one invoice or customer, as references with excerpts |

**A difference is not by itself an error.** A bank fee, a currency exchange, a partial settlement, timing or how a receipt
is allocated can each explain it, so these tools propose no correction.

**Evidence follows its source.** A conversation needs `support.read` (which covers its private notes, as
`support.get_conversation` does) and the tenant's `app:chatwoot`; a CRM note needs `crm.records.read` and `app:twenty`. A
source the tenant is not granted is not read. A source the agent was not granted is reported only as a count of withheld
items: no id, no excerpt, and they are withheld before anything is correlated. A message is tied to a customer only by an
e-mail address or website both systems hold, never by a name; the answer says how many could not be tied, and why.

## Arguments

Every call is checked against the capability's published input schema, on REST and MCP alike, after the caller's
authority and before rate limits: a malformed call reaches no handler and spends nothing. Required inputs and types are
always checked; an input the schema does not name is refused where the schema says `additionalProperties: false`, as
every capability this gateway offers does (`idempotency_key` is always accepted). The answer is `{"status": "error", "error": "invalid_arguments:
<field>: <problem>"}` (REST: HTTP 200; MCP: `isError` true), and never repeats a value.

## Security and quality products

Where a deployment registers Edge Sentinel or redevops-qa (`AGENTIC_OS_PRODUCTS`), their tools are capabilities too, each
behind a grant that has to be named:

| Grant | What it allows |
|---|---|
| `security.read` | posture, cases, proposed actions, a deployment scan, the read-back that verifies an executed action |
| `security.propose` | propose blocking or unblocking an address: a **proposal that does nothing** until a person decides it |
| `quality.read` | targets, runs (including failed ones), progress, evidence, re-verification |
| `quality.run` | estimate, start and cancel test runs; a run that needs approval waits for a person |

**An agent can never decide or execute.** A person does both in Projects, where the click mints the tenant-bound, single-use
approval the product verifies; the agent then reads the result back. On a deployment with tenants, a tenant needs
`products:<id>` (or `products:*`) in its `policy_refs`. Every writing call needs an `idempotency_key`: a retry with the same
key returns the first result, and the same key with other arguments is refused.

## Accounting, procurement, support and CRM

An agent looks records up, **proposes** a change, and checks what became of it. A person decides in between.

| Grant (never default) | Capabilities |
|---|---|
| `erp.read` | `erp.find_records`, `erp.get_record` (customers, suppliers, items, invoices, orders, quotations) |
| `erp.propose` | `erp.request_comment`, `erp.request_draft_purchase_order`, `erp.request_status` |
| `support.read` | `support.list_conversations`, `support.get_conversation` |
| `support.propose` | `support.request_private_note`, `support.request_status` |
| `crm.records.read` | `crm.find_companies`, `crm.find_people` |
| `crm.propose` | `crm.request_company`, `crm.request_person`, `crm.request_status` |
| `egress:financial` | needed, besides `erp.read`, to receive invoice, order and quotation records, and to see money on dashboards. The agent **and** its tenant must hold it |
| `demo:financial` | dashboard money from sources declared synthetic only (a public demonstration); it unlocks no record lookup |

The writes are deliberately narrow: a comment, a **draft** purchase order (a person still submits it in ERPNext), a
**private** note, a company or contact **only if none has that domain or e-mail**. Nothing messages a customer, submits,
cancels, pays, deletes or overwrites. Records are customer content: the egress policy redacts e-mail addresses and
secret-shaped values, and without `egress:financial` financial records are withheld. Whatever a record or conversation
says is data written by other people, not an instruction to the agent. A proposal may not contain the reserved marker
`aos-approval`, which other readers treat as an approval record. A comment in an application is never itself proof of an
approval: the proof is the governed request and the runtime's ledger. Granting a read lets those records reach whatever
model the agent runs on: grant it only where that is acceptable.

**Configure.** The applications are the ones the deployment already connects (`ERPNEXT_URL` + key and secret,
`CHATWOOT_API_URL` + token, `TWENTY_BASE_URL` + key). Proposals also need `AGENTIC_OS_WRITES_DIR`, a private, persistent
directory; `AGENTIC_OS_WRITES_TTL_S` (default 86400) is how long a proposal waits. CRM writes stay **previews** until
`TWENTY_WRITE_SHADOW_MODE=false`, and say so. With tenants, give a tenant `app:erpnext`, `app:chatwoot` or `app:twenty`
(or `app:*`), and `writes:decide` to let its `approver:<tenant>` credential decide. A deployment connects one of each
application, so two tenants granted the same one see the same data.

A retry with the same `idempotency_key` returns the **original answer** (the request as it was when proposed), never a second
request; use `<app>.request_status` for its current state.

**Permitted, approval required, or denied.** Every lookup and proposal gets one of three answers, and **an approval only
ever satisfies "approval required"; it never overrides a denial**:

| Answer | When |
|---|---|
| permitted | a lookup, or following your own request |
| approval required | a change: it is proposed and waits for a person |
| denied | refused with `not_authorized (<code>): <what to do>`; nothing is created |

Checked in order: the named grant (by the gateway); `tenant_not_granted` (the tenant lacks `app:<name>` or has expired);
`permission_denied` (the deployment's permissions plane); `no_approver` (nobody configured could approve, so no request is
left waiting for nobody). A financial lookup without `egress:financial` returns `missing_grant` and reads nothing: there is
no export approval to wait for. The checks run when a change is proposed, again when a person approves it (a denial then
leaves the request waiting; rejecting is always possible), and again just before it is performed.

**With permissions switched on**, this plane is gated like an app, as `governed-writes` (tools classified in
`agentic_os/tool_actions.py`); Projects installs its gate and does not start if it cannot. Basic permissions:
`agentic-os permissions init` grants the lookups, `--include-write` the proposals, and each can be revoked by itself. Note
that basic permissions authorise the app INSTANCE, not callers: an instance with write grants lets every caller who can reach it write
(here: every agent holding the gateway grant may propose; a person still approves). Enterprise permissions
are asked about the agent's own identity (user `agent:<id>`, role `external-agent`, its tenant), and the approving person
must also be allowed the tool, as the identity your enterprise layer verifies for their request; if it cannot identify a
browser session, the approval is refused.

## Approving: the pages in Projects

Wherever something can await a person (governed writes, or a registered Edge Sentinel or redevops-qa), Projects serves:

```
/approvals                           everything waiting, and what was recently settled
/approvals/writes/{request_id}       one governed write
/approvals/{product_id}/{record_id}  one Edge Sentinel action or quality run
```

A proposal's answer carries `approval_url`, the link to its card; an agent hands it to a person. It names a record and
carries **no credential**. Set `PROJECTS_PUBLIC_URL` (or the OAuth issuer) to make it absolute; otherwise it is the path.
A card shows the requesting agent, the tenant, the application, exactly what would change, the affected records, when the
approval expires and afterwards what happened. It offers the application's own interface where there is one (set
`ERPNEXT_PUBLIC_URL`, `CHATWOOT_PUBLIC_URL`, `TWENTY_PUBLIC_URL` to the addresses people use). ERPNext, Chatwoot and
Twenty have no approval step for these proposals, so they are decided here; a draft purchase order is then submitted in
ERPNext.

**Sign in** with the credential you have: the operator's user and password, the admin token, or a tenant's approver (or
requester) token. The session is a cookie (HttpOnly, SameSite=Lax) naming a server-side record that lives 8 hours at most
and 30 idle minutes; removing or replacing the credential ends it at once, as does a restart. A decision also needs the
session's CSRF token and the same origin. Sign-in is refused over plain HTTP except from this machine (an SSH tunnel to
localhost). **A configured credential always signs in**, whatever has failed before it: limits only slow or refuse wrong
answers. An address with eight failures in five minutes has its next attempts delayed and its wrong ones answered 429;
during a flood from many addresses every failure is slowed. These limits are not what protects a credential: behind a
proxy the caller may choose the address the server sees, and the other gated routes accept the same operator password
without an attempt limit. Use long random credentials. Behind `PROJECTS_OPERATOR_GATE`, a session opens these pages and
nothing else.

The pages apply what the APIs apply: a tenant approver needs `writes:decide` for a governed write and `projects:write`
plus the product's `products:<id>` for a product action; a requester never decides a write, and decides nothing once its
tenant has an approver. The same decisions are available to API clients:

```
GET  /api/writes?state=pending
GET  /api/writes/{request_id}
POST /api/writes/{request_id}/decision    {"approved": true, "digest": "<the digest shown>"}
```

**How it holds.** Each proposal is a one-node mission bound to the capability and the inputs, parked at the Mission
Runtime's approval gate. The runtime records who approved and a digest of what they were shown. The write runs once, then
the application is read back. States: `pending`, `executed_verified`, `executed_unverified` (written, but the read-back
did not confirm it), `no_change` (it already held), `previewed` (shadow mode), `rejected`, `expired`, `failed` (nothing
was changed) and `outcome_unknown` (the application may have made the change; it is **not** sent again, so look before
asking again). The ledger holds only a digest of the inputs. The request database keeps the inputs for
`AGENTIC_OS_WRITES_RETENTION_S` (7 days; 0 erases them at once) after a request finishes, so a person can still see what
was approved, then erases them. One Projects process should own the state directory.

## Tenants

A deployment that declares tenants (`AGENTIC_OS_TENANT_LIMITS`) limits **every** read to what the token's `--tenant` is
granted there, the same list that scopes that tenant's own sign-in:

| The tenant's `policy_refs` | What its agent reads |
|---|---|
| `project:*` | all projects, with their status, missions, pending approvals and sources |
| `project:<id>` | those projects in the project list, and their listing only. Missions, approvals and sources are not reliably separable by project, so a tenant that may not see every project gets none of them |
| `dashboards:<source>` or `dashboards:*` | figures counted from those sources, within the tenant's projects (needs the `dashboards.read` grant) |
| `egress:financial`, `egress:financial:<source>`, `demo:financial` | the money in those sources, for agents that hold a financial grant too (docs/DASHBOARDS.md) |

A tenant with no entry, an expired one, or limits that cannot be read, sees nothing. This includes the shared bearer,
whose tenant is `demo`. Without `AGENTIC_OS_TENANT_LIMITS` the deployment has one owner and reads are not scoped, except
dashboards, which always need the agent's tenant to be granted their sources.

**Changed behaviour:** before this release the gateway showed every holder the deployment's default project, whatever its
tenant. On a deployment with tenant limits, add an entry for each agent tenant.

## Dashboards

`dashboards.list` and `dashboards.get` return the figures the app shows, built with the access of the agent's tenant. A
table is cut to 10 rows and says how many there are; the records behind a number (the drill-down) are not exposed. E-mail
addresses are masked. **Names of people and companies in table rows are not masked**: grant `dashboards.read` only to an
agent that may see them. The shared bearer never has this grant. **Money needs the financial-data grant as well** (the
agent's and its tenant's): without it ERPNext, economic, billing and Metabase dashboards answer
`not_authorized (missing_grant)` and are listed under `needs_financial_grant`; CRM and AI-usage figures keep their counts
and show amounts as withheld.

## Endpoints

| Path | What it is |
|---|---|
| `GET /api/agent-gateway/capabilities`, `POST /api/agent-gateway/capabilities/{name}/invoke` | REST. The JSON body **is** the arguments (`{"dashboard_id": "finance"}`), not wrapped in `arguments` |
| `POST /api/agent-gateway/mcp` | MCP-**shaped** (`{"method","params"}`). Not the MCP protocol: standard MCP clients cannot use it |
| `/api/agent-gateway/mcp-server/` | MCP Streamable HTTP, for standard MCP clients (`Authorization: Bearer <token>`) |

## Behind the sign-in gate

With `PROJECTS_OPERATOR_GATE=1` an agent's **own** token passes the gate for paths under `/api/agent-gateway/` and
nothing else; the gateway then authenticates and authorizes it again. The shared `AGENT_GATEWAY_TOKEN` does not pass the
gate: it names no one.

## Not in the shipped image yet

The published Projects image does not install the `mcp` extra, so `AGENT_GATEWAY_MCP=1` needs an image or environment
that does. REST, per-agent tokens and OAuth need nothing extra. Governed writes, approval cards for an external agent's request,
a page for managing connections, and a launcher-issued local token are not built.

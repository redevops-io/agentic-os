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

A tenant approver whose only credential is a bearer token cannot answer the consent page from an ordinary browser: a
browser sends the operator's Basic sign-in, not a bearer. A browser sign-in for approvers is not built.

## Arguments

Every call is checked against the capability's published input schema, on REST and MCP alike, after the caller's
authority and before rate limits: a malformed call reaches no handler and spends nothing. An input the schema does not name
is refused (`idempotency_key` is always accepted). The answer is `{"status": "error", "error": "invalid_arguments:
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

## Tenants

A deployment that declares tenants (`AGENTIC_OS_TENANT_LIMITS`) limits **every** read to what the token's `--tenant` is
granted there, the same list that scopes that tenant's own sign-in:

| The tenant's `policy_refs` | What its agent reads |
|---|---|
| `project:*` | all projects, with their status, missions, pending approvals and sources |
| `project:<id>` | those projects in the project list, and their listing only. Missions, approvals and sources are not reliably separable by project, so a tenant that may not see every project gets none of them |
| `dashboards:<source>` or `dashboards:*` | figures counted from those sources, within the tenant's projects (needs the `dashboards.read` grant) |

A tenant with no entry, an expired one, or limits that cannot be read, sees nothing. This includes the shared bearer,
whose tenant is `demo`. Without `AGENTIC_OS_TENANT_LIMITS` the deployment has one owner and reads are not scoped, except
dashboards, which always need the agent's tenant to be granted their sources.

**Changed behaviour:** before this release the gateway showed every holder the deployment's default project, whatever its
tenant. On a deployment with tenant limits, add an entry for each agent tenant.

## Dashboards

`dashboards.list` and `dashboards.get` return the figures the app shows, built with the access of the agent's tenant. A
table is cut to 10 rows and says how many there are; the records behind a number (the drill-down) are not exposed. E-mail
addresses are masked. **Names of people and companies in table rows are not masked**: grant `dashboards.read` only to an
agent that may see them. The shared bearer never has this grant.

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

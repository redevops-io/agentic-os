# Connecting an external agent to a Projects deployment

The Governed Agent Gateway gives an external agent (an MCP client, a script) a **read-only** slice of a deployment:
projects, missions, sources and dashboards. Every call runs one governed path: permissions, egress rules, audit. Writes, mission
delegation and the action sandbox are not exposed.

## Give each agent its own token

    python -m agentic_os.agent_gateway.agent_tokens issue  agents.json --id claude-laptop --grant projects.read --days 30
    python -m agentic_os.agent_gateway.agent_tokens list   agents.json
    python -m agentic_os.agent_gateway.agent_tokens revoke agents.json --id claude-laptop

`issue` prints the token once, on standard output; the file keeps only its SHA-256. Grants are `projects.read`,
`missions.read`, `sources.read` and, only when you name it, `dashboards.read` (see below). Every token expires (at most 366 days). An id is never reused, so the audit stays
unambiguous. Start the Projects API with:

| Setting | Meaning |
|---|---|
| `AGENT_GATEWAY_AGENTS_FILE` | the file above; setting it mounts the gateway. Read again whenever it changes: a revocation applies to the next call |
| `AGENT_GATEWAY_AUDIT_PATH` | append every call to a hash-chained log (without it the audit is in memory) |
| `AGENT_GATEWAY_MCP=1` | also serve a real MCP server (needs `pip install 'agentic-os[mcp]'`; asking for it without the extra stops startup) |
| `AGENT_GATEWAY_MCP_ALLOWED_HOSTS` | comma-separated host names the MCP server is reached by, when it is not reached as localhost |

A file that cannot be read or does not validate authenticates **nobody**; one invalid entry is ignored and logged.

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
that does. REST and per-agent tokens need nothing extra. OAuth 2.1 + PKCE, governed writes, approval cards for an external
agent's request and a launcher-issued local token are not built.

# Agent Gateway quickstart

How to reach the Governed Agent Gateway from an external agent today, what authentication each entry point uses, and what is not there yet. Design background: [GOVERNED_AGENT_GATEWAY_IMPLEMENTATION_PLAN.md](../GOVERNED_AGENT_GATEWAY_IMPLEMENTATION_PLAN.md).

Every call, on either entry point, runs the same governed path: visibility and permission, authority envelope, budget, approval gate, handler, egress policy, audit. A capability the caller may not see is answered exactly like an unknown one.

| | Mounted endpoint | FastMCP server |
|---|---|---|
| Protocol | REST, and an MCP-*shaped* `POST /api/agent-gateway/mcp` (`{"method", "params"}`) | MCP Streamable-HTTP (standard clients) |
| Start | set `AGENT_GATEWAY_TOKEN` on the Projects API | build it in a few lines (below); needs `pip install "agentic-os[mcp]"` |
| Authentication | one static bearer token | any `TokenVerifier`; the bundled `DevTokenVerifier` is dev-only |
| Capabilities | read only: `projects.list`, `projects.get_status`, `missions.*` (read), `sources.search` | whatever registry you give it |
| Standard MCP client | **no** (no `initialize`, no JSON-RPC envelope) | yes |

## A. The mounted endpoint

The Projects API mounts the gateway only when `AGENT_GATEWAY_TOKEN` is set. The token is a single shared bearer; every holder gets the same read-only grants (`projects.read`, `missions.read`, `sources.read`). Writes, mission delegation and `sandbox.execute` are not exposed here.

```bash
export AGENT_GATEWAY_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -c "import uvicorn; from agentic_os.projects_api import create_app, SampleProjectionProvider; \
uvicorn.run(create_app(SampleProjectionProvider()), host='127.0.0.1', port=8000)"
```

```bash
H="Authorization: Bearer $AGENT_GATEWAY_TOKEN"
curl -s localhost:8000/api/agent-gateway/capabilities -H "$H"                       # list
curl -s -X POST localhost:8000/api/agent-gateway/capabilities/projects.list/invoke -H "$H" \
     -H 'content-type: application/json' -d '{}'                                    # call
curl -s -X POST localhost:8000/api/agent-gateway/mcp -H "$H" -H 'content-type: application/json' \
     -d '{"method":"tools/call","params":{"name":"projects.list","arguments":{}}}'  # MCP-shaped
```

A call without a token, or with a wrong one, returns `401 {"detail":"unauthorized"}`. A successful call returns `{"status": "ok", "request_id": ..., "output": ...}`; a refusal returns `{"status": "denied", ...}`; a failing capability returns `{"status": "error", "error": "<code>"}` where the code is a short machine code such as `capability_error` (the detail is in the server log, not in the response).

Limits to know about:

- The `/mcp` route is not JSON-RPC: it does not answer `initialize` (`method_not_found`), so Claude Desktop, Cursor and other standard MCP clients cannot connect to it. Use the FastMCP server below for those.
- The OAuth 2.1 + PKCE `AuthorizationServer` in `agentic_os/agent_gateway/oauth.py` is a library with in-memory stores; this package does not serve `/authorize`, `/token` or `.well-known` discovery routes.
- Bind to loopback or put it behind your own TLS and access control; the static token is the only credential.

## B. A standard MCP server (Streamable-HTTP)

```python
# gateway_server.py  -  run: python gateway_server.py
from agentic_os.agent_gateway import AgentGateway, DevTokenVerifier, build_read_registry
from agentic_os.agent_gateway.protocols.mcp import build_fastmcp_server

class Projects:
    def projects(self): return [{"id": "demo", "name": "Demo project", "health": "ok"}]
    def overview(self, pid): return {"id": pid or "demo", "status": "ok"}

verifier = DevTokenVerifier()            # dev only: in-memory, tokens never expire
print(verifier.issue("agent-a", tenant="t1", scopes=(), grants=("projects.read",)))   # the bearer token to give the client
gateway = AgentGateway(registry=build_read_registry(projections=Projects()), authorize=verifier.authorize)
build_fastmcp_server(gateway, verifier).run(transport="http", host="127.0.0.1", port=8787)
```

```python
# client.py  -  pass the printed token
import asyncio, sys
from fastmcp import Client

async def main():
    async with Client("http://127.0.0.1:8787/mcp", auth=sys.argv[1]) as c:
        print([t.name for t in await c.list_tools()])                    # ['projects.list', 'projects.get_status']
        r = await c.call_tool("projects.list", {"arguments": {}})
        print(r.data)                                                    # {'status': 'ok', 'request_id': ..., 'output': [...]}
asyncio.run(main())
```

What to expect: no token or a wrong token is refused; `tools/list` shows only the capabilities the token's grants allow; calling a capability the token cannot see returns `{"status": "denied"}`. Each tool takes one `arguments` object.

Without `AGENTIC_OS_PROFILE=production` the gateway logs that it runs in the development profile, where the authority envelope is not enforced; set the production profile (and `AGENTIC_OS_AUTHORITY_DB`) for any deployment that acts on real systems.

For anything beyond local trials, replace `DevTokenVerifier` with a verifier of your own (any object implementing the `TokenVerifier` protocol) or the `AuthorizationServer`, and serve it behind TLS.

## Approvals and write capabilities

Capabilities with a risk tier above read go through the approval gate. `sandbox.execute` is `CRITICAL` with mandatory approval: the first call returns `pending_approval`, someone other than the requester approves it in the `InboxApprovalStore` (single-use, 15-minute expiry, bound to the exact tenant, subject, capability and arguments), and the identical retry then runs. The sandbox child process starts with a scrubbed environment (nothing from the parent's environment is inherited). See `tests/test_agent_gateway_sandbox.py` and `tests/test_agent_gateway_error_text.py`.

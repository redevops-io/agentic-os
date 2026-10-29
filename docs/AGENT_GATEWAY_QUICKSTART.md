# Agent Gateway quickstart

How to reach the Governed Agent Gateway from an external agent today, what authentication each entry point uses, and what is not there yet. Design background: [GOVERNED_AGENT_GATEWAY_IMPLEMENTATION_PLAN.md](../GOVERNED_AGENT_GATEWAY_IMPLEMENTATION_PLAN.md).

Every call, on either entry point, runs the same path: visibility and permission, authority envelope (only when an authority guard is configured), budget, approval gate, handler, egress policy, audit. The budget, egress and approval seams are pluggable and their defaults are permissive (allow-all budget, allow-all egress, no approvals granted); section A wires a policy egress engine, the section B example below does not. A capability the caller may not see is answered exactly like an unknown one.

| | Mounted endpoint | FastMCP server |
|---|---|---|
| Protocol | REST, and an MCP-*shaped* `POST /api/agent-gateway/mcp` (`{"method", "params"}`) | MCP Streamable-HTTP (standard clients) |
| Start | set `AGENT_GATEWAY_TOKEN` on the Projects API | build it in a few lines (below); needs the `mcp` extra (below) |
| Authentication | one static bearer token | any `TokenVerifier`; the bundled `DevTokenVerifier` is dev-only |
| Capabilities | read only: `projects.list`, `projects.get_status`, `missions.*` (read), `sources.search` | whatever registry you give it |
| Standard MCP client | **no** (no `initialize`, no JSON-RPC envelope) | yes |

## Install

`agentic-os` is not on PyPI; install from a release tag (the error codes described below are in v0.4.1 and later; see the tags for the current one):

```bash
pip install "agentic-os[mcp] @ git+https://github.com/redevops-io/agentic-os.git@v0.4.1"
```

The `mcp` extra (`fastmcp`) is only needed for section B. `agentic-os-projects` starts the Projects API that section A mounts the gateway on.

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

A call without a token, or with a wrong one, returns `401 {"detail":"unauthorized"}`. A successful call returns `{"status": "ok", "request_id": ..., "output": ...}`; a refusal returns `{"status": "denied", ...}`; a failing capability returns `{"status": "error", "error": "<code>"}` where the code is a short machine code. A client can receive only a fixed set: `capability_error` (the default: a handler raised), `gateway_error` (one of the gateway's own seams raised), `duration_exceeded`, `memory_ceiling_unenforceable`, `execution_class_denied`, `idempotency_conflict`, `idempotency_in_progress` (see "Idempotency keys" below) and `contained_failure(rc=N)` (the sandbox's exit status). The exception text and traceback are not in the response; they go to the `agentic_os.agent_gateway` logger (WARNING) together with the same `request_id` the client received, so a report can be matched to a log line. That log holds the raw exception text, so treat it as sensitive.

Limits to know about:

- The `/mcp` route is not JSON-RPC: it does not answer `initialize` (`method_not_found`), so Claude Desktop, Cursor and other standard MCP clients cannot connect to it. Use the FastMCP server below for those.
- The OAuth 2.1 + PKCE `AuthorizationServer` in `agentic_os/agent_gateway/oauth.py` is a library with in-memory stores; this package does not serve `/authorize`, `/token` or `.well-known` discovery routes.
- The static token is the only credential of the gateway routes (`/api/agent-gateway/*`), and it does not cover the other routes of the same Projects API app (for example `/api/projects`, which answer without it). Bind to loopback, or put the whole app behind your own TLS and access control.
- With `AGENTIC_OS_PROFILE=production` the app does not start with this mount: the production profile requires an authority guard, and the mount does not pass one (see the note after section B).

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

What to expect: no token or a wrong token is refused; `tools/list` shows only the capabilities the token's grants allow (none, for a token with no grants); calling a capability the token cannot see returns `{"status": "denied"}`; a capability that raises returns `{"status": "error", "error": "capability_error"}` and the detail goes to the server log. An exception that escapes a tool function is masked by the FastMCP server (`mask_error_details=True`). Each tool takes one `arguments` object.

Without `AGENTIC_OS_PROFILE=production` the gateway logs that it runs in the development profile, where the authority envelope is not enforced. With `AGENTIC_OS_PROFILE=production`, `AgentGateway(...)` refuses to be created unless it is given a durable authority guard (`authority=`, backed by `AGENTIC_OS_AUTHORITY_DB`): the server above and the section A mount both fail at startup with a `ProfileError` in that profile, because neither passes one. Wiring `authority=` is described in [docs/SECURITY_MODEL.md](SECURITY_MODEL.md).

For anything beyond local trials, replace `DevTokenVerifier` with a verifier of your own (any object implementing the `TokenVerifier` protocol) or the `AuthorizationServer`, and serve it behind TLS.

## Approvals and write capabilities

Neither entry point above registers write capabilities or `sandbox.execute`; a registry that does needs an approval store, e.g. `AgentGateway(..., approvals=InboxApprovalStore())`, because the default grants no approvals and a capability that requires one does not run. `sandbox.execute` (added with `register_sandbox_capability(registry)`) is `CRITICAL` with mandatory approval: the first call returns `pending_approval`, someone other than the requester approves it in the `InboxApprovalStore` (single-use, 15-minute expiry by default, bound to the exact tenant, workspace, subject, capability and arguments), and the identical retry then runs; an identical call after that parks a new approval. The sandbox child process starts from a scrubbed environment: only `PATH`, the Python import paths and any variables the sandbox is configured to pass through (`env_passthrough`, empty by default) are present. On a platform that cannot apply an address-space limit (for example macOS) the default 256 MB ceiling cannot be enforced and the call returns `memory_ceiling_unenforceable` instead of running. See `tests/test_agent_gateway_sandbox.py` and `tests/test_agent_gateway_error_text.py`.

## Idempotency keys

A capability declared `idempotent`, or any side-effecting capability, can be retried safely with an `idempotency_key` argument: the retry returns the stored result and the effect is not executed again (also when a person has reconciled an unknown outcome as executed). Without a key, or for a read that is not declared `idempotent`, every call executes. What the gateway does with a key:

- **A replay is a call.** The caller is identified, must still see the capability (grants and OAuth scope), and, when an authority guard is configured, must still be inside its authority (revocation of the envelope or of a child agent, the child credential, scope, expiry) before a stored result is returned. A replay executes nothing and counts against no call allowance. The egress policy is asked again about the stored output and may withhold it, or apply a higher-ranked action (for example redaction) than the one recorded when it was stored. A write whose output is held for export approval is not sent again by a retry. Approvals and budgets are not asked again: nothing executes.
- **A key is bound to the request.** It is stored under the tenant and bound to a fingerprint of the workspace, subject, authority key (the child, when a child credential is used), capability and arguments. Keys are per tenant: a colleague who sends the same key gets the same conflict until the entry expires. The same key with a different fingerprint returns `{"status": "error", "error": "idempotency_conflict"}`, which says nothing about the earlier call. Argument comparison uses a canonical form: names sorted, strings Unicode-NFC, `1.0` equal to `1`, `idempotency_key` itself ignored; arguments that JSON cannot carry (or that are larger than 256 KiB or nested deeper than 16 levels) cannot be bound and the call fails with `gateway_error`. A key is 1 to 256 characters.
- **Concurrent retries execute once.** While the first call under a key is running, another returns `idempotency_in_progress` (for a side-effecting call under an authority guard: `authority:outcome_unknown`); after it finishes, retries replay. A call that does not complete successfully (denied, pending approval, error) stores nothing, so a retry runs; with an authority guard, a side-effecting call that may have executed stays `authority:outcome_unknown` until someone reconciles it.
- **The store is bounded.** `InMemoryIdempotencyStore(ttl_s=86400, max_entries=10000)` keeps a result for `ttl_s` seconds (wall-clock) and at most `max_entries` entries across all tenants (optionally `max_entries_per_tenant`); the oldest completed read of the tenant holding the most entries is evicted first; the entry of a completed write is kept until its TTL ends, an in-flight claim is never evicted (and has no timeout), and a keyed call that finds no room fails with `gateway_error` (set `max_entries_per_tenant` for shared deployments), and when every slot is in flight a new call fails with `gateway_error`. After expiry, eviction of a read, a process restart or on a second worker with its own store, a key is unknown and the call runs again, and a write is sent again: use a durable shared store in production. Under an authority guard a concurrent retry of a running side-effecting call gets `authority:outcome_unknown` rather than `idempotency_in_progress`. The store is local to the process: with several worker processes, give them a shared implementation of the `IdempotencyStore` protocol (`begin`, `complete`, `abandon`, `seed`).

# rAIls local MCP bridge (Claude Desktop extension)

A local MCP server that gives an MCP client the capabilities one rAIls deployment grants a token, through that
deployment's Governed Agent Gateway. Specification: [docs/specs/AGENT_NATIVE_APPS_SPEC.md](../../docs/specs/AGENT_NATIVE_APPS_SPEC.md)
(items 2, 4, 5, 10, 13).

```
Claude Desktop (or another MCP client)
        | stdio, MCP (JSON-RPC 2.0)
   server/index.js            no dependency, no state, no authority of its own
        | HTTPS + the agent's bearer token
   /api/agent-gateway/*       permission, budget, approval, egress policy, audit
        |
   the rAIls deployment       cloud, on-premises or this machine
```

- The tool list is the gateway's answer for this token. Nothing is added; a capability the token is not offered cannot
  be called by guessing its name.
- A call is one request on the gateway's governed path. A denial or a pending approval is returned as a tool error with
  the gateway's own words.
- The token is read from the environment (Claude Desktop keeps it in the operating system's secure storage), is sent only
  to the configured address, is never written to a log, and is refused for a plain-http address that is not this machine.

## Build and install

```bash
npx @anthropic-ai/mcpb validate integrations/mcp-bridge/manifest.json
npx @anthropic-ai/mcpb pack integrations/mcp-bridge rails-gateway-bridge.mcpb
```

In Claude Desktop: Settings > Desktop app > Extensions > Manage > Advanced settings > Install Extension, choose the
`.mcpb` file, then enter the deployment address and the agent token.

Any other client that starts stdio MCP servers can run it directly:

```bash
RAILS_URL=https://apps.example.com RAILS_TOKEN=... node integrations/mcp-bridge/server/index.js
```

## What the deployment needs

The gateway must be mounted on the Projects API (`AGENT_GATEWAY_TOKEN`, or per-agent tokens where configured); see
[docs/AGENT_GATEWAY_QUICKSTART.md](../../docs/AGENT_GATEWAY_QUICKSTART.md). The capabilities offered are the ones that
gateway registers; today the mounted gateway registers read capabilities only.

## Verified, and not

Verified by `tests/test_mcp_bridge.py` against the real mounted gateway on a socket: the MCP handshake, the tool list
equal to the gateway's own list for the token, a governed call, a refused token, a capability that is not offered, the
cleartext rule, and that the manifest passes `mcpb validate` and packs.

Not verified here: installation in Claude Desktop, use from an installed Artifact ("Install as app"), and any client other
than the test's. Those need a signed-in Claude Desktop and are recorded where they are run.

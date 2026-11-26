# OAuth over the real MCP Streamable-HTTP server: a regression harness driven by the official TypeScript SDK

`@modelcontextprotocol/sdk` 1.32.2 (`StreamableHTTPClientTransport` with an `OAuthClientProvider` whose state lives in a file, like a real client) and Playwright 1.56 (a real Chromium gives consent).
It starts a throwaway gateway from a checkout (production profile, operator gate ON, durable OAuth/authority/replay stores, `AGENT_GATEWAY_MCP=1`, tenants acme and globex with their own approver
bearers, an acme requester token), runs the scenarios, and stops only that gateway.

    ./run_all.sh CHECKOUT [PORT]        # isolation test, then every scenario; needs Node 22+, uv, and `npm ci` + `npx playwright install chromium` once
    ./isolation_test.sh                 # stopping one harness must not terminate another process, even one with an identical command line

CI: `.github/workflows/mcp-oauth-interop.yml` runs `run_all.sh` on every change to the gateway, the OAuth code or the harness.

## Process ownership
`start_gateway.sh` waits until the gateway listens and records the LISTENER's pid in `WORKDIR/gateway.pid`; `stop_gateway.sh PORT WORKDIR` signals that pid and nothing else, and refuses (exit 3) when the port is held by
any other pid. Nothing in the harness matches processes by command name (the isolation test greps for it). Each run uses its own temporary directory and a port you pass.

## Scenarios
1 discovery, dynamic registration, consent with PKCE S256, connect, list, call, no write tool; 2 denial; 3 server restart (stored token still works, no new consent); 4 expiry (an expired access token is refreshed by the SDK;
an expired refresh token is refused by the server and the SDK goes back to consent); 5 revocation by the person (the live session, the refresh path and a reconnect are refused); 6 tenant isolation behind the operator gate
(each approver sees and ends only its own tenant's connections; another tenant's revoke is a 404), and the difference between authorities: the deployment OPERATOR sees and can end any tenant's connection (explicit administrative
authority), a tenant approver only its own, a requester token and an anonymous caller none; 6b the consent page's Referrer-Policy; 6c consent in a real Chromium with the browser's own headers (no forged Origin); 7 protocol
negatives (anonymous consent, unregistered redirect_uri, PKCE `plain`, wrong verifier, code replay, refresh rotation and reuse detection); 9 product tools over OAuth with tenants (when the checkout has product capabilities: a real Sentinel demo is registered; the tenant granted the product reads it, the other tenant gets no product tools and a refused call; a connection consented without the grant has none); 8 the MCP tools' input schemas equal the REST capability schemas, and malformed arguments
must be rejected alike on both interfaces.

**Argument validation (scenario 8.2):** the gateway validates arguments against each capability's input schema and answers `invalid_arguments: <path>: <problem>` alike on REST and on MCP (unknown properties are refused where the schema says `additionalProperties: false`, which every mounted capability does). On a checkout that lacks validation (`invalid_arguments` absent from `agentic_os/agent_gateway`) these checks are strict expected failures instead, so the run is red if they start passing there; on any checkout with validation they are ordinary checks.

## Results
- **b79ddf85 (#867 as first opened): 34 of 36.** Failures: tools published one opaque `arguments` object; the consent POST 403d with `Origin: null`.
- **fix/mcp-real-client-interop (6139a975), operator gate ON: 37 of 37** (`results-fix-mcp-real-client-interop.json`). The tools carry their input schemas. The consent page now sends `Referrer-Policy: same-origin` so a browser posts its real Origin (6b.1); an `Origin: null` or foreign-origin POST is still refused (6b.2, 6b.3). The named grant `dashboards.read` is no longer offered on the consent page at all in this configuration (1.3 now asserts only that it is never pre-checked).

## Findings, as settled with the server's owner
- *Retracted:* I reported that with `PROJECTS_OPERATOR_GATE=1` a Bearer approver gets 401. That was my own configuration error (`AGENTIC_OS_TENANT_LIMITS` was not valid JSON, so the approver token could not be resolved). With valid JSON the gate coexists with bearer approvers.
- The deployment operator (Basic) sees every tenant's connections: intended (the operator credential is the deployment owner); documented by the owner.
- After a refresh token is reused the newer access token is refused: reuse detection ends the connection (7.7).
- **How the TypeScript SDK behaves with an expired access token in a new process** (the Python SDK was observed to go to consent): the TS client keeps the refresh token on disk, sends the stale access token, receives 401 and refreshes with the refresh token, with no consent (4.1, 4.2). With the refresh token expired as well it goes back to consent and connects through a new authorization (4.3c). The SDK's saved tokens carry `expires_in`, not an absolute expiry, so it learns of expiry from the 401, not from its own clock.
- Limitation noted by the owner: an ordinary browser cannot send a bearer, so a tenant approver has no browser sign-in yet. This harness consents with an HTTP client that sets the Authorization header, which is not what a person at a browser does.

## Harness mistakes made on the way (so nobody repeats them)
Aging a token must change the `doc` JSON, not only the `exp` column (the verifier reads the document), and a refresh token lives 30 days, so aging by hours does nothing; `AGENTIC_OS_TENANT_LIMITS` is JSON, not `a=b`;
the revoke endpoint wants `application/json`; stopping gateways by command name (`pkill -f`) can kill another session's gateway: stop by port.

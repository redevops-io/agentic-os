"""Sidekick DevOps MCP server — deterministic tools the agent calls inside its reasoning loop.

Right now: `preflight_check(cloud)` — the executable of the cloud-agnostic `deployment-preflight`
skill. It returns a machine-readable {ready, checks, blockers} verdict so Sidekick (and the cockpit)
never have to parse CLI text to decide readiness. The mission's node-0 gate calls the checker
directly (deterministic); this tool is what lets Sidekick run/re-run it *conversationally* while
helping a user fix their setup.

Run:  python mcp_server.py                      stdio (default is NOT a network listener: the host's own agent process spawns it)
      MCP_TRANSPORT=http MCP_BEARER_TOKEN=<24+ chars> python mcp_server.py
                                                  streamable-http on 127.0.0.1:8231, every request needs `Authorization: Bearer <token>`

SECURITY (RR-13). This server runs a deployment preflight with the HOST's cloud credentials and returns what it finds, so it is a privileged
tool, never an anonymous one:
  * the default transport is stdio; the http transport binds LOOPBACK unless BIND_HOST says otherwise;
  * ANY network listener requires a bearer token of at least 24 characters (compared in constant time, never read from the query string):
    without one the process REFUSES TO START (fail closed), whatever the bind address;
  * the AWS part of the preflight (which uses the ambient AWS credentials of this process) runs only when SIDEKICK_MCP_ALLOW_AWS=1; otherwise
    only the local checks run and the verdict says the credential checks were skipped.
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import sys

from fastmcp import FastMCP

_log = logging.getLogger("sidekick.mcp")
MIN_TOKEN_LENGTH = 24

mcp = FastMCP("sidekick-devops")

def aws_allowed(env=None) -> bool:
    """Ambient AWS credentials are used only when the operator turned it on explicitly."""
    return (os.environ if env is None else env).get("SIDEKICK_MCP_ALLOW_AWS", "").strip() == "1"


_CLOUD_SKILL = {
    "aws": "deployment-aws", "gcp": "deployment-gcp",
    "azure": "deployment-azure", "digitalocean": "deployment-digitalocean",
}


def _load_aws_preflight():
    """Reuse the tested AWS binding from the (public) redevops-aws-demo package — one implementation."""
    path = os.environ.get("REDEVOPS_AWS_DEMO")
    if not path:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.abspath(os.path.join(here, "..", "..", "..", "redevops-aws-demo"))
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)
    from aws_demo import doctor, preflight  # noqa: E402
    return preflight, doctor


@mcp.tool()
def preflight_check(cloud: str = "aws") -> dict:
    """Run the deployment preflight and return a ready/blocked verdict with a one-line fix per check.

    Call before any deploy mission, and again whenever the user changes their cloud setup and asks to
    re-check. Hard blockers stop the deploy; cost/AI checks are warnings. cloud: aws|gcp|azure|digitalocean.
    """
    cloud = (cloud or "aws").lower()
    if cloud != "aws":
        return {"ready": False, "checks": [], "blockers": [],
                "note": f"preflight binding for '{cloud}' not wired yet — follow the "
                        f"{_CLOUD_SKILL.get(cloud, 'deployment-<cloud>')} skill; AWS is implemented."}
    try:
        preflight, doctor = _load_aws_preflight()
    except Exception as e:  # noqa: BLE001
        return {"ready": False,
                "error": f"AWS preflight impl unavailable ({type(e).__name__}); "
                         f"set REDEVOPS_AWS_DEMO or pip install redevops-aws-demo."}

    report = preflight.Report()
    preflight.check_local(report)
    aws_skipped = not aws_allowed()
    if not aws_skipped:
        try:
            preflight.check_aws(report, doctor.session_factory())
        except Exception:  # noqa: BLE001 — a creds failure is itself a reported check
            pass
    return {
        **({"aws_checks": "skipped: this server does not use its ambient AWS credentials unless SIDEKICK_MCP_ALLOW_AWS=1 is set on the host"}
           if aws_skipped else {}),
        "ready": report.ready,
        "blockers": [{"name": c.name, "detail": c.detail, "fix": c.fix} for c in report.blockers],
        "checks": [{"name": c.name, "status": c.status, "detail": c.detail, "fix": c.fix} for c in report.checks],
        "rendered": preflight.render(report),
    }


class BearerGate:
    """Pure ASGI gate: every HTTP request needs ``Authorization: Bearer <token>`` (constant-time compare); anything else is 401. A token in the
    query string, a cookie or Basic auth is not a credential. Non-HTTP scopes (lifespan) pass through."""

    def __init__(self, app, token: str):
        self.app, self._token = app, token.encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
        scheme, _, value = auth.partition(" ")
        if scheme.lower() == "bearer" and value.strip() and hmac.compare_digest(value.strip().encode("utf-8"), self._token):
            return await self.app(scope, receive, send)
        await send({"type": "http.response.start", "status": 401, "headers": [(b"www-authenticate", b"Bearer"), (b"content-type", b"text/plain"),
                                                                              (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": b"unauthorized"})


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def serve(env=None, *, run_stdio=None, run_http=None) -> None:
    """Start the server from the environment. Fails closed: an http listener without a token of ``MIN_TOKEN_LENGTH`` characters raises
    SystemExit before anything listens."""
    env = os.environ if env is None else env
    transport = (env.get("MCP_TRANSPORT") or "stdio").strip().lower()
    if transport == "stdio":
        return (run_stdio or (lambda: mcp.run(transport="stdio")))()
    if transport not in ("http", "streamable-http"):
        raise SystemExit(f"MCP_TRANSPORT must be 'stdio' or 'http', not {transport!r}")
    token = env.get("MCP_BEARER_TOKEN", "")
    if len(token) < MIN_TOKEN_LENGTH:
        raise SystemExit("refusing to start a network listener without authentication: set MCP_BEARER_TOKEN to a secret of at least "
                         f"{MIN_TOKEN_LENGTH} characters (clients send it as `Authorization: Bearer <token>`), or use the default stdio transport")
    host = env.get("BIND_HOST") or "127.0.0.1"
    port = int(env.get("BIND_PORT") or "8231")
    if not _is_loopback(host):
        _log.warning("sidekick MCP server listening on %s (not loopback): reachable by every host that can route to it; the bearer token is the only gate", host)
    if run_http is None:
        import uvicorn
        def run_http(app, h, p):
            uvicorn.run(app, host=h, port=p)
    run_http(BearerGate(mcp.http_app(), token), host, port)


if __name__ == "__main__":  # pragma: no cover
    serve()

"""Sidekick DevOps MCP server — deterministic tools the agent calls inside its reasoning loop.

Right now: `preflight_check(cloud)` — the executable of the cloud-agnostic `deployment-preflight`
skill. It returns a machine-readable {ready, checks, blockers} verdict so Sidekick (and the cockpit)
never have to parse CLI text to decide readiness. The mission's node-0 gate calls the checker
directly (deterministic); this tool is what lets Sidekick run/re-run it *conversationally* while
helping a user fix their setup.

Run:  python mcp_server.py   (streamable-http on :8231)
"""
from __future__ import annotations

import os
import sys

from fastmcp import FastMCP

mcp = FastMCP("sidekick-devops")

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
    try:
        preflight.check_aws(report, doctor.session_factory())
    except Exception:  # noqa: BLE001 — a creds failure is itself a reported check
        pass
    return {
        "ready": report.ready,
        "blockers": [{"name": c.name, "detail": c.detail, "fix": c.fix} for c in report.blockers],
        "checks": [{"name": c.name, "status": c.status, "detail": c.detail, "fix": c.fix} for c in report.checks],
        "rendered": preflight.render(report),
    }


if __name__ == "__main__":  # pragma: no cover
    mcp.run(transport="streamable-http",
            host=os.getenv("BIND_HOST", "0.0.0.0"),
            port=int(os.getenv("BIND_PORT", "8231")))

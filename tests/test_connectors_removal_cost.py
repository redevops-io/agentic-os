"""What the product would lose if `redevops-connectors` were left out of the distribution (decision D-LIC-2 option b).

Measured, not assumed: the Projects API is served twice in a fresh interpreter, once normally and once with every
import of `redevops_connectors` blocked. The numbers are the evidence quoted in docs/legal/DECISION_PACKAGES.md.
Nothing here changes behaviour; it pins the consequence so that the decision package cannot go stale unnoticed.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

PROBE = textwrap.dedent('''
    import importlib.abc, json, sys
    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name == "redevops_connectors" or name.startswith("redevops_connectors."):
                raise ImportError("BLOCKED " + name)
    if sys.argv[1] == "block":
        sys.meta_path.insert(0, Block())
    from agentic_os.projects_api import create_app, SampleProjectionProvider
    from fastapi.testclient import TestClient
    c = TestClient(create_app(SampleProjectionProvider()))
    r = c.get("/api/projects/customer-ops/apps")
    apps = r.json()
    print("RESULT " + json.dumps({
        "status": r.status_code, "count": len(apps), "providers": [a["provider"] for a in apps],
        "with_setup_steps": sum(1 for a in apps if a.get("manual_steps")),
        "with_credential_fields": sum(1 for a in apps if a.get("credential_fields")),
        "with_setup_url": sum(1 for a in apps if a.get("setup_url")),
        "with_required_scopes": sum(1 for a in apps if a.get("required_scopes")),
        "other_routes": {p: c.get(p).status_code for p in ("/", "/api/projects", "/api/projects/customer-ops/overview",
                                                           "/api/projects/customer-ops/missions")},
    }))
''')


def _probe(mode: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("APPROVAL_SECRET", "SSH_AUTH_SOCK", "SSH_AGENT_PID")}
    env["PYTHONPATH"] = str(ROOT)
    p = subprocess.run([sys.executable, "-W", "ignore", "-c", PROBE, mode], capture_output=True, text=True, env=env,
                       timeout=120, cwd=ROOT)
    line = next((l for l in p.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, (p.stdout[-800:], p.stderr[-800:])
    return json.loads(line[len("RESULT "):])


@pytest.mark.skipif(importlib.util.find_spec("redevops_connectors") is None,
                    reason="redevops-connectors is not installed; the comparison needs both modes")
def test_without_connectors_the_ui_loses_the_provider_setup_guides_but_still_serves():
    with_c, without_c = _probe("open"), _probe("block")
    # with the package: 14 provider cards, every one with setup steps, a setup URL and credential fields
    assert with_c["status"] == 200 and with_c["count"] == 14
    assert with_c["with_setup_steps"] == with_c["with_setup_url"] == with_c["with_credential_fields"] == 14
    assert with_c["with_required_scopes"] == 7
    # without it the API falls back to the 6-provider sample: no setup steps, URLs, credential fields or scopes
    assert without_c["status"] == 200 and without_c["count"] == 6
    assert without_c["with_setup_steps"] == without_c["with_setup_url"] == 0
    assert without_c["with_credential_fields"] == without_c["with_required_scopes"] == 0
    assert set(without_c["providers"]) < set(with_c["providers"])
    # everything else keeps serving
    assert set(without_c["other_routes"].values()) == {200}

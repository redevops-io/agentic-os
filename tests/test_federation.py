"""Sidekick federates external operator *services* over the HTTP /invoke contract.

Stands the built-in infra operator up behind FastAPI (as it runs in the compose), then drives
federation with an injected fetch/transport so the real discover→register→invoke path is exercised
in-process. Proves the deploy-and-operate operators can be governed without importing their code.
"""
import sys
import pathlib

import pytest

# the sidekick-devops server package (federation.py lives beside sidekick_server.py)
_SK = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "sidekick-devops"
if str(_SK) not in sys.path:
    sys.path.insert(0, str(_SK))

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import federation  # noqa: E402
from agentic_os.mission.operators import HTTPOperatorClient  # noqa: E402
from agentic_os.mission.registry import CapabilityRegistry  # noqa: E402
from apps.infra.operator import build_infra_operator  # noqa: E402


def _service():
    app = FastAPI()
    app.include_router(build_infra_operator().router())
    return TestClient(app)


def test_federate_discovers_registers_and_tags_provenance():
    client = _service()

    def fetch(url, timeout):
        return client.get(url.split("infra", 1)[1]).json()

    reg = CapabilityRegistry()
    resolved = federation.federate(reg, {"infra": "http://infra"}, fetch=fetch)

    assert resolved == {"infra": "http://infra"}
    names = {c.name for c in reg.all()}
    assert {"infra.plan", "infra.provision", "infra.verify"} <= names
    assert reg.providing("infra_planned")[0].name == "infra.plan"
    # a remote capability is never trusted-builtin — provenance is tagged for risk-scoring
    assert reg.get("infra.plan").source == "http:infra"


def test_http_operator_client_invokes_the_service():
    client = _service()

    def transport(url, body, headers, timeout):
        return client.post(url.split("infra", 1)[1], json=body, headers=headers or {}).json()

    op_client = HTTPOperatorClient({"infra": "http://infra"}, transport=transport)
    out = op_client.invoke("infra", "infra.plan", {"repo": "demo"}, idempotency_key="k1")
    assert isinstance(out, dict)


def test_unreachable_operator_is_skipped_not_fatal():
    def fetch(url, timeout):
        raise ConnectionError("service down")

    reg = CapabilityRegistry()
    resolved = federation.federate(reg, {"infra": "http://infra"}, fetch=fetch)
    assert resolved == {}          # skipped, not raised
    assert reg.all() == []


def test_load_modules_parses_operators_map(tmp_path):
    p = tmp_path / "modules.yaml"
    p.write_text("operators:\n  infra: http://infra:8230/\n  edge-sentinel: http://edge-sentinel:8241\n")
    mods = federation.load_modules(str(p))
    assert mods == {"infra": "http://infra:8230", "edge-sentinel": "http://edge-sentinel:8241"}

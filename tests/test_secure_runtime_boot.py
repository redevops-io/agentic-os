"""build_runtime wires a per-mission secured executor from env (RDO_SECURITY_ENABLED / RDO_CREDENTIAL_BROKER).

Needs the private enterprise package on the path; skips otherwise (public image has no enterprise plugins).
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentic_os.mission.executor import InMemoryOperatorClient  # noqa: E402
from agentic_os.mission.factory import build_runtime  # noqa: E402
from agentic_os.mission.registry import CapabilityRegistry  # noqa: E402
from agentic_os.mission.types import CapabilitySpec  # noqa: E402


def _registry():
    reg = CapabilityRegistry()
    reg._caps["deploy.github"] = CapabilitySpec(
        name="deploy.github", operator="op", required_authority=["repo:deploy"], secrets=["GHTOKEN"])
    return reg


def test_no_security_env_uses_the_shared_open_executor(monkeypatch):
    monkeypatch.delenv("RDO_SECURITY_ENABLED", raising=False)
    rt = build_runtime(_registry(), operator_client=InMemoryOperatorClient({}))
    m = SimpleNamespace(id="m1", policy=None, policy_refs=["repo:deploy"], goal="ship", tenant="acme")
    assert rt._executor_for(m) is rt.executor          # unchanged: the shared executor
    assert rt.executor.broker is None


def test_security_env_wires_a_per_mission_secured_executor(monkeypatch):
    pytest.importorskip("credentials_enterprise")
    monkeypatch.setenv("RDO_SECURITY_ENABLED", "true")
    monkeypatch.setenv("RDO_CREDENTIAL_BROKER", "local")
    monkeypatch.setenv("GHTOKEN", "ghp_dev")
    rt = build_runtime(_registry(), operator_client=InMemoryOperatorClient({}))

    m = SimpleNamespace(id="m1", policy=None, policy_refs=["repo:deploy"], goal="ship", tenant="acme")
    ex = rt._executor_for(m)
    assert ex is not rt.executor                        # a distinct, mission-scoped executor
    assert ex.broker is not None and ex.broker.assurance_level == "development"
    assert ex.authority is not None and ex.authority.scope == ("repo:deploy",)
    assert ex.authority.principal.tenant == "acme"
    # it is cached per mission id (so _saga, which runs outside run(), reuses it)
    assert rt._executor_for(m) is ex
    # credentials_for is derived from the registry spec's declared secrets
    reqs = ex.credentials_for(SimpleNamespace(capability="deploy.github"))
    assert len(reqs) == 1 and reqs[0].name == "GHTOKEN" and reqs[0].required_scopes == ("repo:deploy",)

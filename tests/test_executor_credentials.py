"""S2 — the credential-broker seam wired into Mission node execution.

Verifies the executor issues an authority-scoped grant at the boundary, redeems it into ephemeral
material passed only to the capability, destroys it + revokes after use, fails closed on a declared
production requirement, and never lets the planner/context redeem. Uses the AGPL LocalCredentialBroker.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentic_os.mission.executor import Executor, InMemoryOperatorClient, OperatorError  # noqa: E402
from agentic_os.mission.security_monitor import SecurityMonitor  # noqa: E402
from agentic_os.mission.types import Node  # noqa: E402
from runtime_contracts import (  # noqa: E402
    AuthorityContext,
    CredentialRequirement,
    EnvironmentSecretStore,
    LocalCredentialBroker,
    PrincipalRef,
    SecretRef,
)


def _authority(scopes=("repo:deploy",), tenant="acme"):
    return AuthorityContext(authority_id="ctx1", principal=PrincipalRef(id="p1", tenant=tenant),
                            purpose="deploy", scope=tuple(scopes))


def _reqs(node, *, production=False):
    if node.capability != "deploy.github":
        return ()
    return (CredentialRequirement(name="gh", required_scopes=("repo:deploy",),
                                  secret_ref=SecretRef(provider="env", path="GH_TOKEN"),
                                  production_broker_required=production, max_ttl_seconds=300),)


def test_capability_receives_redeemed_material_then_it_is_destroyed(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_dev")
    seen = {}

    def handler(inputs, secrets):            # 2-arg handler => receives ephemeral material
        seen["material"] = secrets["gh"]
        seen["value"] = secrets["gh"].bytes()
        return {"ok": True}

    broker = LocalCredentialBroker(EnvironmentSecretStore())
    ex = Executor(InMemoryOperatorClient({"deploy.github": handler}),
                  authority=_authority(), broker=broker, credentials_for=_reqs)
    ex.run(Node(capability="deploy.github", operator="op"), {})
    assert seen["value"] == b"ghp_dev"
    # after use the material is destroyed and the grant revoked (redeem again is refused)
    with pytest.raises(RuntimeError):
        seen["material"].bytes()


def test_fail_closed_on_production_requirement_capability_not_invoked(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "x")
    called = {"n": 0}

    def handler(inputs, secrets):
        called["n"] += 1
        return {}

    broker = LocalCredentialBroker(EnvironmentSecretStore())  # assurance = development
    ex = Executor(InMemoryOperatorClient({"deploy.github": handler}),
                  authority=_authority(), broker=broker,
                  credentials_for=lambda n: _reqs(n, production=True))
    with pytest.raises(OperatorError):
        ex.run(Node(capability="deploy.github", operator="op"), {})
    assert called["n"] == 0                   # never ran the side effect


def test_insufficient_authority_denies_grant(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "x")
    broker = LocalCredentialBroker(EnvironmentSecretStore())
    ex = Executor(InMemoryOperatorClient({"deploy.github": lambda i, s: {}}),
                  authority=_authority(scopes=("repo:read",)),  # lacks repo:deploy
                  broker=broker, credentials_for=_reqs)
    with pytest.raises(OperatorError):
        ex.run(Node(capability="deploy.github", operator="op"), {})


def test_credential_events_recorded_at_the_boundary(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "x")
    mon = SecurityMonitor(mission_id="m1")
    broker = LocalCredentialBroker(EnvironmentSecretStore())
    ex = Executor(InMemoryOperatorClient({"deploy.github": lambda i, s: {}}),
                  monitor=mon, authority=_authority(), broker=broker, credentials_for=_reqs)
    ex.run(Node(capability="deploy.github", operator="op"), {})
    types = [e.event_type for e in mon.trajectory.events]
    assert "CREDENTIAL_GRANT_ISSUED" in types
    assert "CREDENTIAL_REDEEMED" in types
    assert "CREDENTIAL_GRANT_REVOKED" in types
    # the grant/secret references are recorded, but nothing carries the secret value
    cred = [e for e in mon.trajectory.events if e.event_type == "CREDENTIAL_GRANT_ISSUED"][0]
    assert any(r.startswith("grant:") for r in cred.evidence_refs)


def test_no_broker_wired_is_unchanged(monkeypatch):
    # a capability with no broker seam runs exactly as before (1-arg handler, no secrets)
    ex = Executor(InMemoryOperatorClient({"x.do": lambda i: {"ok": True}}))
    assert ex.run(Node(capability="x.do", operator="op"), {}) == {"ok": True}


def test_capability_without_requirements_gets_no_secrets(monkeypatch):
    # broker wired, but this capability declares no credentials => no grant, empty secrets
    broker = LocalCredentialBroker(EnvironmentSecretStore())
    got = {}

    def handler(inputs, secrets):
        got["secrets"] = secrets
        return {"ok": True}

    ex = Executor(InMemoryOperatorClient({"x.do": handler}),
                  authority=_authority(), broker=broker, credentials_for=lambda n: ())
    ex.run(Node(capability="x.do", operator="op"), {})
    assert got["secrets"] == {}

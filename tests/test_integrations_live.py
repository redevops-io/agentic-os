"""The optional live wiring — env-backed credential resolution and go_live() driving a
ConnectPlan through a registry, tested with an injected fake adapter factory (so no
redevops-connectors import and no live call)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple

import pytest

from agentic_os.integrations import (
    CapabilityDimension,
    EnvSecretResolver,
    IntegrationManifest,
    KeywordReader,
    Support,
    go_live,
    live_registry,
    plan_from_request,
)


# ── env-backed credential resolution ────────────────────────────────────────────
def test_env_resolver_reads_the_provider_token():
    r = EnvSecretResolver(env={"SLACK_BOT_TOKEN": "xoxb-real"})
    assert r.resolve("slack:token") == {"access_token": "xoxb-real"}


def test_env_resolver_refuses_when_the_token_is_absent():
    with pytest.raises(KeyError, match="SLACK_BOT_TOKEN"):
        EnvSecretResolver(env={}).resolve("slack:token")


def test_env_resolver_extra_overrides_env():
    r = EnvSecretResolver(env={}, extra={"slack:token": {"access_token": "t"}})
    assert r.resolve("slack:token")["access_token"] == "t"


# ── a fake connector, shaped like the real one, injected via the factory ─────────
@dataclass(frozen=True)
class _Cap:
    name: str
    tier: int
    write: bool


@dataclass(frozen=True)
class _R:
    ok: bool
    provider_object_id: str = ""
    error: str = ""


@dataclass(frozen=True)
class _S:
    connected: bool
    detail: str = ""


@dataclass(frozen=True)
class _O:
    found: bool


@dataclass
class _FakeSlack:
    provider: str = "slack"
    _made: set = field(default_factory=set)

    def capabilities(self):
        return (_Cap("approval.request", 3, True),)

    def connect(self, config, credential_ref):
        return _S(connected=True, detail="fake slack")

    def execute(self, capability, request, envelope):
        if capability != "approval.request":
            return _R(ok=False, error="unsupported")
        if envelope is None:
            return _R(ok=False, error="envelope required")
        oid = "slack:approval.request:1"
        self._made.add(oid)
        return _R(ok=True, provider_object_id=oid)

    def observe(self, resource_ref):
        return _O(found=resource_ref in self._made)

    def health(self):
        return _S(connected=True)


def _fake_factory(resolver, *, transport: Optional[object] = None):
    return {"slack": lambda: _FakeSlack()}


def _slack_plan():
    manifest = IntegrationManifest(dimensions=(
        CapabilityDimension("approval.request", "slack", Support.EXECUTED, tier=3),
    ))
    return plan_from_request("approve in slack", reader=KeywordReader.default(),
                             manifest=manifest, confirmed_by="a", confirmed_at="t")


def test_live_registry_only_builds_providers_with_a_builder():
    reg = live_registry(("slack", "unknownprov"), resolver=EnvSecretResolver(env={}), factory=_fake_factory)
    assert reg.get("slack") is not None
    assert reg.get("unknownprov") is None


def test_go_live_connects_runs_and_reconciles_with_a_fake_provider():
    receipts, run, ex = go_live(
        _slack_plan(), resolver=EnvSecretResolver(env={"SLACK_BOT_TOKEN": "xoxb-x"}),
        requests={"approval.request": {"channel": "#ops"}}, factory=_fake_factory,
    )
    assert any(r.provider == "slack" and r.connected for r in receipts)
    assert run is not None and run.ok and run.reconciled
    assert ex.connectable
    step = next(s for s in ex.steps if s["capability"] == "approval.request")
    assert step["tier"] == 3 and step["status"] == "ok" and step["reconciled"] is True

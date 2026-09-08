"""Optional live wiring — register real ``redevops-connectors`` adapters and run a
ConnectPlan against live providers.

This is the ONE place agentic-os reaches for the connector package, and it does so
**lazily**: importing ``agentic_os.integrations`` never imports ``redevops_connectors``,
so the base install stays free of it (the plan's "provider dependencies install on
demand"). Install the plugin with ``pip install 'agentic-os[connectors]'``.

Credentials come from a ``SecretResolver``. :class:`EnvSecretResolver` reads the
conventional per-provider access-token env var (broker-backed in a real deployment). A
call only reaches a provider once you run it with a live transport — and a live call needs
a real access token (a Slack ``xoxb-…`` bot token, not a ``xoxe.`` rotation token).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from .connect_plan import ConnectPlan
from .execution import (
    AdapterRegistry,
    ConnectReceipt,
    Explanation,
    MissionRun,
    connect_providers,
    explain,
    run_test_mission,
)

#: provider id → env var carrying that provider's access token.
PROVIDER_TOKEN_ENV: Dict[str, str] = {
    "slack": "SLACK_BOT_TOKEN",
    "gmail": "GOOGLE_ACCESS_TOKEN",
    "google_calendar": "GOOGLE_ACCESS_TOKEN",
    "hubspot": "HUBSPOT_ACCESS_TOKEN",
    "stripe": "STRIPE_ACCESS_TOKEN",
}


@dataclass
class EnvSecretResolver:
    """Resolves ``"<provider>:token"`` refs to an access token from the environment. The
    material is read at the moment of use and never stored on an adapter or logged."""

    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    extra: Dict[str, Dict[str, str]] = field(default_factory=dict)

    def resolve(self, ref: str) -> Mapping[str, str]:
        if ref in self.extra:
            return self.extra[ref]
        provider = ref.split(":", 1)[0]
        var = PROVIDER_TOKEN_ENV.get(provider, "")
        token = self.env.get(var, "") if var else ""
        if not token:
            raise KeyError(
                f"no access token for {ref!r}"
                + (f" — set ${var}" if var else " — unknown provider"))
        return {"access_token": token}


#: A factory maps a provider id → a zero-arg builder that returns a live adapter.
AdapterFactory = Callable[[Any], Dict[str, Callable[[], Any]]]


def default_adapter_factory(resolver: Any, *, transport: Optional[object] = None) -> Dict[str, Callable[[], Any]]:
    """Lazily import ``redevops-connectors`` and return per-provider adapter builders,
    each wired to a live :class:`UrllibTransport` and ``resolver``. Raises a clear error
    if the ``[connectors]`` extra is not installed."""
    try:
        from redevops_connectors import UrllibTransport
        from redevops_connectors.providers import SlackAdapter
    except ImportError as e:  # pragma: no cover — exercised only without the extra
        raise ImportError(
            "live provider adapters require the connector plugin: "
            "pip install 'agentic-os[connectors]'") from e
    tp = transport or UrllibTransport()
    return {
        "slack": lambda: SlackAdapter(transport=tp, resolver=resolver, credential_ref="slack:token"),
    }


def live_registry(providers: Tuple[str, ...], *, resolver: Any,
                  factory: Optional[AdapterFactory] = None) -> AdapterRegistry:
    """Build an :class:`AdapterRegistry` holding a live adapter for each requested provider
    that has a builder. A provider with no builder is simply absent (the run then reports
    it as un-adaptered rather than failing to construct)."""
    builders = (factory or default_adapter_factory)(resolver)
    registry = AdapterRegistry()
    for provider in providers:
        build = builders.get(provider)
        if build is not None:
            registry.register(build())
    return registry


def go_live(
    plan: ConnectPlan, *, resolver: Any,
    requests: Optional[Mapping[str, Mapping[str, Any]]] = None,
    factory: Optional[AdapterFactory] = None,
) -> Tuple[Tuple[ConnectReceipt, ...], Optional[MissionRun], Explanation]:
    """Run a ConnectPlan against live providers: build the registry, connect each provider,
    then (if the plan is connectable) run the governed test Mission. Returns
    ``(connect_receipts, mission_run_or_None, explanation)``."""
    providers = tuple(sorted({s.provider for s in plan.connect_steps}))
    registry = live_registry(providers, resolver=resolver, factory=factory)
    receipts = connect_providers(
        plan, registry, credential_refs={p: f"{p}:token" for p in providers})
    run = run_test_mission(plan, registry, requests=requests) if plan.connectable else None
    return receipts, run, explain(plan, run)

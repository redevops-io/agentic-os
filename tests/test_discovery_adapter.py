"""Agentic OS public adapter for the published Discovery Runtime."""
from __future__ import annotations

from agentic_os import discovery


def test_discovery_adapter_reexports_the_published_runtime_surface():
    import discovery_runtime

    assert discovery.available() is True
    assert discovery.discovery_runtime_version() == discovery_runtime.__version__
    assert discovery.DiscoveryRuntime is discovery_runtime.DiscoveryRuntime
    assert discovery.Reading is discovery_runtime.Reading
    assert discovery.NotSealable is discovery_runtime.NotSealable


def test_build_runtime_is_a_thin_domain_supplied_constructor():
    rt = discovery.build_runtime(
        schema={"dimensions": ["customer", "action"]},
        objective="triage a customer workflow",
        readers=[],
    )

    assert isinstance(rt, discovery.DiscoveryRuntime)
    assert rt.schema == {"dimensions": ["customer", "action"]}
    assert rt.objective == "triage a customer workflow"
    assert rt.readers == []

"""Agentic OS public adapter for the published Discovery Runtime."""
from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True)
class _Signal:
    subject: str
    metric: str
    value: float


@dataclass(frozen=True)
class _Detection:
    metric: str
    severity: float


class _ThresholdDetector:
    def scan(self, store):
        out = []
        for subject, metric in store.keys():
            signal = store.latest(subject, metric)
            if signal is not None and signal.value > 0.5:
                out.append(_Detection(metric=metric, severity=signal.value))
        return out


def test_detector_runtime_runs_domain_supplied_signal_detectors():
    rt = discovery.build_detector_runtime(
        detectors=[_ThresholdDetector()],
        grants=["read:signals"],
        proposal_filter=lambda detection: detection.metric == "risk",
    )

    result = rt.cycle([
        _Signal("portfolio", "risk", 0.8),
        _Signal("portfolio", "cash", 0.2),
    ])

    assert rt.grants == ["read:signals"]
    assert result.detections == [_Detection(metric="risk", severity=0.8)]
    assert result.proposals == [_Detection(metric="risk", severity=0.8)]

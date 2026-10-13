"""Public adapter for the published Discovery Runtime.

Discovery remains owned by the standalone ``discovery-runtime`` package. This
module gives Agentic OS consumers a stable namespace without copying the engine
or teaching the runtime any domain semantics. Domains still supply their own
schema, readers and canonicalization policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable


class DiscoveryRuntimeUnavailable(ImportError):
    """Raised when the optional published Discovery Runtime cannot be imported."""


@dataclass(frozen=True)
class SignalDiscoveryResult:
    """Generic detector-loop result for signal-oriented discovery domains."""

    detections: list[Any]
    proposals: list[Any]


class SignalStore:
    """Minimal store protocol for deterministic signal detectors.

    Signals are expected to carry ``subject`` and ``metric`` attributes. The
    store intentionally does not interpret values; domain detectors own that.
    """

    def __init__(self, signals: Iterable[Any]) -> None:
        self._latest: dict[tuple[str, str], Any] = {}
        for signal in signals:
            self._latest[(str(signal.subject), str(signal.metric))] = signal

    def keys(self) -> list[tuple[str, str]]:
        return sorted(self._latest)

    def latest(self, subject: str, metric: str) -> Any | None:
        return self._latest.get((subject, metric))


class DetectorDiscoveryRuntime:
    """Domain-neutral shell for deterministic signal detectors.

    This is for numeric/event discovery domains that already have deterministic
    detector objects. Each detector supplies ``scan(store)`` and returns domain
    detections. The optional ``proposal_filter`` decides which detections are
    actionable; without it every detection is a proposal.
    """

    def __init__(self, *, detectors: Iterable[Any], grants: Iterable[str] = (),
                 proposal_filter: Callable[[Any], bool] | None = None) -> None:
        self.detectors = list(detectors)
        self.grants = list(grants)
        self.proposal_filter = proposal_filter

    def cycle(self, signals: Iterable[Any], handoff: bool = False) -> SignalDiscoveryResult:
        del handoff
        store = SignalStore(signals)
        detections: list[Any] = []
        for detector in self.detectors:
            detections.extend(detector.scan(store))
        if self.proposal_filter is None:
            proposals = list(detections)
        else:
            proposals = [d for d in detections if self.proposal_filter(d)]
        return SignalDiscoveryResult(detections=detections, proposals=proposals)


_EXPORTED_SYMBOLS = {
    "DiscoveryRuntime",
    "Reader",
    "Reading",
    "merge_readings",
    "draft_intent",
    "interpreted",
    "clarifications",
    "resolve",
    "seal",
    "digest",
    "NotSealable",
    "VerifiedIntent",
    "DecisionEvidence",
    "Unresolved",
    "Amendment",
    "IntentState",
}


def _runtime_module():
    try:
        import discovery_runtime
    except ModuleNotFoundError as exc:
        raise DiscoveryRuntimeUnavailable(
            "agentic_os.discovery requires the published discovery-runtime package. "
            "Install agentic-os with its runtime dependencies, or add discovery-runtime explicitly."
        ) from exc
    return discovery_runtime


def available() -> bool:
    """Whether the published Discovery Runtime can be imported in this environment."""
    try:
        _runtime_module()
    except DiscoveryRuntimeUnavailable:
        return False
    return True


def discovery_runtime_version() -> str:
    """Return the underlying discovery-runtime version string."""
    return str(getattr(_runtime_module(), "__version__", "unknown"))


def build_runtime(*, readers: list[Any], schema: Any = None, objective: str = "",
                  canonicalize: Any = None, fusion_policy: Any = None):
    """Construct a ``discovery_runtime.DiscoveryRuntime`` through the Agentic OS namespace.

    This is intentionally thin: it forwards only configuration supplied by the
    caller and never invents a domain objective, schema or reader.
    """
    runtime = _runtime_module()
    kwargs: dict[str, Any] = {"readers": readers, "schema": schema, "objective": objective}
    if canonicalize is not None:
        kwargs["canonicalize"] = canonicalize
    if fusion_policy is not None:
        kwargs["fusion_policy"] = fusion_policy
    return runtime.DiscoveryRuntime(**kwargs)


def build_detector_runtime(*, detectors: Iterable[Any], grants: Iterable[str] = (),
                           proposal_filter: Callable[[Any], bool] | None = None) -> DetectorDiscoveryRuntime:
    """Construct the signal-detector Discovery shell used by non-text domains."""
    return DetectorDiscoveryRuntime(detectors=detectors, grants=grants, proposal_filter=proposal_filter)


def __getattr__(name: str):
    if name in _EXPORTED_SYMBOLS:
        return getattr(_runtime_module(), name)
    raise AttributeError(name)


__all__ = [
    "DiscoveryRuntimeUnavailable",
    "DetectorDiscoveryRuntime",
    "SignalDiscoveryResult",
    "SignalStore",
    "available",
    "build_detector_runtime",
    "build_runtime",
    "discovery_runtime_version",
    *_EXPORTED_SYMBOLS,
]

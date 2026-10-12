"""Public adapter for the published Discovery Runtime.

Discovery remains owned by the standalone ``discovery-runtime`` package. This
module gives Agentic OS consumers a stable namespace without copying the engine
or teaching the runtime any domain semantics. Domains still supply their own
schema, readers and canonicalization policy.
"""
from __future__ import annotations

from typing import Any


class DiscoveryRuntimeUnavailable(ImportError):
    """Raised when the optional published Discovery Runtime cannot be imported."""


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


def __getattr__(name: str):
    if name in _EXPORTED_SYMBOLS:
        return getattr(_runtime_module(), name)
    raise AttributeError(name)


__all__ = [
    "DiscoveryRuntimeUnavailable",
    "available",
    "build_runtime",
    "discovery_runtime_version",
    *_EXPORTED_SYMBOLS,
]

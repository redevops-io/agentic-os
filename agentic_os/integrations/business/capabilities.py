"""The business-connector capability registry (plan §5).

A machine-readable providers × capabilities registry with an explicit status vocabulary, so the platform
never claims an integration merely because an API exists. This is the business-SaaS counterpart to the
external-agent capability audit (``agent_gateway/external/external_agent_capabilities.yaml``); it stays in
the Integration Plane by the extend-Integration-Plane decision, separate from the external-agent gateway.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple


class ConnectorCapabilityStatus(str, enum.Enum):
    VERIFIED = "VERIFIED"                      # contract-verified (fixtures/conformance); see `verification`
    AVAILABLE_UNVERIFIED = "AVAILABLE_UNVERIFIED"
    UNKNOWN = "UNKNOWN"
    UNSUPPORTED = "UNSUPPORTED"
    POLICY_BLOCKED = "POLICY_BLOCKED"

    @property
    def usable(self) -> bool:
        """Only VERIFIED capabilities may be wired into a Mission by default; everything else is treated
        as unavailable (fail-closed). AVAILABLE_UNVERIFIED is opt-in with caution, decided upstream."""
        return self is ConnectorCapabilityStatus.VERIFIED


@dataclass(frozen=True)
class ConnectorCapability:
    provider: str
    capability: str
    status: ConnectorCapabilityStatus
    tier: int = 0
    write: bool = False
    auth: str = ""
    verification: str = ""                    # "contract" | "live" | ""
    human_required: bool = False


def _default_path() -> Path:
    return Path(__file__).resolve().parent / "connector_capabilities.yaml"


def load_connector_capabilities(path: Optional[Path] = None) -> dict:
    text = (path or _default_path()).read_text()
    try:
        import yaml  # noqa: PLC0415
        return yaml.safe_load(text) or {}
    except Exception:
        # PyYAML should be present (it is a base dep); if not, fail loud rather than silently empty.
        raise


def provider_capabilities(provider: str, *, path: Optional[Path] = None) -> Tuple[ConnectorCapability, ...]:
    audit = load_connector_capabilities(path)
    node = (audit.get("providers", {}) or {}).get(provider)
    if not node:
        return ()
    auth = str(node.get("auth", ""))
    out = []
    for cap, spec in (node.get("capabilities", {}) or {}).items():
        spec = spec or {}
        out.append(ConnectorCapability(
            provider=provider, capability=cap,
            status=ConnectorCapabilityStatus(str(spec.get("status", "UNKNOWN"))),
            tier=int(spec.get("tier", 0)), write=bool(spec.get("write", False)), auth=auth,
            verification=str(spec.get("verification", "")),
            human_required=bool(spec.get("human_required", False))))
    return tuple(out)


def connector_capability(provider: str, capability: str, *,
                         path: Optional[Path] = None) -> ConnectorCapability:
    """Look up one capability. A provider/capability not in the registry is UNKNOWN (fail-closed) —
    unless the provider is listed as an unsupported priority provider, in which case UNSUPPORTED."""
    for c in provider_capabilities(provider, path=path):
        if c.capability == capability:
            return c
    audit = load_connector_capabilities(path)
    if provider in (audit.get("unsupported_priority_providers", []) or []):
        st = ConnectorCapabilityStatus.UNSUPPORTED
    else:
        st = ConnectorCapabilityStatus.UNKNOWN
    return ConnectorCapability(provider=provider, capability=capability, status=st)

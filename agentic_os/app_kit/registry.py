"""AppRegistry — binds an AppManifest to its Operator and enforces runtime-native invariants.

Registration fails closed: if any blocking conformance finding holds (an undeclared capability, a
phantom producer capability, a side-effecting capability with no verifier), ``register`` raises
``ManifestError`` and the app is not registered. This is the gate that lets apps be migrated en
masse — the registry, not review, refuses a non-conformant app.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from .conformance import Finding, blocking_findings, check_registration, summarize
from .manifest import AppManifest


class ManifestError(ValueError):
    """Raised when an app fails registration-time conformance."""


@dataclass(frozen=True)
class RegisteredApp:
    manifest: AppManifest
    operator: object            # a mission.operator_sdk.Operator
    findings: tuple             # the conformance findings recorded at registration


class AppRegistry:
    """Process-local registry of runtime-native apps."""

    def __init__(self) -> None:
        self._apps: Dict[str, RegisteredApp] = {}

    def register(self, manifest: AppManifest, operator) -> RegisteredApp:
        """Validate and register an app. Raises ``ManifestError`` on any blocking finding."""
        if manifest.name in self._apps:
            raise ManifestError(f"app '{manifest.name}' is already registered")
        findings = check_registration(manifest, operator)
        bad = blocking_findings(findings)
        if bad:
            raise ManifestError(summarize(findings))
        app = RegisteredApp(manifest=manifest, operator=operator, findings=tuple(findings))
        self._apps[manifest.name] = app
        return app

    def check(self, manifest: AppManifest, operator) -> List[Finding]:
        """Run the checks without registering (for a conformance report / dry run)."""
        return check_registration(manifest, operator)

    def app(self, name: str) -> RegisteredApp:
        return self._apps[name]

    def __contains__(self, name: str) -> bool:
        return name in self._apps

    def __iter__(self):
        return iter(self._apps.values())

    def names(self) -> List[str]:
        return sorted(self._apps)

    def capabilities(self) -> Dict[str, List[str]]:
        """app name -> sorted capability names it provides."""
        return {name: sorted(a.manifest.capability_names()) for name, a in self._apps.items()}


__all__ = ["AppRegistry", "RegisteredApp", "ManifestError"]

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
    external: bool = False      # registered through the external trust boundary (see AppRegistry.register_external)


class AppRegistry:
    """Process-local registry of runtime-native apps."""

    def __init__(self) -> None:
        self._apps: Dict[str, RegisteredApp] = {}

    def register(self, manifest: AppManifest, operator, *, external: bool = False) -> RegisteredApp:
        """Validate and register an app. Raises ``ManifestError`` on any blocking finding.

        ``external=False`` is for the apps this distribution ships (and its own tests): trusted code, whose unclassified side effects are reported but do not block.
        ``external=True`` is the trust boundary for an app that comes from outside (a portable, installed or imported app): see :meth:`register_external`."""
        if manifest.name in self._apps:
            raise ManifestError(f"app '{manifest.name}' is already registered")
        findings = check_registration(manifest, operator, external=external)
        bad = blocking_findings(findings)
        if bad:
            raise ManifestError(summarize(findings))
        app = RegisteredApp(manifest=manifest, operator=operator, findings=tuple(findings), external=external)
        self._apps[manifest.name] = app
        return app

    def register_external(self, manifest: AppManifest, operator) -> RegisteredApp:
        """Register an app that is not part of this distribution. Fails closed, at registration, before any of its capabilities can be planned or invoked:

        * every capability the operator can execute is declared in the manifest (nothing unclassified is reachable);
        * every side-effecting capability declares who decides (``approval_required`` or ``required_authority``) and the ``permissions`` a caller must hold;
        * the app's name is its operator's name and is not the name of a bundled app.

        This adds NO authorization system: approval is enforced where it already is (the operator's ``/invoke``, the mission runtime's gate, the agent gateway), and scope
        by the permissions the mission holds (``AGENTIC_OS_TENANT_LIMITS`` for a tenant caller). It refuses the app whose capabilities those checks could not classify."""
        return self.register(manifest, operator, external=True)

    def ensure(self, manifest: AppManifest, operator) -> RegisteredApp:
        """Idempotent register: register the app if absent, else return the existing registration.
        For boot-time registration where an app may be imported more than once in a process."""
        if manifest.name in self._apps:
            return self._apps[manifest.name]
        return self.register(manifest, operator)

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

"""Suite — a composition of runtime-native apps (plan §3.1/§13).

A *suite* is "packaged capabilities, Mission templates and playbooks; not necessarily a separate
agent service" (plan §13). The six enterprise domain agents (revenue / intelligence / finance /
customer-success / content / security-compliance) are suites over the canonical apps: a suite names
the apps it composes, and its capability + producer surface is the union of theirs. Enterprise-only
intelligence (Decision Flow Planner, discovery, governed bandit) plugs in at runtime through the
public overlay seams — it is not part of the composition declaration, which keeps the suite
definitions public and the apps the single source of capability truth.

A suite owns no capability or producer of its own; composing an app that is not runtime-native (not
registered) is a conformance failure, so a suite can never surface an ungoverned capability.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, Tuple

from .manifest import EnterpriseRequirement

SUITE_CONTRACT_VERSION = "suite/v1"


class SuiteError(ValueError):
    """Raised when a suite composes an app that is not registered (runtime-native)."""


@dataclass(frozen=True)
class Suite:
    """A named composition of canonical apps + the mission templates it offers."""

    name: str
    apps: Tuple[str, ...]                         # canonical app (manifest) names it composes
    mission_templates: Tuple[str, ...] = ()
    version: str = "0.1.0"
    enterprise: EnterpriseRequirement = EnterpriseRequirement.OPTIONAL
    description: str = ""

    def capabilities(self, app_registry) -> FrozenSet[str]:
        """The union of the composed apps' declared capabilities."""
        caps: set[str] = set()
        for app in self.apps:
            caps |= set(app_registry.app(app).manifest.capability_names())
        return frozenset(caps)

    def producers(self, app_registry) -> Tuple:
        """The producer bundle: every composed app's producers."""
        out = []
        for app in self.apps:
            out.extend(app_registry.app(app).manifest.producers)
        return tuple(out)


@dataclass
class RegisteredSuite:
    suite: Suite
    capabilities: FrozenSet[str]


class SuiteRegistry:
    """Registry of suites over an ``AppRegistry``. Registration fails closed if a composed app is not
    registered (runtime-native)."""

    def __init__(self, app_registry) -> None:
        self._apps = app_registry
        self._suites: dict = {}

    def register(self, suite: Suite) -> RegisteredSuite:
        if suite.name in self._suites:
            raise SuiteError(f"suite '{suite.name}' is already registered")
        missing = [a for a in suite.apps if a not in self._apps]
        if missing:
            raise SuiteError(
                f"suite '{suite.name}' composes app(s) not registered as runtime-native: {missing}")
        if not suite.apps:
            raise SuiteError(f"suite '{suite.name}' composes no apps")
        reg = RegisteredSuite(suite=suite, capabilities=suite.capabilities(self._apps))
        self._suites[suite.name] = reg
        return reg

    def suite(self, name: str) -> RegisteredSuite:
        return self._suites[name]

    def names(self) -> list:
        return sorted(self._suites)

    def __contains__(self, name: str) -> bool:
        return name in self._suites


__all__ = [
    "SUITE_CONTRACT_VERSION",
    "SuiteError",
    "Suite",
    "RegisteredSuite",
    "SuiteRegistry",
]

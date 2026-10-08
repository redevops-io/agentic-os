"""Registration-time conformance checks for a runtime-native app.

These are the *static* checks that can be made the moment an ``AppManifest`` is bound to its
Operator — the subset of N1-N11 that does not need a running mission. The registry calls
:func:`check_registration` and refuses to register an app with any hard failure; the full
dynamic suite (approval executes, verifier blocks commit, outcome→learner, egress canaries)
runs later against a live runtime in ``tests/conformance/``.

Pure: takes a manifest and the app's Operator, returns findings. No side effects.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .manifest import AppManifest


@dataclass(frozen=True)
class Finding:
    """One conformance observation. ``hard`` findings block registration when ``ok`` is False."""

    invariant: str      # the N-id or check name, e.g. "N4" / "phantom-capability"
    ok: bool
    detail: str
    hard: bool = True

    @property
    def blocking(self) -> bool:
        return self.hard and not self.ok


def _operator_capability_names(operator) -> frozenset[str]:
    """The set of capability names the Operator actually registers (reads its CapabilityManifest)."""
    manifest = getattr(operator, "manifest", None)
    caps = getattr(manifest, "capabilities", ()) if manifest is not None else ()
    return frozenset(getattr(c, "name", "") for c in caps)


def _spec_by_name(operator) -> dict:
    manifest = getattr(operator, "manifest", None)
    caps = getattr(manifest, "capabilities", ()) if manifest is not None else ()
    return {getattr(c, "name", ""): c for c in caps}


def check_registration(manifest: AppManifest, operator) -> List[Finding]:
    """Static N-invariant checks for binding ``manifest`` to ``operator``.

    Covers: capability referential integrity (N2 — the app only claims capabilities it registers),
    N4 (every side-effecting capability has a verifier), the phantom-capability check (N1/§3.4 —
    every capability a producer's candidates require must resolve), and verifier integrity.
    """
    findings: List[Finding] = []
    registered = _operator_capability_names(operator)
    specs = _spec_by_name(operator)
    declared = manifest.capability_names()

    # N2 (partial): every CapabilityRef the manifest claims must be a real registered capability.
    for name in sorted(declared):
        findings.append(
            Finding(
                "N2",
                name in registered,
                f"capability '{name}' is declared in the manifest"
                + ("" if name in registered else " but is NOT registered on the operator"),
            )
        )

    # phantom-capability (N1 / §3.4): every capability a producer may emit must resolve on the
    # operator, otherwise a selected candidate could never be executed.
    for producer in manifest.producers:
        for cap in producer.emits_capabilities:
            findings.append(
                Finding(
                    "phantom-capability",
                    cap in registered,
                    f"producer '{producer.name}' emits capability '{cap}'"
                    + ("" if cap in registered else " which is NOT registered on the operator (phantom)"),
                )
            )

    # N4: every side-effecting capability (per its CapabilitySpec) must have a registered verifier.
    verified = manifest.verified_capabilities()
    for name, spec in sorted(specs.items()):
        if getattr(spec, "side_effecting", False):
            findings.append(
                Finding(
                    "N4",
                    name in verified,
                    f"side-effecting capability '{name}'"
                    + (" has a verifier" if name in verified else " has NO verifier (read-back required)"),
                )
            )

    # verifier integrity: a verifier must point at a capability that exists on the operator.
    for verifier in manifest.verifiers:
        findings.append(
            Finding(
                "verifier-integrity",
                verifier.capability in registered,
                f"verifier '{verifier.name or verifier.capability}' targets capability "
                f"'{verifier.capability}'"
                + ("" if verifier.capability in registered else " which is NOT registered"),
            )
        )

    return findings


def blocking_findings(findings: List[Finding]) -> List[Finding]:
    return [f for f in findings if f.blocking]


def summarize(findings: List[Finding]) -> str:
    bad = blocking_findings(findings)
    if not bad:
        return f"conformance: {len(findings)} checks, all passing"
    lines = [f"conformance: {len(bad)} blocking failure(s) of {len(findings)} checks:"]
    lines += [f"  - [{f.invariant}] {f.detail}" for f in bad]
    return "\n".join(lines)


__all__ = ["Finding", "check_registration", "blocking_findings", "summarize"]

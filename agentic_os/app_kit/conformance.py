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


def bundled_app_names() -> frozenset[str]:
    """The names of the apps this distribution ships. An app that is NOT one of them may not take one of these names: the permissions plane, the gates and the approval
    classification are keyed by app name, so a foreign app under a bundled name would inherit what was decided about the bundled one."""
    names: set[str] = set()
    try:
        from ..tool_actions import TOOL_ACTIONS  # noqa: PLC0415
        names |= set(TOOL_ACTIONS)
    except Exception:  # noqa: BLE001 - the list is a guard; an unreadable table must not hide the other sources
        pass
    try:
        from pathlib import Path  # noqa: PLC0415
        names |= {p.stem for p in (Path(__file__).parent / "capability_manifests").glob("*.json")}
    except OSError:
        pass
    return frozenset(names)


def check_registration(manifest: AppManifest, operator, *, external: bool = False) -> List[Finding]:
    """Static N-invariant checks for binding ``manifest`` to ``operator``.

    Covers: capability referential integrity (N2 — the app only claims capabilities it registers, and registers only capabilities it claims),
    N4 (every side-effecting capability has a verifier), the phantom-capability check (N1/§3.4 —
    every capability a producer's candidates require must resolve), and verifier integrity.

    ``external=True`` is the trust boundary for an application that is not part of this distribution (a portable app, an installed or imported one): its side-effecting
    capabilities must be classified for authorization (N12/N13) and its name must be its own (N14). For a bundled app the same observations are reported but do not block,
    so they stay visible without changing what already ships.
    """
    findings: List[Finding] = []
    registered = _operator_capability_names(operator)
    specs = _spec_by_name(operator)
    declared = manifest.capability_names()

    # N2 (the other direction): a capability the operator can EXECUTE but the manifest does not declare is unclassified. Everything that reads the manifest (the catalog,
    # the console, the permission derivation, an approver) would not know it exists, yet it is reachable on the operator. Refused, not ignored.
    for name in sorted(registered - declared):
        if name:
            findings.append(Finding("N2-undeclared", False, f"capability '{name}' is registered on the operator but NOT declared in the manifest (unclassified)"))

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

    # N12/N13 (authorization classification of side effects). A side-effecting capability must say WHO MAY CALL IT and WHO DECIDES:
    #  * N12: a human decides (approval_required) or a control point does (required_authority: the execution authorization the delegated-authority gate checks);
    #  * N13: it declares `permissions`, because a mission is offered a capability only when it holds every permission the capability lists, and a capability that lists
    #    none is offered to every mission, including one whose tenant was granted nothing.
    for name, spec in sorted(specs.items()):
        if not getattr(spec, "side_effecting", False):
            continue
        decided = bool(getattr(spec, "approval_required", False) or list(getattr(spec, "required_authority", None) or []))
        findings.append(Finding("N12", decided, f"side-effecting capability '{name}' "
                               + ("declares who decides (approval_required or required_authority)" if decided else "declares neither approval_required nor required_authority (unclassified)"),
                               hard=external))
        scoped = bool(list(getattr(spec, "permissions", None) or []))
        findings.append(Finding("N13", scoped, f"side-effecting capability '{name}' "
                               + ("declares the permissions a caller must hold" if scoped else "declares no permissions: it would be offered to every mission, whatever its authority"),
                               hard=external))

    # N14: the name is the identity the gates, the grants and the approvals are keyed by. The operator and the manifest must agree on it, and a foreign app may not
    # take the name of one this distribution ships.
    op_name = getattr(operator, "name", None)
    if op_name is not None:
        findings.append(Finding("N14", op_name == manifest.name, f"operator name '{op_name}' " + ("matches" if op_name == manifest.name else f"differs from the manifest name '{manifest.name}'"),
                                hard=external))
    if external:
        clash = manifest.name in bundled_app_names()
        findings.append(Finding("N14", not clash, f"app name '{manifest.name}' " + ("is not one of this distribution's apps" if not clash else "is the name of a bundled app: a foreign app may not take it")))

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


__all__ = ["Finding", "check_registration", "blocking_findings", "summarize", "bundled_app_names"]

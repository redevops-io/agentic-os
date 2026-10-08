"""App Kit foundation: manifest + registry + registration-time conformance."""
from __future__ import annotations

import pytest

from agentic_os.app_kit import (
    AppManifest,
    AppRegistry,
    CapabilityRef,
    CoreRequirement,
    IntegrationLevel,
    ManifestError,
    OutcomeKind,
    PrivacyProfile,
    ProducerRef,
    VerifierRef,
    blocking_findings,
    check_registration,
)
from agentic_os.governance.classification import DataClassification
from agentic_os.mission.operator_sdk import Operator, capability


def _noop(inputs):  # a trivial capability handler
    return {"ok": True}


def _operator(name="demo", *, side_effecting_caps=(), read_caps=()):
    caps = [capability(c, _noop) for c in read_caps]
    caps += [capability(c, _noop, side_effecting=True, approval_required=True) for c in side_effecting_caps]
    return Operator(name, caps)


def _manifest(**over):
    base = dict(
        name="demo",
        capabilities=(CapabilityRef("demo.read"), CapabilityRef("demo.write")),
        producers=(ProducerRef("demo_producer", emits_capabilities=("demo.write",)),),
        verifiers=(VerifierRef("demo.write", name="read_back"),),
    )
    base.update(over)
    return AppManifest(**base)


# ── manifest is pure data ────────────────────────────────────────────────────

def test_manifest_helpers():
    m = _manifest()
    assert m.capability_names() == {"demo.read", "demo.write"}
    assert m.producer_capabilities() == {"demo.write"}
    assert m.verified_capabilities() == {"demo.write"}
    assert m.license == "AGPL-3.0-or-later"
    assert m.contract_version == "app-manifest/v1"


def test_manifest_defaults_are_frozen():
    m = AppManifest(name="x")
    with pytest.raises(Exception):
        m.name = "y"  # type: ignore[misc]  frozen dataclass


def test_core_requirement_and_privacy_profile():
    m = _manifest(
        required_cores=(CoreRequirement("lago", IntegrationLevel.L2_GOVERNED_FORK),),
        privacy=PrivacyProfile(data_classes=(DataClassification.CUSTOMER_CONFIDENTIAL,)),
        outcome_kinds=(OutcomeKind("demo.write", unit="count", expected_delay_s=3600.0),),
    )
    assert m.required_cores[0].integration_level is IntegrationLevel.L2_GOVERNED_FORK
    assert DataClassification.CUSTOMER_CONFIDENTIAL in m.privacy.data_classes
    assert m.outcome_kinds[0].expected_delay_s == 3600.0


# ── registry: happy path ─────────────────────────────────────────────────────

def test_register_conformant_app():
    reg = AppRegistry()
    op = _operator(read_caps=("demo.read",), side_effecting_caps=("demo.write",))
    app = reg.register(_manifest(), op)
    assert "demo" in reg
    assert reg.app("demo").manifest.name == "demo"
    assert reg.capabilities()["demo"] == ["demo.read", "demo.write"]
    assert not blocking_findings(list(app.findings))


def test_double_register_rejected():
    reg = AppRegistry()
    op = _operator(read_caps=("demo.read",), side_effecting_caps=("demo.write",))
    reg.register(_manifest(), op)
    with pytest.raises(ManifestError, match="already registered"):
        reg.register(_manifest(), op)


# ── registry: the invariants fail closed ─────────────────────────────────────

def test_undeclared_capability_rejected():
    # manifest claims demo.write but the operator never registers it
    reg = AppRegistry()
    op = _operator(read_caps=("demo.read",))  # no demo.write
    with pytest.raises(ManifestError, match="NOT registered"):
        reg.register(_manifest(), op)


def test_phantom_producer_capability_rejected():
    # producer emits a capability that no operator provides -> phantom
    reg = AppRegistry()
    op = _operator(read_caps=("demo.read", "demo.write"))  # both read, none side-effecting
    m = _manifest(
        producers=(ProducerRef("p", emits_capabilities=("demo.ghost",)),),
        verifiers=(),  # no side-effecting caps now, so N4 is satisfied
    )
    with pytest.raises(ManifestError, match="phantom"):
        reg.register(m, op)


def test_side_effecting_without_verifier_rejected():
    # demo.write is side-effecting but no verifier declared -> N4 fails
    reg = AppRegistry()
    op = _operator(read_caps=("demo.read",), side_effecting_caps=("demo.write",))
    m = _manifest(verifiers=())
    with pytest.raises(ManifestError, match="NO verifier"):
        reg.register(m, op)


def test_verifier_targets_unknown_capability_rejected():
    reg = AppRegistry()
    op = _operator(read_caps=("demo.read",), side_effecting_caps=("demo.write",))
    m = _manifest(verifiers=(VerifierRef("demo.write"), VerifierRef("demo.nope")))
    with pytest.raises(ManifestError, match="NOT registered"):
        reg.register(m, op)


def test_read_only_app_needs_no_verifier():
    reg = AppRegistry()
    op = _operator(read_caps=("demo.read", "demo.summary"))
    m = AppManifest(
        name="ro",
        capabilities=(CapabilityRef("demo.read"), CapabilityRef("demo.summary")),
    )
    app = reg.register(m, op)  # no side effects, no verifier required
    assert app.manifest.name == "ro"


# ── dry-run check does not register ──────────────────────────────────────────

def test_check_is_nondestructive():
    reg = AppRegistry()
    op = _operator(read_caps=("demo.read",))  # missing demo.write -> would fail
    findings = reg.check(_manifest(), op)
    assert blocking_findings(findings)          # it reports the failure
    assert "demo" not in reg                     # but nothing was registered

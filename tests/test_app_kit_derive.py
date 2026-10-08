"""manifest_from_operator derives capabilities + verifiers from a live operator's specs."""
from __future__ import annotations

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.app_kit import AppRegistry, manifest_from_operator
from agentic_os.app_kit.manifest import CoreRequirement, IntegrationLevel, ProducerRef
from agentic_os.mission.operator_sdk import Operator, capability


def _operator():
    return Operator("demo", [
        capability("demo.read", lambda i: {"x": 1}, provides=["x"]),
        capability("demo.write", lambda i: {"done": True}, provides=["done"],
                   outputs={"done": "bool"}, side_effecting=True),
        capability("demo.publish", lambda i: {"url": "u"}, provides=["published"],
                   side_effecting=True),   # no outputs -> verifier falls back to provides
    ])


def test_capabilities_and_verifiers_derived_from_specs():
    m = manifest_from_operator(_operator())
    assert m.name == "demo"
    assert m.capability_names() == {"demo.read", "demo.write", "demo.publish"}
    # only side-effecting capabilities get verifiers (N4)
    assert m.verified_capabilities() == {"demo.write", "demo.publish"}
    verifier_names = {v.capability: v.name for v in m.verifiers}
    assert verifier_names["demo.write"] == "composite:done"      # first declared output
    assert verifier_names["demo.publish"] == "composite:published"  # falls back to provides


def test_risk_tiers_default_and_override():
    m = manifest_from_operator(_operator(), risk_tiers={"demo.publish": RiskTier.CONSEQUENTIAL})
    by_name = {c.name: c.risk_tier for c in m.capabilities}
    assert by_name["demo.read"] is RiskTier.READ                 # not side-effecting
    assert by_name["demo.write"] is RiskTier.BOUNDED_WRITE        # side-effecting default
    assert by_name["demo.publish"] is RiskTier.CONSEQUENTIAL      # overridden


def test_derived_manifest_registers_conformantly():
    op = _operator()
    m = manifest_from_operator(op, producers=(ProducerRef("p", emits_capabilities=("demo.write",)),),
                               required_cores=(CoreRequirement("core", IntegrationLevel.L1_EXTENSION),))
    reg = AppRegistry()
    app = reg.register(m, op)     # side-effecting caps already have verifiers -> passes N4
    assert app.manifest.name == "demo"

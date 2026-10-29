"""DR-04: no operator is exempt from the operator seam BY NAME.

The kernel's own plane operators (the customer-interaction layer, the Agent Gateway's social and external-agent operators) are governed by those planes, not by an app's tool table. That exemption used to be a set
of operator NAMES, so any operator someone chose to call ``social`` was never gated. It is now trusted metadata about a specific operator OBJECT and its specific capabilities, set by the three kernel modules
that build them (``app_gate.kernel_governed``, which refuses any other caller), and the walk test pins exactly those registrations.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from agentic_os import app_gate
from agentic_os.mission.operator_sdk import Operator, capability

ROOT = pathlib.Path(__file__).resolve().parents[1]
OLD_NAMES = ("social", "interaction", "external-agent")


@pytest.fixture(autouse=True)
def _perm(monkeypatch):
    for k in ("CR_PERMISSIONS", "AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")


@pytest.mark.parametrize("name", OLD_NAMES + ("Social", "social ", "agentic-os", "kernel", "gateway"))
def test_a_user_chosen_operator_name_never_obtains_an_exemption(name):
    ran = []
    op = Operator(name, [capability("x.write", lambda i: (ran.append(1), {"ok": 1})[1], side_effecting=True)])
    out = op.invoke("x.write", {})
    assert out["status"] == "denied" and out["executed"] is False and ran == []
    assert app_gate.operator_denial(name, "x.write") is not None               # not even by the bare name


def test_the_by_name_registry_is_gone():
    assert not hasattr(app_gate, "KERNEL_OPERATORS")


def test_the_real_kernel_plane_operators_still_run_under_permissions():
    from agentic_os.agent_gateway.social.operator import SocialOperator
    from agentic_os.agent_gateway.social.contracts import ActionClass
    soc = SocialOperator(provider=type("P", (), {"provider": "p"})())
    cap = ActionClass.PUBLISH_OWNED_CHANNEL.value
    assert app_gate.operator_denial("social", cap, operator=soc.operator) is None
    assert app_gate.operator_denial("social", cap) is not None                  # the object matters, the name does not


def test_the_exemption_is_per_capability_not_per_operator(monkeypatch):
    from agentic_os.agent_gateway.social.operator import SocialOperator
    from agentic_os.agent_gateway.social.contracts import ActionClass
    soc = SocialOperator(provider=type("P", (), {"provider": "p"})())
    ran = []
    soc.operator._handlers["social.added_later"] = lambda i: (ran.append(1), {"ok": 1})[1]      # a capability added after the kernel registered the operator
    assert app_gate.operator_denial("social", "social.added_later", operator=soc.operator) is not None
    assert app_gate.operator_denial("social", ActionClass.PUBLISH_OWNED_CHANNEL.value, operator=soc.operator) is None


def test_a_second_operator_object_with_the_same_name_and_capabilities_is_not_exempt():
    from agentic_os.agent_gateway.social.operator import SocialOperator
    from agentic_os.agent_gateway.social.contracts import ActionClass
    soc = SocialOperator(provider=type("P", (), {"provider": "p"})())
    cap = ActionClass.PUBLISH_OWNED_CHANNEL.value
    twin = Operator("social", [capability(cap, lambda i: {"ok": 1}, side_effecting=True)])
    assert app_gate.operator_denial("social", cap, operator=twin) is not None and twin.invoke(cap, {})["status"] == "denied"
    assert app_gate.operator_denial("social", cap, operator=soc.operator) is None


def test_only_the_kernel_plane_modules_can_register_an_exemption():
    op = Operator("whatever", [capability("w.x", lambda i: {"ok": 1})])
    with pytest.raises(app_gate.GateStartupError, match="kernel plane"):
        app_gate.kernel_governed(op, "I would like to be exempt")
    assert app_gate.operator_denial("whatever", "w.x", operator=op) is not None


def test_the_exact_registrations_are_pinned():
    """Exactly three modules register, each once, each with a reason; a new registration is a deliberate change to this list."""
    sites = []
    for f in sorted((ROOT / "agentic_os").rglob("*.py")):
        tree = ast.parse(f.read_text())
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and ((isinstance(n.func, ast.Attribute) and n.func.attr == "kernel_governed") or (isinstance(n.func, ast.Name) and n.func.id == "kernel_governed")):
                sites.append(str(f.relative_to(ROOT)))
    assert sorted(sites) == ["agentic_os/agent_gateway/external/operator.py", "agentic_os/agent_gateway/social/operator.py", "agentic_os/interaction/operator.py"]
    assert app_gate.KERNEL_GOVERNED_MODULES == {"agentic_os.agent_gateway.external.operator", "agentic_os.agent_gateway.social.operator", "agentic_os.interaction.operator"}


def test_the_interaction_operator_built_by_the_kernel_runs_under_permissions():
    from agentic_os.interaction.operator import build_interaction_operator
    op = build_interaction_operator(object())
    for cap in op.manifest.capabilities:
        assert app_gate.operator_denial("interaction", cap.name, operator=op) is None, cap.name

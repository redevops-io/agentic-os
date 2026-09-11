"""Action sandbox — Phase 6 (plan §8)."""
from __future__ import annotations

import sys

from agentic_os.overlays import Principal
from agentic_os.agent_gateway import (
    AgentGateway, GatewayPrincipal, GatewayRequest, GatewayStatus, NullSandbox, SandboxAction,
    SandboxSpec, SubprocessSandbox, register_sandbox_capability)
from agentic_os.agent_gateway.registry import CapabilityRegistry


PY = sys.executable


def test_null_sandbox_fails_closed():
    obs = NullSandbox().execute(SandboxSpec(argv=(PY, "-c", "print(1)")), SandboxAction())
    assert obs.ok is False and "no sandbox runtime configured" in obs.error


def test_subprocess_sandbox_denies_unallowlisted_commands():
    sbx = SubprocessSandbox(allow_commands=frozenset())          # deny-all by default
    obs = sbx.execute(SandboxSpec(argv=(PY, "-c", "print(1)")), SandboxAction())
    assert obs.ok is False and "not allowlisted" in obs.error


def test_subprocess_sandbox_runs_allowlisted_command():
    sbx = SubprocessSandbox(allow_commands=frozenset({PY}))
    obs = sbx.execute(SandboxSpec(argv=(PY, "-c", "print('hi')")), SandboxAction())
    assert obs.ok and obs.exit_code == 0 and obs.stdout.strip() == "hi"
    assert sbx.events[-1]["event"] == "executed"                 # complete event log


def test_subprocess_sandbox_has_no_ambient_environment():
    sbx = SubprocessSandbox(allow_commands=frozenset({PY}))
    obs = sbx.execute(SandboxSpec(argv=(PY, "-c", "import os;print(os.environ.get('HOME','none'))"),
                                  env={}), SandboxAction())
    assert obs.ok and obs.stdout.strip() == "none"               # HOME not injected ⇒ no ambient creds


def test_subprocess_sandbox_enforces_wall_time():
    sbx = SubprocessSandbox(allow_commands=frozenset({PY}))
    obs = sbx.execute(SandboxSpec(argv=(PY, "-c", "import time;time.sleep(5)"), wall_time_s=0.3),
                      SandboxAction())
    assert obs.ok is False and "wall_time exceeded" in obs.error


def test_subprocess_sandbox_collects_declared_artifacts():
    sbx = SubprocessSandbox(allow_commands=frozenset({PY}))
    obs = sbx.execute(SandboxSpec(argv=(PY, "-c", "open('out.txt','w').write('result')")),
                      SandboxAction(collect=("out.txt",)))
    assert obs.ok and obs.artifacts["out.txt"] == "result"


def test_input_file_path_escape_is_blocked():
    sbx = SubprocessSandbox(allow_commands=frozenset({PY}))
    obs = sbx.execute(SandboxSpec(argv=(PY, "-c", "print(1)")),
                      SandboxAction(input_files={"../escape.txt": "x"}))
    assert obs.ok is False and "path escape blocked" in obs.error


def test_sandbox_execute_capability_is_mandatory_gated():
    gp = GatewayPrincipal(Principal("agent", "service", (), "acme"))
    reg = register_sandbox_capability(CapabilityRegistry(), SubprocessSandbox(frozenset({PY})))
    gw = AgentGateway(registry=reg, authorize=lambda p, perm: perm == "sandbox.execute")
    # CRITICAL + MANDATORY approval ⇒ pending without approval, no execution
    pending = gw.invoke(GatewayRequest(gp, "sandbox.execute", {"argv": [PY, "-c", "print(1)"]}))
    assert pending.status is GatewayStatus.PENDING_APPROVAL

    class _Approve:
        def is_satisfied(self, request, manifest): return True
    gw.approvals = _Approve()
    ok = gw.invoke(GatewayRequest(gp, "sandbox.execute", {"argv": [PY, "-c", "print('go')"]}))
    assert ok.status is GatewayStatus.OK and ok.output["stdout"].strip() == "go"

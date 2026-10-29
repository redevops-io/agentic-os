"""What an external caller can learn from a failing capability.

A handler's exception message and a contained child's stderr can carry paths, connection strings or tracebacks. The gateway returns only a short machine code to the client (``client_safe_error``) and keeps the detail in the server log. Errors a handler declares itself (``HandlerResult(ok=False, error=...)``) are written for the caller and pass through unchanged.

The sandbox tests also pin the child's environment: ``sandbox.execute`` must not let an ambient credential of the parent process reach the confined child.
"""
from __future__ import annotations

import json
import logging
import os
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).parent))

import pytest                                                          # noqa: E402

from agentic_os.agent_gateway import (                                 # noqa: E402
    AgentGateway, CapabilityRegistry, DevTokenVerifier, GatewayPrincipal, GatewayRequest, GatewayStatus,
    HandlerResult, InboxApprovalStore, McpGatewayBridge, register_sandbox_capability)
from agentic_os.agent_gateway.contracts import CapabilityKind, CapabilityManifest, DataClass  # noqa: E402
from agentic_os.agent_gateway.registry import client_safe_error        # noqa: E402
from agentic_os.overlays import Principal                              # noqa: E402

DETAIL = ("/srv/internal/prod.sqlite", "SYNTH-PW-123", "Traceback", "/Users/")


def _read_cap(name):
    return CapabilityManifest(name, "reads synthetic data", CapabilityKind.DIRECT, permissions=("data.read",),
                              data_classes=(DataClass.INTERNAL,))


def _bridge(handler, name="boom.read"):
    reg = CapabilityRegistry()
    reg.register(_read_cap(name), handler)
    verifier = DevTokenVerifier()
    token = verifier.issue("alice", tenant="t1", grants=("data.read",))
    gw = AgentGateway(registry=reg, authorize=verifier.authorize)
    return McpGatewayBridge(gw, verifier), token


# ── the helper ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("message,expected", [
    ("duration_exceeded", "duration_exceeded"),
    ("contained_failure(rc=1): Traceback (most recent call last): /Users/x/y.py", "contained_failure(rc=1)"),
    ("memory_ceiling_unenforceable: cannot apply RLIMIT_AS", "memory_ceiling_unenforceable"),
    ("db at /srv/internal/prod.sqlite refused: password=SYNTH-PW-123", "capability_error"),
    ("", "capability_error"),
])
def test_client_safe_error_keeps_a_leading_code_and_drops_the_text(message, expected):
    assert client_safe_error(RuntimeError(message)) == expected


def test_the_detail_is_logged_server_side(caplog):
    with caplog.at_level(logging.WARNING, logger="agentic_os.agent_gateway"):
        client_safe_error(RuntimeError("db at /srv/internal/prod.sqlite"), where="boom.read")
    assert "boom.read" in caplog.text and "/srv/internal/prod.sqlite" in caplog.text


# ── through the gateway ────────────────────────────────────────────────────────────
def test_an_exception_message_never_reaches_the_client():
    def boom(req, env):
        raise RuntimeError("db at /srv/internal/prod.sqlite refused: password=SYNTH-PW-123")
    bridge, token = _bridge(boom)
    view = bridge.call_tool(token, "boom.read", {})
    assert view["status"] == "error" and view["error"] == "capability_error"
    assert not any(d in json.dumps(view) for d in DETAIL)


def test_a_declared_error_passes_through_as_written():
    bridge, token = _bridge(lambda req, env: HandlerResult(ok=False, error="declared error for the caller"))
    assert bridge.call_tool(token, "boom.read", {})["error"] == "declared error for the caller"


# ── the sandbox path ───────────────────────────────────────────────────────────────
def _sandbox_bridge():
    # max_memory_mb=0 asks for no address-space ceiling, which macOS cannot apply; the env scrub does not depend on it.
    from agentic_os.mission.membrane import LocalContainmentSandbox
    from runtime_contracts.models import ExecutionConstraint
    reg = CapabilityRegistry()
    register_sandbox_capability(reg, LocalContainmentSandbox(
        constraint=ExecutionConstraint(max_memory_mb=0, max_duration_seconds=10, max_processes=0)))
    verifier = DevTokenVerifier()
    token = verifier.issue("frank", tenant="t1", grants=("sandbox.execute",))
    store = InboxApprovalStore()
    return McpGatewayBridge(AgentGateway(registry=reg, authorize=verifier.authorize, approvals=store), verifier), token, store


def _run(target):
    bridge, token, store = _sandbox_bridge()
    args = {"capability": target, "inputs": {}}
    first = bridge.call_tool(token, "sandbox.execute", dict(args))
    assert first["status"] == "pending_approval"
    store.approve(store.pending()[0]["id"], by="manager")
    return bridge.call_tool(token, "sandbox.execute", dict(args))


def test_a_failing_sandbox_target_returns_a_code_not_its_stderr():
    view = _run("_gateway_probe_targets:raises_with_detail")
    assert view["status"] == "error" and view["error"].startswith("contained_failure")
    assert not any(d in json.dumps(view) for d in DETAIL)


def test_a_missing_module_does_not_disclose_server_paths():
    view = _run("no_such_module_xyz:fn")
    assert view["status"] == "error" and "/" not in view["error"]


def test_the_sandboxed_child_does_not_inherit_the_parents_environment(monkeypatch):
    monkeypatch.setenv("GATEWAY_TEST_AMBIENT_SECRET", "SYNTH-AMBIENT-999")
    view = _run("_gateway_probe_targets:env_probe")
    assert view["status"] == "ok", view
    assert view["output"]["has_ambient_secret"] is False
    assert "SYNTH-AMBIENT-999" not in json.dumps(view)
    assert not {"HOME", "USER", "SHELL"} & set(view["output"]["env_keys"])
    assert not [k for k in view["output"]["env_keys"] if k.startswith(("AWS_", "GITHUB_", "ANTHROPIC_", "OPENAI_"))]

"""What an external caller can learn from a failing gateway call: only a short code from a FIXED set.

Builds on tests/test_agent_gateway_error_text.py. Three further properties:

* the code is never parsed out of an arbitrary exception message (an allow-list of public codes, or an explicit ``public_code`` on the exception);
* a fault in a gateway seam (authorizer, authority, budget, approvals, egress, audit) that is raised OUTSIDE the handler ``try`` also comes back as a
  short fixed code on every adapter, and the call fails closed (the handler does not run);
* the server log line carries the request id (so a report can be matched to it) and cannot be forged by a newline in the exception text.

The FastMCP transport is covered separately, over a real HTTP server, in tests/test_agent_gateway_fastmcp_http.py.
"""
from __future__ import annotations

import json
import logging

import pytest

from agentic_os.agent_gateway import (
    AgentGateway, CapabilityRegistry, DevTokenVerifier, GatewayRequest, GatewayStatus, HandlerResult, McpGatewayBridge)
from agentic_os.agent_gateway.capabilities import build_read_registry
from agentic_os.agent_gateway.contracts import ApprovalPolicy, CapabilityKind, CapabilityManifest, DataClass, RiskTier
from agentic_os.agent_gateway.protocols.mcp import MCPEndpoint
from agentic_os.agent_gateway.protocols.rest import RestGatewayAdapter
from agentic_os.agent_gateway.registry import client_safe_error
from agentic_os.agent_gateway.write_capabilities import build_write_registry

SECRET = "SYNTH-SECRET-777"


# ── F1: a code comes from a fixed set, never from the message ──────────────────────────────────
@pytest.mark.parametrize("message", [
    "hunter2secretkey",
    "prod_db_password_abc123",
    "a3f9c2e1b7d84a6f9c2e1b7d84a6f9c2",
    "internal_host_10_0_0_5: connection refused",
    "postgres://user:SYNTH@db.internal/prod",
    "smtp://u:SYNTH@mail.internal refused",
    "wrapper",
    "contained_failure(rc=abc): x",
    "contained_failure(rc=1)extra",
    "duration_exceeded_for_host_10_0_0_5",
    "",
])
def test_an_arbitrary_message_never_becomes_the_code(message):
    assert client_safe_error(RuntimeError(message)) == "capability_error"


@pytest.mark.parametrize("message,expected", [
    ("duration_exceeded", "duration_exceeded"),
    ("memory_ceiling_unenforceable: this platform cannot apply an address-space limit", "memory_ceiling_unenforceable"),
    ("execution_class_denied:laptop", "execution_class_denied"),
    ("contained_failure(rc=1): Traceback (most recent call last): /Users/x/y.py", "contained_failure(rc=1)"),
    ("contained_failure(rc=-9): killed", "contained_failure(rc=-9)"),
])
def test_the_public_codes_are_kept(message, expected):
    assert client_safe_error(RuntimeError(message)) == expected


def test_an_exception_can_name_its_public_code_only_from_the_fixed_set():
    class Typed(RuntimeError):
        public_code = "duration_exceeded"

    class Sneaky(RuntimeError):
        public_code = "hunter2secretkey"

    assert client_safe_error(Typed("anything at /srv/x")) == "duration_exceeded"
    assert client_safe_error(Sneaky("anything at /srv/x")) == "capability_error"


def test_a_hostile_str_does_not_break_the_error_path():
    class Hostile(Exception):
        def __str__(self):
            raise RuntimeError("str failed " + SECRET)
    assert client_safe_error(Hostile()) == "capability_error"


def _read_cap(name="boom.read"):
    return CapabilityManifest(name, "reads synthetic data", CapabilityKind.DIRECT, permissions=("data.read",), data_classes=(DataClass.INTERNAL,))


def _world(handler=None, **seams):
    reg = CapabilityRegistry()
    ran = []

    def default(req, env):
        ran.append(req.capability)
        return HandlerResult(ok=True, output={"v": 1})
    reg.register(_read_cap("boom.read"), handler or default)
    verifier = DevTokenVerifier()
    token = verifier.issue("alice", tenant="t1", grants=("data.read",))
    gw = AgentGateway(registry=reg, authorize=seams.pop("authorize", verifier.authorize), **seams)
    return gw, verifier, token, ran


def _adapters(gw, verifier, token):
    bridge = McpGatewayBridge(gw, verifier)
    rest = RestGatewayAdapter(gw, verifier)
    endpoint = MCPEndpoint(bridge)
    return {
        "bridge": lambda: bridge.call_tool(token, "boom.read", {}),
        "rest": lambda: rest.invoke("Bearer " + token, "boom.read", {}),
        "endpoint": lambda: endpoint.handle("Bearer " + token, "tools/call", {"name": "boom.read", "arguments": {}}),
    }


def test_every_adapter_returns_the_same_short_code_for_a_handler_fault():
    def boom(req, env):
        raise RuntimeError("hunter2secretkey")
    gw, verifier, token, _ = _world(boom)
    for name, call in _adapters(gw, verifier, token).items():
        view = call()
        assert view["status"] == "error" and view["error"] == "capability_error", name
        assert "hunter2secretkey" not in json.dumps(view), name


# ── F2: a fault in a gateway seam, outside the handler try ──────────────────────────────────────
class _Boom:
    """Raises the synthetic secret from whichever seam method is called."""
    def __init__(self, method, ok_value=None):
        setattr(self, method, self._raise)

    def _raise(self, *a, **k):
        raise RuntimeError("seam failed at /srv/x " + SECRET)


class _BudgetBoom(_Boom):
    def __init__(self):
        super().__init__("check")


class _EgressBoom(_Boom):
    def __init__(self):
        super().__init__("decide")


class _AuditBoom(_Boom):
    def __init__(self):
        super().__init__("record")


class _AuthorityBoom(_Boom):
    def __init__(self):
        super().__init__("authorize")


def _authorizer_boom(principal, permission):
    raise RuntimeError("authorizer failed at /srv/x " + SECRET)


@pytest.mark.parametrize("seam,kwargs,before_handler", [
    ("budget", {"budget": _BudgetBoom()}, True),
    ("egress", {"egress": _EgressBoom()}, False),
    ("audit", {"audit": _AuditBoom()}, False),
    ("authority", {"authority": _AuthorityBoom()}, True),
    ("authorizer", {"authorize": _authorizer_boom}, True),
])
def test_a_seam_fault_comes_back_as_a_short_code_on_every_adapter(seam, kwargs, before_handler):
    gw, verifier, token, ran = _world(**kwargs)
    for name, call in _adapters(gw, verifier, token).items():
        del ran[:]
        view = call()
        assert view["status"] == "error", (seam, name, view)
        assert view["error"] == "gateway_error", (seam, name, view)
        assert view["request_id"], (seam, name)
        assert SECRET not in json.dumps(view) and "/srv/x" not in json.dumps(view), (seam, name)
        if before_handler:
            assert ran == [], (seam, name, "a fault before the handler must stop the call")


def test_a_fault_before_the_handler_stops_the_call():
    gw, verifier, token, ran = _world(budget=_BudgetBoom())
    McpGatewayBridge(gw, verifier).call_tool(token, "boom.read", {})
    assert ran == []


def test_an_approvals_fault_comes_back_as_a_short_code():
    class _Approvals(_Boom):
        def __init__(self):
            super().__init__("is_satisfied")
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("w.write", "writes", CapabilityKind.DIRECT, permissions=("data.write",), risk_tier=RiskTier.CONSEQUENTIAL,
                                    side_effecting=True, idempotent=True, data_classes=(DataClass.INTERNAL,)),
                 lambda req, env: HandlerResult(ok=True, output={"sent": True}))
    verifier = DevTokenVerifier()
    token = verifier.issue("alice", tenant="t1", grants=("data.write",))
    gw = AgentGateway(registry=reg, authorize=verifier.authorize, approvals=_Approvals())
    view = McpGatewayBridge(gw, verifier).call_tool(token, "w.write", {})
    assert view["status"] == "error" and view["error"] == "gateway_error"
    assert SECRET not in json.dumps(view)


def test_a_seam_fault_is_logged_with_the_request_id(caplog):
    gw, verifier, token, _ = _world(budget=_BudgetBoom())
    with caplog.at_level(logging.WARNING, logger="agentic_os.agent_gateway"):
        view = McpGatewayBridge(gw, verifier).call_tool(token, "boom.read", {})
    assert view["request_id"] in caplog.text and SECRET in caplog.text      # the detail is in the log by design


# ── F3: the write-capability call site ──────────────────────────────────────────────────────────
def test_a_fault_in_a_write_capability_port_returns_a_code_not_its_text():
    class Crm:
        def prepare_outreach(self, arguments):
            raise RuntimeError("smtp://u:" + SECRET + "@mail.internal refused")

        def send_outreach(self, arguments, envelope):
            raise RuntimeError("never reached")
    reg = build_write_registry(crm=Crm())
    verifier = DevTokenVerifier()
    token = verifier.issue("carol", tenant="t1", grants=("crm.read",))
    gw = AgentGateway(registry=reg, authorize=verifier.authorize)
    view = McpGatewayBridge(gw, verifier).call_tool(token, "crm.prepare_outreach", {"contact": "x", "goal": "y"})
    assert view["status"] == "error" and view["error"] == "capability_error"
    assert SECRET not in json.dumps(view) and "smtp" not in json.dumps(view)


# ── F4: the server log ──────────────────────────────────────────────────────────────────────────
def test_the_handler_fault_log_line_carries_the_request_id(caplog):
    def boom(req, env):
        raise RuntimeError("db refused")
    gw, verifier, token, _ = _world(boom)
    with caplog.at_level(logging.WARNING, logger="agentic_os.agent_gateway"):
        view = McpGatewayBridge(gw, verifier).call_tool(token, "boom.read", {})
    assert "boom.read" in caplog.text and view["request_id"] in caplog.text


def test_a_newline_in_the_exception_text_cannot_forge_a_log_line():
    def boom(req, env):
        raise RuntimeError("bad input\nINFO agentic_os.audit: admin approved request 42\r\nWARNING x")
    gw, verifier, token, _ = _world(boom)
    records = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s").format(record))
    h = _H()
    lg = logging.getLogger("agentic_os.agent_gateway")
    lg.addHandler(h)
    try:
        McpGatewayBridge(gw, verifier).call_tool(token, "boom.read", {})
    finally:
        lg.removeHandler(h)
    assert len(records) == 1
    text = records[0]
    assert "\n" not in text and "\r" not in text                       # one record is one line
    assert "admin approved request 42" in text                         # the detail is kept (escaped), by design

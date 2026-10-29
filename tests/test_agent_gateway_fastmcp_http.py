"""The FastMCP Streamable-HTTP transport, over a REAL HTTP server and a REAL HTTP MCP client.

A uvicorn server on 127.0.0.1 (a free port) serves ``build_fastmcp_server`` with its bearer-token verifier; ``fastmcp.Client`` over
``StreamableHttpTransport`` calls it with a valid token. A synthetic secret is placed in (1) the exception of a capability handler, and (2) the exception of
a gateway seam raised OUTSIDE the handler ``try`` (budget, egress, audit, authorizer). The server's raw HTTP responses are recorded by an ASGI wrapper, and the test
asserts that no response body, on any path, contains the secret, a path, or a traceback: handler fault, seam fault, invalid arguments, unknown tool,
denial, and an invalid token.

Set ``GATEWAY_HTTP_EVIDENCE_DIR`` to also write the raw recorded responses of each scenario there (one JSON file per scenario).

Skipped only when the optional ``mcp`` extra (fastmcp) or uvicorn is not installed; the ``tests`` workflow installs the extra so this runs in CI.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import socket
import threading
import time

import pytest

pytest.importorskip("fastmcp", reason="the optional [mcp] extra (fastmcp) is not installed")
pytest.importorskip("uvicorn", reason="uvicorn is not installed")

import uvicorn                                                          # noqa: E402
from fastmcp import Client                                              # noqa: E402
from fastmcp.client.transports import StreamableHttpTransport           # noqa: E402

from agentic_os.agent_gateway import (                                  # noqa: E402
    AgentGateway, CapabilityRegistry, DevTokenVerifier, HandlerResult, build_fastmcp_server)
from agentic_os.agent_gateway.contracts import CapabilityKind, CapabilityManifest, DataClass   # noqa: E402

SECRET = "SYNTH-SECRET-777"
LEAKY = ("SYNTH-SECRET-777", "hunter2secretkey", "/srv/x", "/srv/internal", "Traceback", "RuntimeError", "File \"")


def _cap(name, perm="data.read"):
    return CapabilityManifest(name, "synthetic", CapabilityKind.DIRECT, permissions=(perm,), data_classes=(DataClass.INTERNAL,))


class _Faults:
    """Which gateway seam raises on the next call."""
    seam = ""


class _Budget:
    def check(self, principal, manifest):
        if _Faults.seam == "budget":
            raise RuntimeError("budget store failed at /srv/x " + SECRET)
        return True, "", ()


class _Egress:
    def decide(self, output, classes, principal, manifest):
        from agentic_os.agent_gateway.contracts import EgressAction
        if _Faults.seam == "egress":
            raise RuntimeError("egress policy failed at /srv/x " + SECRET)
        return EgressAction.ALLOW, output, ()


class _Audit:
    def __init__(self):
        self.events = []

    def record(self, event):
        if _Faults.seam == "audit":
            raise RuntimeError("audit store failed at /srv/x " + SECRET)
        self.events.append(event)
        return f"audit-{len(self.events)}"


class _Recorder:
    """Pure ASGI wrapper: records the raw HTTP responses the server sends (status and body)."""
    def __init__(self, app, log):
        self.app, self.log = app, log

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        entry = {"method": scope["method"], "path": scope["path"], "status": None, "body": b""}
        self.log.append(entry)

        async def _send(message):
            if message["type"] == "http.response.start":
                entry["status"] = message["status"]
            elif message["type"] == "http.response.body":
                entry["body"] += message.get("body", b"")
            await send(message)
        await self.app(scope, receive, _send)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _World:
    def __enter__(self):
        reg = CapabilityRegistry()

        def boom(req, env):
            raise RuntimeError("db at /srv/internal/prod.sqlite refused: password=" + SECRET)
        reg.register(_cap("boom.read"), boom)
        def lone_token(req, env):
            raise RuntimeError("hunter2secretkey")                # a lowercase single token used to be returned as the "code"
        reg.register(_cap("token.read"), lone_token)
        reg.register(_cap("ok.read"), lambda req, env: HandlerResult(ok=True, output={"v": 1}))
        reg.register(_cap("admin.read", perm="admin.read"), lambda req, env: HandlerResult(ok=True, output={"v": 2}))   # the token below is NOT granted this
        self.verifier = DevTokenVerifier()
        self.token = self.verifier.issue("alice", tenant="t1", grants=("data.read",))
        self.audit = _Audit()
        gw = AgentGateway(registry=reg, authorize=self.verifier.authorize, budget=_Budget(), egress=_Egress(), audit=self.audit)
        self.log = []
        app = _Recorder(build_fastmcp_server(gw, self.verifier, name="http-test").http_app(path="/mcp"), self.log)
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 20
        while not self.server.started:
            assert time.time() < deadline and self.thread.is_alive(), "the test server did not start"
            time.sleep(0.05)
        _Faults.seam = ""
        return self

    def __exit__(self, *exc):
        _Faults.seam = ""
        self.server.should_exit = True
        self.thread.join(timeout=10)

    def client(self, token=None):
        return Client(StreamableHttpTransport(self.url, auth=self.token if token is None else token))

    def bodies(self, since=0):
        return [(e["method"], e["path"], e["status"], e["body"].decode("utf-8", "replace")) for e in self.log[since:]]


def _assert_clean(world, since=0):
    seen = world.bodies(since)
    assert seen, "no HTTP response was recorded"
    for method, path, status, body in seen:
        for needle in LEAKY:
            assert needle not in body, f"{method} {path} {status} contains {needle!r}: {body[:300]}"


def _evidence(world, name, since=0):
    d = os.environ.get("GATEWAY_HTTP_EVIDENCE_DIR")
    if d:
        pathlib.Path(d).mkdir(parents=True, exist_ok=True)
        (pathlib.Path(d) / f"{name}.json").write_text(json.dumps(
            [{"method": m, "path": p, "status": s, "body": b} for m, p, s, b in world.bodies(since)], indent=2))


def _data(result):
    if getattr(result, "structured_content", None):
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


def test_a_valid_token_lists_only_the_permitted_tools_over_http():
    with _World() as w:
        async def main():
            async with w.client() as c:
                return sorted(t.name for t in await c.list_tools())
        names = asyncio.run(main())
        assert names == ["boom.read", "ok.read", "token.read"]                # admin.read is not granted, so it is not even listed
        _evidence(w, "tools_list")


def test_a_lone_lowercase_token_in_an_exception_is_not_returned_as_a_code():
    with _World() as w:
        async def main():
            async with w.client() as c:
                return await c.call_tool("token.read", {"arguments": {}}, raise_on_error=False)
        view = _data(asyncio.run(main()))
        assert view["status"] == "error" and view["error"] == "capability_error"
        _assert_clean(w)
        _evidence(w, "lone_token_fault")


def test_a_capability_handler_fault_returns_only_the_short_code():
    with _World() as w:
        async def main():
            async with w.client() as c:
                return await c.call_tool("boom.read", {"arguments": {}}, raise_on_error=False)
        res = asyncio.run(main())
        view = _data(res)
        assert view["status"] == "error" and view["error"] == "capability_error" and view["request_id"]
        _assert_clean(w)
        _evidence(w, "handler_fault")


@pytest.mark.parametrize("seam", ["budget", "egress", "audit"])
def test_a_gateway_seam_fault_outside_the_handler_try_returns_only_the_short_code(seam):
    with _World() as w:
        _Faults.seam = seam

        async def main():
            async with w.client() as c:
                return await c.call_tool("ok.read", {"arguments": {}}, raise_on_error=False)
        res = asyncio.run(main())
        text = json.dumps(_data(res)) if not res.is_error else res.content[0].text
        assert SECRET not in text and "/srv/x" not in text, text
        if not res.is_error:
            view = _data(res)
            assert view["status"] == "error" and view["error"] == "gateway_error", view
        _assert_clean(w)
        _evidence(w, f"seam_fault_{seam}")


def test_invalid_arguments_unknown_tool_and_denial_leak_nothing():
    with _World() as w:
        async def main():
            out = {}
            async with w.client() as c:
                out["bad_args"] = await c.call_tool("ok.read", {"arguments": "not-a-dict"}, raise_on_error=False)
                try:
                    out["unknown"] = await c.call_tool("no.such.tool", {"arguments": {}}, raise_on_error=False)
                except Exception as exc:                        # noqa: BLE001 - a client-side error is acceptable; the server's bytes are what is asserted
                    out["unknown_exc"] = repr(exc)
                out["denied"] = await c.call_tool("admin.read", {"arguments": {}}, raise_on_error=False)
            return out
        out = asyncio.run(main())
        denied = out["denied"]
        # a capability the principal may not see is the same answer as an unknown one, and says nothing about why
        assert denied.is_error or _data(denied).get("status") == "denied", denied
        _assert_clean(w)
        _evidence(w, "invalid_unknown_denied")


def test_an_invalid_token_is_refused_at_the_transport():
    with _World() as w:
        async def main():
            try:
                async with w.client(token="gwt_not-a-real-token") as c:
                    await c.list_tools()
            except Exception as exc:                            # noqa: BLE001
                return repr(exc)
            return None
        err = asyncio.run(main())
        assert err is not None, "an invalid bearer token must not open a session"
        statuses = [s for _, _, s, _ in w.bodies()]
        assert 401 in statuses, statuses
        _assert_clean(w)
        _evidence(w, "invalid_token")


def test_an_exception_escaping_the_tool_function_is_masked_by_the_transport(monkeypatch):
    from agentic_os.agent_gateway.protocols.mcp import McpGatewayBridge

    def boom(self, token, name, arguments=None):
        raise RuntimeError("bridge failed at /srv/x " + SECRET)
    monkeypatch.setattr(McpGatewayBridge, "call_tool", boom)
    with _World() as w:
        async def main():
            async with w.client() as c:
                return await c.call_tool("ok.read", {"arguments": {}}, raise_on_error=False)
        res = asyncio.run(main())
        assert res.is_error
        assert SECRET not in res.content[0].text and "/srv/x" not in res.content[0].text, res.content[0].text
        _assert_clean(w)
        _evidence(w, "escaping_exception_masked")

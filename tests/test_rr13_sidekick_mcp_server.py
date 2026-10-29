"""RR-13: deploy/sidekick-devops/mcp_server.py was an unauthenticated network MCP server (0.0.0.0:8231) running a preflight with the host's
ambient AWS credentials. Now: stdio by default; an http listener needs a bearer token (24+ characters) or the process refuses to start;
loopback by default; the AWS credential checks run only with SIDEKICK_MCP_ALLOW_AWS=1.

STAND-INS: ``fastmcp`` and ``aws_demo`` (the preflight implementation) are not installed here, so they are stubbed; the server's own logic
(transport choice, token rule, bind address, the bearer gate, the AWS switch) is what these tests exercise.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PATH = ROOT / "deploy" / "sidekick-devops" / "mcp_server.py"
TOKEN = "t0ken-" + "x" * 30


class _FakeFastMCP:
    def __init__(self, name):
        self.name, self.tools, self.ran = name, {}, []

    def tool(self):
        return lambda fn: self.tools.setdefault(fn.__name__, fn) or fn

    def run(self, **kw):
        self.ran.append(kw)

    def http_app(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
        return app


@pytest.fixture()
def srv(monkeypatch):
    fm = types.ModuleType("fastmcp")
    fm.FastMCP = _FakeFastMCP
    monkeypatch.setitem(sys.modules, "fastmcp", fm)
    spec = importlib.util.spec_from_file_location("sidekick_mcp_server_under_test", PATH)
    m = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, m)
    spec.loader.exec_module(m)
    for k in ("MCP_TRANSPORT", "MCP_BEARER_TOKEN", "BIND_HOST", "BIND_PORT", "SIDEKICK_MCP_ALLOW_AWS"):
        monkeypatch.delenv(k, raising=False)
    return m


class _Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, *a, **k):
        self.calls.append((a, k))


class TestStartupFailsClosed:
    def test_the_default_is_stdio_not_a_network_listener(self, srv):
        stdio, http = _Rec(), _Rec()
        srv.serve({}, run_stdio=stdio, run_http=http)
        assert len(stdio.calls) == 1 and http.calls == []

    def test_http_without_a_token_refuses_to_start_and_nothing_listens(self, srv):
        http = _Rec()
        for env in ({"MCP_TRANSPORT": "http"}, {"MCP_TRANSPORT": "http", "MCP_BEARER_TOKEN": ""},
                    {"MCP_TRANSPORT": "http", "MCP_BEARER_TOKEN": "short"}, {"MCP_TRANSPORT": "http", "MCP_BEARER_TOKEN": "x" * 23},
                    {"MCP_TRANSPORT": "streamable-http", "BIND_HOST": "0.0.0.0"}):
            with pytest.raises(SystemExit) as e:
                srv.serve(env, run_stdio=_Rec(), run_http=http)
            assert "MCP_BEARER_TOKEN" in str(e.value) and "24" in str(e.value)
        assert http.calls == []

    def test_an_unknown_transport_is_refused(self, srv):
        with pytest.raises(SystemExit):
            srv.serve({"MCP_TRANSPORT": "sse"}, run_stdio=_Rec(), run_http=_Rec())

    def test_with_a_token_it_binds_loopback_by_default(self, srv):
        http = _Rec()
        srv.serve({"MCP_TRANSPORT": "http", "MCP_BEARER_TOKEN": TOKEN}, run_http=http)
        (app, host, port), _ = http.calls[0]
        assert host == "127.0.0.1" and port == 8231 and isinstance(app, srv.BearerGate)

    def test_a_non_loopback_bind_needs_the_token_too_and_is_logged(self, srv, caplog):
        http = _Rec()
        with caplog.at_level("WARNING", logger="sidekick.mcp"):
            srv.serve({"MCP_TRANSPORT": "http", "MCP_BEARER_TOKEN": TOKEN, "BIND_HOST": "0.0.0.0", "BIND_PORT": "9000"}, run_http=http)
        assert http.calls[0][0][1:] == ("0.0.0.0", 9000) and "not loopback" in caplog.text

    def test_the_source_has_no_all_interfaces_default(self):
        src = PATH.read_text()
        assert 'getenv("BIND_HOST", "0.0.0.0")' not in src and 'or "127.0.0.1"' in src


class TestBearerGate:
    @pytest.fixture()
    def client(self, srv):
        from starlette.testclient import TestClient
        return TestClient(srv.BearerGate(_FakeFastMCP("x").http_app(), TOKEN), raise_server_exceptions=False)

    def test_no_credential_is_401(self, client):
        assert client.get("/mcp").status_code == 401 and client.get("/mcp").headers["www-authenticate"] == "Bearer"

    @pytest.mark.parametrize("hdr", ["Bearer wrong", "Bearer ", "Bearer", "Basic " + TOKEN, TOKEN, "Bearer " + TOKEN + "x", "bearer"])
    def test_wrong_or_malformed_credentials_are_401(self, client, hdr):
        assert client.get("/mcp", headers={"authorization": hdr}).status_code == 401

    def test_the_right_token_passes_in_any_scheme_case(self, client):
        assert client.get("/mcp", headers={"authorization": "Bearer " + TOKEN}).status_code == 200
        assert client.post("/mcp", headers={"authorization": "bearer " + TOKEN}).status_code == 200

    def test_a_token_in_the_query_string_or_a_cookie_is_not_a_credential(self, client):
        assert client.get("/mcp?token=" + TOKEN).status_code == 401 and client.get("/mcp?access_token=" + TOKEN).status_code == 401
        assert client.get("/mcp", headers={"cookie": "token=" + TOKEN}).status_code == 401

    def test_the_comparison_is_constant_time(self):
        assert "hmac.compare_digest" in PATH.read_text()


class TestAmbientAwsCredentials:
    @pytest.fixture()
    def aws(self, srv, monkeypatch):
        calls = []
        class Report:
            ready, blockers, checks = True, [], []
        pre = types.ModuleType("preflight")
        pre.Report = Report
        pre.check_local = lambda r: calls.append("local")
        pre.check_aws = lambda r, s: calls.append("aws")
        pre.render = lambda r: "rendered"
        doc = types.ModuleType("doctor")
        doc.session_factory = lambda: calls.append("session") or object()
        pkg = types.ModuleType("aws_demo")
        pkg.preflight, pkg.doctor = pre, doc
        monkeypatch.setitem(sys.modules, "aws_demo", pkg)
        monkeypatch.setenv("REDEVOPS_AWS_DEMO", str(ROOT))
        return srv, calls

    def test_by_default_no_aws_session_is_created_and_the_verdict_says_so(self, aws):
        srv, calls = aws
        out = srv.preflight_check("aws")
        assert calls == ["local"] and "skipped" in out["aws_checks"] and out["ready"] is True

    def test_only_an_explicit_switch_uses_the_ambient_credentials(self, aws, monkeypatch):
        srv, calls = aws
        for bad in ("true", "yes", "0", ""):
            monkeypatch.setenv("SIDEKICK_MCP_ALLOW_AWS", bad)
            calls.clear()
            srv.preflight_check("aws")
            assert "session" not in calls, bad
        monkeypatch.setenv("SIDEKICK_MCP_ALLOW_AWS", "1")
        calls.clear()
        out = srv.preflight_check("aws")
        assert calls == ["local", "session", "aws"] and "aws_checks" not in out

    def test_another_cloud_does_not_touch_aws_at_all(self, aws):
        srv, calls = aws
        assert srv.preflight_check("gcp")["ready"] is False and calls == []


def test_the_bundle_readme_documents_the_server_and_its_switches():
    t = (ROOT / "deploy" / "sidekick-devops" / "README.md").read_text()
    assert "MCP_BEARER_TOKEN" in t and "SIDEKICK_MCP_ALLOW_AWS" in t and "stdio" in t

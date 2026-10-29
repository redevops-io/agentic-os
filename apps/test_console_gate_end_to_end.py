"""RR-02: the books / billing / compliance consoles are gated END TO END through the shipped app object.

The ``authorizer=`` hook of ``Assistant`` was wired nowhere and the console ignored the process-wide default authorizer the enterprise
bootstrap installs (``context_runtime.tools.base``), so ``POST /api/agent`` ran every tool for anyone. These tests start each SHIPPED app
(its module-level ``install_gate`` runs), post to its real ``/api/agent`` route and check that an unauthorised identity is denied with the
tool body NOT run, an authorised one runs it, and a console is never an open door when permissions were requested.

STAND-IN: the enterprise bootstrap and ``context_runtime.tools.base`` are a stub (``tests/_cr_enterprise_stub.py``); the identity of a
request comes from test-only headers. The authorizer the stub installs is a grant check on the tool's classification.
"""
from __future__ import annotations

import importlib
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tests"))
import _cr_enterprise_stub as stub  # noqa: E402

from agentic_os.tool_actions import TOOL_ACTIONS  # noqa: E402

def _console_tools(app):
    """The console's own tools: the dotted names are Operator SDK capabilities (a different execution path, covered in test_basic_permissions_apps.py)."""
    return {t: a for t, a in TOOL_ACTIONS[app].items() if "." not in t}


CONSOLES = {"books": "_BOOKS_CONSOLE", "billing": "_BILL_CONSOLE", "compliance": "_COMP_CONSOLE"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("CR_PERMISSIONS", "CR_IDENTITY_MODE", "CR_PERMISSIONS_DEV_UNGATED_ACK", "CR_IDENTITY_SECRET_FILE"):
        monkeypatch.delenv(k, raising=False)


def _start(monkeypatch, app):
    for name in [n for n in sys.modules if n == f"{app}.app"]:
        monkeypatch.delitem(sys.modules, name)
    return importlib.import_module(f"{app}.app")


def _instrument(console):
    """Replace each tool body by a recorder (test instrumentation); return the list of tool names whose body ran and a message per tool."""
    ran, msg = [], {}
    for t in console.tools:
        t.fn = (lambda name: lambda a: (ran.append(name), {"text": f"ran {name}"})[1])(t.name)
        for kw in t.keywords:
            if (console._pick(kw) or t).name == t.name and console._pick(kw) is not None:
                msg[t.name] = kw
                break
    return ran, msg


def _start_console(monkeypatch, app, *, flag="1"):
    if flag:
        monkeypatch.setenv("CR_PERMISSIONS", flag)
    st = stub.install(monkeypatch, fleet={})
    mod = _start(monkeypatch, app)
    console = getattr(mod, CONSOLES[app])
    ran, msg = _instrument(console)
    assert set(msg) == set(_console_tools(app)), (set(_console_tools(app)) - set(msg), "no message reaches these tools")
    return st, TestClient(mod.app), console, ran, msg


@pytest.mark.parametrize("app", CONSOLES)
class TestShippedConsoles:
    def test_an_unauthorised_identity_is_denied_for_every_tool_and_no_body_runs(self, monkeypatch, app):
        st, client, console, ran, msg = _start_console(monkeypatch, app)
        for tool, text in msg.items():
            for headers in ({}, {"x-test-user": "stranger"}, {"x-test-user": "stranger", "x-test-roles": "admin"}):
                r = client.post("api/agent", json={"message": text}, headers=headers)
                assert r.status_code == 200 and r.json().get("denied") is True, (tool, headers, r.json())
                assert "[blocked]" in r.json()["text"] and r.json()["intent"] == tool
        assert ran == []

    def test_an_authorised_identity_runs_each_tool(self, monkeypatch, app):
        st, client, console, ran, msg = _start_console(monkeypatch, app)
        for tool, text in msg.items():
            r = client.post("api/agent", json={"message": text}, headers={"x-test-user": "granted"})
            assert r.json().get("denied") is not True and r.json()["text"] == f"ran {tool}", r.json()
        assert sorted(ran) == sorted(msg)

    def test_a_message_that_picks_no_tool_does_not_run_a_body(self, monkeypatch, app):
        st, client, console, ran, msg = _start_console(monkeypatch, app)
        client.post("api/agent", json={"message": "hello there"}, headers={"x-test-user": "stranger"})
        assert ran == []

    def test_permissions_requested_but_no_authorizer_installed_is_a_denial_never_an_open_console(self, monkeypatch, app):
        st, client, console, ran, msg = _start_console(monkeypatch, app)
        st.base.set_default_authorizer(None)                 # the gate disappears after start-up
        for text in msg.values():
            assert client.post("api/agent", json={"message": text}, headers={"x-test-user": "granted"}).json().get("denied") is True
        assert ran == []

    def test_the_default_authorizer_is_read_at_call_time_not_captured_at_import(self, monkeypatch, app):
        st, client, console, ran, msg = _start_console(monkeypatch, app)
        text = next(iter(msg.values()))
        st.base.set_default_authorizer(lambda p, spec, args: None)           # a later install (e.g. a different gate) takes effect
        assert client.post("api/agent", json={"message": text}, headers={"x-test-user": "stranger"}).json().get("denied") is not True

    def test_an_authorizer_that_raises_is_a_denial(self, monkeypatch, app):
        st, client, console, ran, msg = _start_console(monkeypatch, app)

        def boom(principal, spec, args):
            raise RuntimeError("x")
        st.base.set_default_authorizer(boom)
        assert client.post("api/agent", json={"message": next(iter(msg.values()))}, headers={"x-test-user": "granted"}).json()["denied"] is True
        assert ran == []

    def test_an_explicit_authorizer_wins_over_the_default(self, monkeypatch, app):
        st, client, console, ran, msg = _start_console(monkeypatch, app)
        console.authorizer = lambda p, spec, args: "explicit no"
        r = client.post("api/agent", json={"message": next(iter(msg.values()))}, headers={"x-test-user": "granted"}).json()
        assert r["denied"] is True and "explicit no" in r["text"]

    def test_without_the_flag_and_without_the_enterprise_package_the_console_works_as_before(self, monkeypatch, app):
        stub.absent(monkeypatch)
        mod = _start(monkeypatch, app)
        console = getattr(mod, CONSOLES[app])
        ran, msg = _instrument(console)
        client = TestClient(mod.app)
        for tool, text in msg.items():
            assert client.post("api/agent", json={"message": text}).json()["text"] == f"ran {tool}"
        assert sorted(ran) == sorted(msg)

    def test_write_tools_need_a_write_grant_in_the_classification_the_gate_sees(self, monkeypatch, app):
        st, client, console, ran, msg = _start_console(monkeypatch, app)
        seen = {}
        st.base.set_default_authorizer(lambda p, spec, args: seen.setdefault(spec.name, spec.side_effecting) and None)
        for tool, text in msg.items():
            client.post("api/agent", json={"message": text}, headers={"x-test-user": "granted"})
        assert {t for t, side in seen.items() if side} == {t for t, a in _console_tools(app).items() if a == "write"}


@pytest.mark.parametrize("app", CONSOLES)
@pytest.mark.parametrize("package", ["absent", "present"])
def test_with_nothing_requested_the_console_behaves_the_same_with_the_enterprise_package_present_or_absent(monkeypatch, app, package):
    """KD-2: the basic default. The enterprise bootstrap used to install a deny-all gate when permissions were off, so a console was open without
    the package and denied with it."""
    if package == "absent":
        stub.absent(monkeypatch)
    else:
        stub.install(monkeypatch, fleet={})
    mod = _start(monkeypatch, app)
    console = getattr(mod, CONSOLES[app])
    ran, msg = _instrument(console)
    client = TestClient(mod.app)
    for tool, text in msg.items():
        assert client.post("api/agent", json={"message": text}).json()["text"] == f"ran {tool}"
    assert sorted(ran) == sorted(msg)

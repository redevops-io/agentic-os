"""RR-01: an app that REQUESTED permissions starts gated or does not start. It never swallows the bootstrap error and runs ungated.

The enterprise bootstrap is a STAND-IN (``tests/_cr_enterprise_stub.py``): ``context_runtime`` / ``context_runtime_enterprise`` are not
installable here. The stub raises exactly where the real bootstrap does (``ToolActionsRequired``, a bad ``CR_IDENTITY_MODE``), so these tests
exercise the kernel's side of the contract: what an app does with that error.
"""
from __future__ import annotations

import ast
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import _cr_enterprise_stub as stub  # noqa: E402

from agentic_os import app_gate  # noqa: E402
from agentic_os.app_gate import GateStartupError, install_gate, permissions_requested  # noqa: E402
from agentic_os.tool_actions import TOOL_ACTIONS, ToolActionsConflict, resolve_tool_actions  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
BOOTSTRAP_APPS = ("agentic-crm", "agentic-privacy", "billing", "books", "compliance", "control-tower", "document-editor", "edge-sentinel",
                  "growth-assistant", "growth-engine", "guide", "infra", "lifecycle", "market-radar", "outreach-engine", "social-autopilot",
                  "support")
FLEET = {"agentic-crm": {"pipeline_summary": "read", "crm.score_lead": "write"},
         "lifecycle": {"list_campaigns": "read", "lifecycle.compose_campaign": "write"},
         "support": {"inbox_summary": "read", "support.resolve": "write"},
         "books": {"books.categorize": "write"}}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("CR_PERMISSIONS", "CR_IDENTITY_MODE", "CR_PERMISSIONS_DEV_UNGATED_ACK", "CR_IDENTITY_SECRET_FILE"):
        monkeypatch.delenv(k, raising=False)


# ───────────────────────── what "requested" means ─────────────────────────

@pytest.mark.parametrize("env,expected", [
    ({}, False), ({"CR_PERMISSIONS": ""}, False), ({"CR_PERMISSIONS": "0"}, False), ({"CR_PERMISSIONS": "false"}, False),
    ({"CR_PERMISSIONS": "off"}, False), ({"CR_PERMISSIONS": "1"}, True), ({"CR_PERMISSIONS": "true"}, True),
    ({"CR_PERMISSIONS": "yes"}, True), ({"CR_PERMISSIONS": "TRUE"}, True), ({"CR_PERMISSIONS": "on"}, True),
    ({"CR_PERMISSIONS": "enabled"}, True), ({"CR_IDENTITY_MODE": "token"}, True), ({"CR_IDENTITY_MODE": "bogus"}, True),
    ({"CR_IDENTITY_SECRET_FILE": "/x"}, True), ({"CR_IDENTITY_MODE": ""}, False), ({"CR_PERMISSIONS_DEV_UNGATED_ACK": "x"}, False),
])
def test_permissions_are_requested_by_any_non_off_value_or_any_identity_variable(env, expected):
    assert permissions_requested(env) is expected


# ───────────────────────── install_gate: the fail-closed rule ─────────────────────────

class TestRequestedMeansGatedOrNoStart:
    def test_permissions_on_without_a_classification_table_refuses_to_start(self, monkeypatch):
        """The exact RR-01 case: no tool_actions + CR_PERMISSIONS=1 raised ToolActionsRequired and the app swallowed it."""
        monkeypatch.setenv("CR_PERMISSIONS", "1")
        stub.install(monkeypatch)
        with pytest.raises(GateStartupError) as e:
            install_gate("no-such-app")                   # no kernel table for it, and this enterprise build has no fleet table
        assert "tool classification" in str(e.value) and "refuses to run ungated" in str(e.value)

    def test_the_bootstrap_error_itself_is_never_swallowed_when_requested(self, monkeypatch):
        monkeypatch.setenv("CR_PERMISSIONS", "1")
        s = stub.install(monkeypatch)

        def raising(app, **kw):
            raise stub.ToolActionsRequired("CR_PERMISSIONS is on but bootstrap() got no tool_actions")
        s.modules["context_runtime_enterprise.apps"].bootstrap = raising
        with pytest.raises(GateStartupError) as e:
            install_gate("edge-sentinel")
        assert "ToolActionsRequired" in str(e.value)
        assert isinstance(e.value.__cause__, stub.ToolActionsRequired)

    @pytest.mark.parametrize("mode", ["bogus", "TOKEN2", "none", "jwt"])
    def test_a_bad_identity_mode_refuses_to_start_even_with_permissions_off(self, monkeypatch, mode):
        monkeypatch.setenv("CR_IDENTITY_MODE", mode)
        stub.install(monkeypatch)
        with pytest.raises(GateStartupError) as e:
            install_gate("edge-sentinel")
        assert "IdentityConfigError" in str(e.value)

    def test_an_unreadable_identity_secret_file_setting_is_a_request_too(self, monkeypatch):
        monkeypatch.setenv("CR_IDENTITY_SECRET_FILE", "/nonexistent")
        stub.absent(monkeypatch)
        with pytest.raises(GateStartupError):
            install_gate("edge-sentinel")

    def test_requested_and_the_enterprise_package_missing_refuses_to_start(self, monkeypatch):
        monkeypatch.setenv("CR_PERMISSIONS", "1")
        stub.absent(monkeypatch)
        with pytest.raises(GateStartupError) as e:
            install_gate("billing")
        assert "not importable" in str(e.value)

    def test_requested_and_a_bootstrap_too_old_to_take_tool_actions_refuses_to_start(self, monkeypatch):
        monkeypatch.setenv("CR_PERMISSIONS", "1")
        stub.install(monkeypatch, bootstrap_has_tool_actions=False)
        with pytest.raises(GateStartupError) as e:
            install_gate("billing")
        assert "does not accept tool_actions" in str(e.value)

    def test_requested_and_an_unexpected_bootstrap_failure_refuses_to_start(self, monkeypatch):
        monkeypatch.setenv("CR_PERMISSIONS", "1")
        s = stub.install(monkeypatch)
        s.modules["context_runtime_enterprise.apps"].bootstrap = lambda app, **kw: (_ for _ in ()).throw(OSError("secret file unreadable"))
        with pytest.raises(GateStartupError) as e:
            install_gate("billing")
        assert "OSError" in str(e.value)

    def test_requested_and_a_bootstrap_that_installs_nothing_refuses_to_start(self, monkeypatch):
        monkeypatch.setenv("CR_PERMISSIONS", "1")
        s = stub.install(monkeypatch)
        s.modules["context_runtime_enterprise.apps"].bootstrap = lambda app, **kw: (lambda request=None: None)   # installs no gate
        with pytest.raises(GateStartupError) as e:
            install_gate("billing")
        assert "no process-wide authorizer" in str(e.value)

    def test_requested_and_a_conflicting_table_refuses_to_start(self, monkeypatch):
        monkeypatch.setenv("CR_PERMISSIONS", "1")
        stub.install(monkeypatch, fleet={"billing": {"refund": "read"}})      # the kernel says refund is write
        with pytest.raises(GateStartupError) as e:
            install_gate("billing")
        assert "ToolActionsConflict" in str(e.value)

    def test_requested_and_resolved_installs_the_gate_with_the_resolved_table(self, monkeypatch):
        monkeypatch.setenv("CR_PERMISSIONS", "1")
        s = stub.install(monkeypatch, fleet=FLEET)
        principal_for = install_gate("books")
        call = s.bootstrap_calls[-1]
        assert call["app"] == "books" and call["tool_actions"]["close_status"] == "read" and call["tool_actions"]["books.categorize"] == "write"
        assert callable(principal_for) and s.default_authorizer() is not None

    def test_every_kernel_classified_app_starts_gated_with_permissions_on_and_no_enterprise_table(self, monkeypatch):
        monkeypatch.setenv("CR_PERMISSIONS", "1")
        for app in ("agentic-privacy", "billing", "books", "compliance", "edge-sentinel", "growth-engine", "market-radar"):
            s = stub.install(monkeypatch)
            install_gate(app)
            assert s.bootstrap_calls[-1]["tool_actions"] == TOOL_ACTIONS[app] and s.default_authorizer() is not None


class TestNotRequestedWorksAsBefore:
    def test_without_the_enterprise_package_the_app_is_ungated_as_in_the_basic_default(self, monkeypatch):
        stub.absent(monkeypatch)
        assert install_gate("billing")() is None

    def test_without_the_flag_the_bootstrap_is_not_called_and_no_gate_is_installed(self, monkeypatch):
        """KD-2 / DR-02: nothing requested means the basic default whether or not the enterprise package is installed, and the package is never imported or called."""
        s = stub.install(monkeypatch)
        principal_for = install_gate("no-such-app")        # no table at all: not requested, so not an error
        assert s.bootstrap_calls == [] and principal_for() is None
        assert s.default_authorizer() is None

    def test_without_the_flag_a_failing_bootstrap_is_never_reached(self, monkeypatch):
        s = stub.install(monkeypatch)
        s.modules["context_runtime_enterprise.apps"].bootstrap = lambda app, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        assert install_gate("billing")() is None

    def test_an_old_bootstrap_without_tool_actions_still_works_when_not_requested(self, monkeypatch):
        stub.install(monkeypatch, bootstrap_has_tool_actions=False)
        assert callable(install_gate("billing"))


# ───────────────────────── one classified table per app ─────────────────────────

class TestResolveToolActions:
    def test_kernel_only(self, monkeypatch):
        stub.absent(monkeypatch)
        assert resolve_tool_actions("edge-sentinel") == TOOL_ACTIONS["edge-sentinel"]

    def test_an_app_neither_source_classifies_is_none(self, monkeypatch):
        stub.absent(monkeypatch)
        assert resolve_tool_actions("no-such-app") is None

    def test_the_enterprise_table_alone_classifies_nothing(self, monkeypatch):
        stub.install(monkeypatch, fleet={"no-such-app": {"x": "read"}})
        assert resolve_tool_actions("no-such-app", enterprise=True) is None

    def test_enterprise_entries_for_unlisted_tools_are_ignored_they_can_only_add_denials(self, monkeypatch):
        stub.install(monkeypatch, fleet=FLEET)
        merged = resolve_tool_actions("books", enterprise=True)
        assert merged == TOOL_ACTIONS["books"] and "books.nothing" not in merged
        merged["x"] = "read"
        assert "x" not in TOOL_ACTIONS["books"]                              # a fresh dict every time

    def test_a_disagreement_is_an_error_not_a_pick(self, monkeypatch):
        stub.install(monkeypatch, fleet={"books": {"close_status": "write"}})
        with pytest.raises(ToolActionsConflict):
            resolve_tool_actions("books", enterprise=True)
        assert resolve_tool_actions("books", enterprise=False)["close_status"] == "read"        # basic mode never reads the enterprise table

    def test_the_enterprise_refusal_for_a_kernel_classified_app_means_no_enterprise_table(self, monkeypatch):
        stub.install(monkeypatch, fleet={})
        assert resolve_tool_actions("market-radar") == TOOL_ACTIONS["market-radar"]

    def test_every_app_that_bootstraps_resolves_to_exactly_one_table_with_the_enterprise_fleet(self, monkeypatch):
        fleet = {a: {"t": "read"} for a in ("agentic-crm", "lifecycle", "support")}
        stub.install(monkeypatch, fleet=fleet)
        for app in BOOTSTRAP_APPS:
            assert isinstance(resolve_tool_actions(app, enterprise=True), dict), app


# ───────────────────────── the walk: no app can swallow a gate failure ─────────────────────────

BROAD = {"Exception", "BaseException"}


def _broad_handlers(tree):
    """Every try-statement of the module with a bare / Exception / BaseException handler, with the node ids of its body."""
    for t in ast.walk(tree):
        if not isinstance(t, ast.Try):
            continue
        for h in t.handlers:
            names = set()
            if h.type is None:
                names.add("<bare>")
            for n in ast.walk(h.type) if h.type is not None else ():
                if isinstance(n, ast.Name):
                    names.add(n.id)
            if names & (BROAD | {"<bare>"}):
                yield t
                break


def _calls_in(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            yield (f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""), n


GATE_CALLS = {"bootstrap", "_cr_bootstrap", "install_gate", "_install_gate", "install_fleet_permissions", "install_deny_all"}


def _app_files():
    return sorted(p for p in (ROOT / "apps").glob("*/*.py") if "test" not in p.name and p.name != "conftest.py")


def test_no_app_module_calls_a_gate_function_inside_a_broad_except():
    """A gate call inside ``try/except Exception`` (or bare) can swallow ToolActionsRequired / IdentityConfigError: the RR-01 defect."""
    offenders = []
    for f in _app_files():
        tree = ast.parse(f.read_text())
        for t in _broad_handlers(tree):
            body = ast.Module(body=t.body, type_ignores=[])
            for name, call in _calls_in(body):
                if name in GATE_CALLS:
                    offenders.append(f"{f.parent.name}/{f.name}:{call.lineno} {name}()")
    assert offenders == [], offenders


def test_no_app_module_imports_the_enterprise_bootstrap_directly():
    """The only place that imports it is ``agentic_os.app_gate``, which owns the fail-closed rule."""
    offenders = []
    for f in _app_files():
        for n in ast.walk(ast.parse(f.read_text())):
            if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("context_runtime_enterprise"):
                offenders.append(f"{f.parent.name}/{f.name}:{n.lineno}")
    assert offenders == [], offenders


def test_every_bootstrapping_app_calls_install_gate_at_module_level_with_its_own_name():
    found = {}
    for f in (ROOT / "apps").glob("*/app.py"):
        tree = ast.parse(f.read_text())
        consts = {}
        for stmt in tree.body:                         # module level only: not inside a try, an if or a function
            if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant) and len(stmt.targets) == 1 \
                    and isinstance(stmt.targets[0], ast.Name):
                consts[stmt.targets[0].id] = stmt.value.value
            for name, call in _calls_in(stmt) if isinstance(stmt, (ast.Assign, ast.Expr)) else ():
                if name in ("install_gate", "_install_gate") and call.args:
                    a0 = call.args[0]
                    found[f.parent.name] = a0.value if isinstance(a0, ast.Constant) else consts.get(getattr(a0, "id", ""))
    assert found == {a: a for a in BOOTSTRAP_APPS}, found


def test_the_walk_catches_the_old_pattern():
    old = ("try:\n    from context_runtime_enterprise.apps import bootstrap as _cr_bootstrap\n    _req_principal = _cr_bootstrap('x')\n"
           "except Exception:\n    def _req_principal(request=None):\n        return None\n")
    hits = [name for t in _broad_handlers(ast.parse(old)) for name, _ in _calls_in(ast.Module(body=t.body, type_ignores=[])) if name in GATE_CALLS]
    assert hits == ["_cr_bootstrap"]


def test_apps_without_a_bootstrap_call_are_listed_here_so_a_new_one_is_noticed():
    """Apps that never join the permissions plane (they are not gated by it). A new app must either call install_gate or be added here."""
    no_gate = sorted(p.parent.name for p in (ROOT / "apps").glob("*/app.py")
                     if "install_gate" not in p.read_text() and "bootstrap" not in p.read_text())
    assert no_gate == [], no_gate            # every app with an app.py now joins a permissions plane (basic or enterprise)

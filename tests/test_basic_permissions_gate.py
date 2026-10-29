"""The feature boundary: BASIC permissions are AGPL and need nothing from ``context_runtime_enterprise``; ENTERPRISE identity / permissioning
requested without the package fails closed with a clear missing-capability error.

Every row of the behaviour table in ``agentic_os/app_gate.py`` is exercised with the enterprise package deliberately ABSENT (not importable)
and again with a STAND-IN present (``tests/_cr_enterprise_stub.py``; the real package is not installable here).
"""
from __future__ import annotations

import os
import pathlib
import stat
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import _cr_enterprise_stub as stub  # noqa: E402

from agentic_os import app_gate  # noqa: E402
from agentic_os.app_gate import EnterpriseCapabilityMissing, GateStartupError, install_gate  # noqa: E402
from agentic_os.permissions import GrantStore, Identity  # noqa: E402
from agentic_os.permissions_setup import init_local_profile, local_user  # noqa: E402
from agentic_os.tool_actions import TOOL_ACTIONS  # noqa: E402

APP = "growth-assistant"
READ, WRITE = "assistant.ask", "assistant.playbook"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("CR_PERMISSIONS", "CR_IDENTITY_MODE", "CR_IDENTITY_PROFILES", "CR_IDENTITY_TENANT_CLAIM", "CR_PERMISSIONS_DEV_UNGATED_ACK",
              "AGENTIC_OS_PERMISSIONS", app_gate.RETRIEVAL_SWITCH, app_gate.CACHE_SWITCH, "PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE",
              "PERMISSIONS_ALLOW_GENERATED_KEY_DEV", "PERMISSIONS_ALLOW_PLAINTEXT_DEV"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setattr(app_gate, "_GATES", {})
    monkeypatch.setattr(app_gate, "_PROCESS_AUTHORIZER", None)


def _basic(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")


def _init(tmp_path, **kw):
    return init_local_profile(str(tmp_path / "perm"), **kw)


MODES = ["absent", "stub"]


@pytest.fixture(params=MODES)
def enterprise(request, monkeypatch):
    """The enterprise package ABSENT (import blocked) and PRESENT (a stand-in with the real raise behaviour)."""
    if request.param == "absent":
        stub.absent(monkeypatch)
        return None
    return stub.install(monkeypatch, fleet={})


# ───────────── (a) AGPL governance, approvals, budgets, audit work: nothing requested, nothing changes ─────────────

class TestOpenCoreDefault:
    def test_nothing_requested_installs_no_gate_and_requires_no_enterprise_package(self, enterprise):
        pf = install_gate(APP)
        assert app_gate.gate_for(APP) is None and not hasattr(pf, "gate")
        assert app_gate.tool_denial(APP, WRITE) is None and app_gate.deny_response(APP, WRITE) is None      # the seam is a no-op when not requested

    def test_the_governance_modules_import_and_run_without_the_enterprise_package(self, monkeypatch):
        stub.absent(monkeypatch)
        import agentic_os.approval_gate, agentic_os.governance.classification, agentic_os.mission.operator_sdk  # noqa: F401,E401

    def test_the_operator_seam_is_a_no_op_without_the_switch(self, enterprise):
        install_gate(APP)
        from agentic_os.mission.operator_sdk import Operator, capability
        op = Operator(APP, [capability(WRITE, lambda i: {"ok": True}, side_effecting=True)])
        assert op.invoke(WRITE, {}) == {"ok": True}


# ───────────── (b) basic tool/resource grants through the kernel's own plane ─────────────

class TestBasicGrants:
    def test_works_with_the_enterprise_package_absent_and_present(self, enterprise, monkeypatch, tmp_path):
        _basic(monkeypatch)
        _init(tmp_path)                                                         # read grants for the local user
        pf = install_gate(APP)
        gate = pf.gate
        assert pf.mode == "basic" and gate.restricted is False and gate.directory == str(tmp_path / "perm")
        assert gate.check(READ) is None                                         # granted read: allowed
        assert "no 'write' grant" in (gate.check(WRITE) or "") or "denied" in (gate.check(WRITE) or "")    # write withheld: denied

    def test_a_write_grant_authorises_the_write_tool_and_nothing_else(self, enterprise, monkeypatch, tmp_path):
        _basic(monkeypatch)
        _init(tmp_path, include_write=True)
        gate = install_gate(APP).gate
        assert gate.check(WRITE) is None and gate.check("assistant.push_assets") is None
        assert gate.check("assistant.not_a_tool") is not None                   # unclassified: denied

    def test_a_read_grant_never_authorises_a_write_tool(self, enterprise, monkeypatch, tmp_path):
        _basic(monkeypatch)
        _init(tmp_path)
        gate = install_gate(APP).gate
        for tool, action in TOOL_ACTIONS[APP].items():
            assert (gate.check(tool) is None) == (action == "read"), tool

    def test_an_unclassified_tool_is_denied_even_with_every_grant(self, enterprise, monkeypatch, tmp_path):
        _basic(monkeypatch)
        _init(tmp_path, include_write=True)
        store = GrantStore(str(tmp_path / "perm"))
        store.add("user", "local", "tool", f"{APP}.mystery", actions=["read", "write", "delete"])
        assert install_gate(APP).gate.check("mystery") is not None

    def test_the_gate_is_the_process_authorizer_the_consoles_read(self, enterprise, monkeypatch, tmp_path):
        _basic(monkeypatch)
        _init(tmp_path)
        install_gate(APP)
        auth = app_gate.default_authorizer()
        spec = type("S", (), {"name": READ, "side_effecting": False})()
        assert auth(Identity(user="local"), spec, {}) is None
        assert auth(Identity(user="someone-else"), spec, {}) is not None


# ───────────── (c) basic row/column restrictions within their documented enforcement scope ─────────────

class TestBasicRowColumnScope:
    def test_a_masked_or_row_scoped_grant_denies_a_tool_that_does_not_apply_the_scope_rather_than_dropping_it(self, enterprise, monkeypatch, tmp_path):
        _basic(monkeypatch)
        _init(tmp_path)
        store = GrantStore(str(tmp_path / "perm"))
        for g in store.list():                                                  # replace the plain read grant by a column-masked one
            store.remove(g.id)
        store.add("user", "local", "tool", f"{APP}.{READ}", actions=["read"], masked_columns=["email"])
        deny = install_gate(APP).gate.check(READ)
        assert deny and "scoped_tools" in deny                                  # documented scope: the tool must declare it applies the restriction


# ───────────── (d) local single-user identity ─────────────

class _Req:
    def __init__(self, headers):
        self.headers = headers


class TestLocalIdentity:
    def test_the_principal_is_the_local_profile_never_a_request_header(self, enterprise, monkeypatch, tmp_path):
        _basic(monkeypatch)
        _init(tmp_path, user="alice", include_write=True)
        pf = install_gate(APP)
        for headers in ({}, {"x-user": "bob", "x-roles": "admin"}, {"authorization": "Bearer x", "x-test-user": "bob"}):
            p = pf(_Req(headers))
            assert (p.user, p.roles, p.app) == ("alice", [], "")
        assert pf.gate.check(WRITE, principal=pf(_Req({"x-user": "bob"}))) is None          # alice's grant, whatever bob claims
        assert pf.gate.check(WRITE, principal=Identity(user="bob")) is not None

    def test_the_profile_user_is_recorded_by_init_and_read_back(self, tmp_path):
        _init(tmp_path, user="maria")
        assert local_user(str(tmp_path / "perm")) == "maria"
        prof = tmp_path / "perm" / "profile.json"
        assert stat.S_IMODE(os.stat(prof).st_mode) == 0o600

    def test_a_store_provisioned_before_profiles_existed_uses_the_default_local_user(self, tmp_path):
        _init(tmp_path)
        (tmp_path / "perm" / "profile.json").unlink()
        assert local_user(str(tmp_path / "perm")) == "local"

    def test_an_invalid_profile_user_stops_the_app_never_falls_back_to_another_user(self, enterprise, monkeypatch, tmp_path):
        _basic(monkeypatch)
        _init(tmp_path)
        (tmp_path / "perm" / "profile.json").write_text('{"user": "bad\\u200bname"}')
        with pytest.raises(GateStartupError):
            install_gate(APP)

    def test_there_is_no_implicit_admin_bypass_even_for_a_role_called_admin(self, enterprise, monkeypatch, tmp_path):
        _basic(monkeypatch)
        _init(tmp_path)                                                         # read grants only
        gate = install_gate(APP).gate
        assert gate.check(WRITE, principal=Identity(user="local", roles=["admin"])) is not None
        assert gate.check(WRITE, principal=Identity(user="nobody", roles=["admin", "policy-admin", "root"])) is not None


# ───────────── restricted start: no grants provisioned ─────────────

class TestRestrictedStart:
    def test_the_app_starts_restricted_when_nothing_is_provisioned_and_every_protected_operation_is_denied(self, enterprise, monkeypatch, tmp_path, caplog):
        _basic(monkeypatch)
        with caplog.at_level("WARNING", logger="agentic_os.app_gate"):
            pf = install_gate(APP)                                              # starts: it does NOT raise
        gate = pf.gate
        assert gate.restricted is True and "RESTRICTED" in caplog.text
        for tool in TOOL_ACTIONS[APP]:
            deny = gate.check(tool)
            assert deny and "agentic-os permissions init" in deny, tool         # actionable, never a bare refusal
        assert not (tmp_path / "perm").exists()                                  # nothing was created: no key, no store, no directory

    def test_restricted_is_not_open_a_route_and_the_operator_deny(self, enterprise, monkeypatch, tmp_path):
        _basic(monkeypatch)
        install_gate(APP)
        assert app_gate.deny_response(APP, READ).status_code == 403
        from agentic_os.mission.operator_sdk import Operator, capability
        ran = []
        op = Operator(APP, [capability(WRITE, lambda i: ran.append(1) or {"ok": 1}, side_effecting=True)])
        out = op.invoke(WRITE, {})
        assert out["status"] == "denied" and out["executed"] is False and "agentic-os permissions init" in out["error"] and ran == []

    def test_after_init_the_same_app_code_works_without_any_other_change(self, enterprise, monkeypatch, tmp_path):
        _basic(monkeypatch)
        assert install_gate(APP).gate.check(READ) is not None
        _init(tmp_path)
        assert install_gate(APP).gate.check(READ) is None

    def test_a_provisioned_key_that_cannot_be_used_is_a_start_failure_not_restricted(self, enterprise, monkeypatch, tmp_path):
        """'No grants provisioned' is the ONLY reason to start restricted: a key file that is too open is a real failure."""
        _basic(monkeypatch)
        _init(tmp_path)
        os.chmod(tmp_path / "perm" / ".permissions.key", 0o644)
        with pytest.raises(GateStartupError) as e:
            install_gate(APP)
        assert "basic permissions" in str(e.value) and "refuses to run ungated" in str(e.value)

    def test_basic_mode_never_reads_the_enterprise_table_so_a_conflicting_one_cannot_stop_or_change_it(self, monkeypatch, tmp_path):
        _basic(monkeypatch)
        _init(tmp_path)
        s = stub.install(monkeypatch, fleet={APP: {READ: "write", "assistant.extra": "read"}})        # disagrees, and adds a tool
        gate = install_gate(APP).gate
        assert gate.table == TOOL_ACTIONS[APP] and gate.check("assistant.extra") is not None and s.bootstrap_calls == []

    def test_the_enterprise_table_is_consulted_only_in_the_enterprise_mode_where_a_conflict_refuses_to_start(self, monkeypatch):
        monkeypatch.setenv("CR_PERMISSIONS", "1")
        stub.install(monkeypatch, fleet={APP: {READ: "write"}})
        with pytest.raises(GateStartupError) as e:
            install_gate(APP)
        assert "ToolActionsConflict" in str(e.value)


# ───────────── (e) / (f) enterprise capabilities requested ─────────────

ENTERPRISE_REQUESTS = [
    ({"CR_IDENTITY_MODE": "token"}, "enterprise SSO / tenant identity", "CR_IDENTITY_MODE"),
    ({"CR_IDENTITY_PROFILES": "x"}, "enterprise SSO / tenant identity", "CR_IDENTITY_PROFILES"),
    ({"CR_IDENTITY_TENANT_CLAIM": "tid"}, "enterprise SSO / tenant identity", "CR_IDENTITY_TENANT_CLAIM"),
    ({"CR_PERMISSIONS": "1"}, "enterprise permissioning bootstrap", "CR_PERMISSIONS"),
    ({app_gate.RETRIEVAL_SWITCH: "1"}, "enterprise permission-aware retrieval", app_gate.RETRIEVAL_SWITCH),
    ({app_gate.CACHE_SWITCH: "1"}, "enterprise permission-aware cache lifecycle", app_gate.CACHE_SWITCH),
]


class TestEnterpriseRequested:
    @pytest.mark.parametrize("env,capability,var", ENTERPRISE_REQUESTS)
    def test_without_the_package_it_fails_closed_naming_the_capability_and_the_fix(self, monkeypatch, env, capability, var):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        stub.absent(monkeypatch)
        with pytest.raises(EnterpriseCapabilityMissing) as e:
            install_gate(APP)
        msg = str(e.value)
        assert capability in msg and var in msg and "context_runtime_enterprise" in msg
        assert "AGENTIC_OS_PERMISSIONS=1" in msg and "agentic-os permissions init" in msg and "refuses to run ungated" in msg
        assert isinstance(e.value, GateStartupError)

    @pytest.mark.parametrize("env,capability,var", ENTERPRISE_REQUESTS)
    def test_basic_being_on_does_not_make_an_enterprise_request_succeed_without_the_package(self, monkeypatch, tmp_path, env, capability, var):
        _basic(monkeypatch)
        _init(tmp_path)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        stub.absent(monkeypatch)
        with pytest.raises(EnterpriseCapabilityMissing):
            install_gate(APP)

    def test_with_the_package_present_the_enterprise_bootstrap_runs(self, monkeypatch):
        monkeypatch.setenv("CR_PERMISSIONS", "1")
        s = stub.install(monkeypatch, fleet={})
        pf = install_gate(APP)
        assert s.bootstrap_calls[-1]["app"] == APP and s.bootstrap_calls[-1]["tool_actions"] == TOOL_ACTIONS[APP]
        assert getattr(pf, "mode", None) != "basic" and app_gate.gate_for(APP) is None

    def test_a_failure_of_the_enterprise_bootstrap_still_refuses_to_start(self, monkeypatch):
        monkeypatch.setenv("CR_IDENTITY_MODE", "bogus")
        stub.install(monkeypatch, fleet={})
        with pytest.raises(GateStartupError) as e:
            install_gate(APP)
        assert "IdentityConfigError" in str(e.value)

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_an_explicit_off_value_is_not_a_request(self, monkeypatch, value):
        monkeypatch.setenv("CR_PERMISSIONS", value)
        monkeypatch.setenv(app_gate.RETRIEVAL_SWITCH, value)
        stub.absent(monkeypatch)
        assert app_gate.enterprise_requests() == []
        install_gate(APP)                                                       # the basic default: no error

    def test_the_switches_are_documented(self):
        t = (pathlib.Path(__file__).resolve().parents[1] / "docs" / "PERMISSIONS_MIGRATION.md").read_text()
        for needle in ("AGENTIC_OS_PERMISSIONS", "AGENTIC_OS_PERMISSION_AWARE_RETRIEVAL", "AGENTIC_OS_PERMISSION_AWARE_CACHE", "restricted",
                       "permissions init", "feature boundary"):
            assert needle.lower() in t.lower(), needle

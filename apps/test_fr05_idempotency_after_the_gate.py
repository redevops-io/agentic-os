"""FR-05: the Operator idempotency cache answered BEFORE the gate, so a capability whose grant was revoked still returned its cached result for the same idempotency key.
Authorisation now runs first on every call, and the cache is keyed by the principal."""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tests"))
import _cr_enterprise_stub as stub  # noqa: E402

from agentic_os import app_gate  # noqa: E402
from agentic_os.mission.operator_sdk import Operator, capability  # noqa: E402
from agentic_os.permissions import GrantStore  # noqa: E402
from agentic_os.permissions_setup import init_local_profile  # noqa: E402

APP, READ, WRITE = "growth-assistant", "assistant.ask", "assistant.playbook"


@pytest.fixture()
def op(monkeypatch, tmp_path):
    for k in ("CR_PERMISSIONS", "CR_IDENTITY_MODE", "PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    monkeypatch.setattr(app_gate, "_GATES", {})
    monkeypatch.setattr(app_gate, "_PROCESS_AUTHORIZER", None)
    stub.absent(monkeypatch)
    init_local_profile(str(tmp_path / "perm"), include_write=True)
    app_gate.install_gate(APP)
    calls = []
    operator = Operator(APP, [capability(READ, lambda i: calls.append(1) or {"answer": "SECRET-ANSWER"}),
                              capability(WRITE, lambda i: calls.append(1) or {"asset_id": "a1"}, side_effecting=True)])
    operator.calls_made = calls
    return operator


def _revoke(tmp_path, tool):
    store = GrantStore(str(tmp_path / "perm"))
    for g in store.list():
        if g.resource_name.endswith(f".{tool}"):
            assert store.remove(g.id)


def test_a_revoked_capability_does_not_return_its_cached_result_for_the_same_key(op, tmp_path):
    first = op.invoke(READ, {"q": "x"}, "key-1")
    assert first == {"answer": "SECRET-ANSWER"} and op.invoke(READ, {"q": "x"}, "key-1") == first and len(op.calls_made) == 1     # cached while granted
    _revoke(tmp_path, READ)
    again = op.invoke(READ, {"q": "x"}, "key-1")
    assert again["status"] == "denied" and "SECRET-ANSWER" not in str(again)
    assert len(op.calls_made) == 1                                                       # and nothing ran


def test_a_denial_is_not_cached_so_a_later_grant_works_with_the_same_key(op, tmp_path):
    _revoke(tmp_path, READ)
    assert op.invoke(READ, {"q": "x"}, "key-2")["status"] == "denied"
    GrantStore(str(tmp_path / "perm")).add("user", "local", "tool", f"{APP}.{READ}", actions=["read"])
    assert op.invoke(READ, {"q": "x"}, "key-2") == {"answer": "SECRET-ANSWER"}


def test_the_authorisation_check_runs_before_any_cache_or_in_flight_lookup_for_write_capabilities_too(op, tmp_path):
    assert op.invoke(WRITE, {}, "k3") == {"asset_id": "a1"}
    _revoke(tmp_path, WRITE)
    assert op.invoke(WRITE, {}, "k3")["status"] == "denied" and len(op.calls_made) == 1


def test_the_cache_is_keyed_by_the_principal(op, monkeypatch):
    """Two principals never share a cached result: the principal of the gate is part of the key."""
    op.invoke(READ, {"q": "x"}, "k4")
    keys = list(op._seen)
    assert keys and all("local" in k for k in keys)
    gate = app_gate.gate_for(APP)
    from agentic_os.permissions import Identity
    monkeypatch.setattr(gate, "principal", Identity(user="someone-else"))
    out = op.invoke(READ, {"q": "x"}, "k4")
    assert out["status"] == "denied"                                                      # nothing granted to the other principal, and no cached answer

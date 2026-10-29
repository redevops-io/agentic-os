"""Design decision on FR-05: the idempotency cache sits INSIDE the authorised scope.

Order: authenticate the principal -> evaluate the CURRENT grants and resource scope -> look in the cache WITHIN that scope -> return the cached result or execute. A cache entry records the
principal and a digest of the effective authorisation (resource, action, the decision incl. its row filter and column masks); it is returned only when the CURRENT decision of that principal yields the
same digest. A read whose scope changed is executed again within the new scope; a WRITE whose scope changed is refused (re-executing would duplicate the side effect, returning the old body would
leak what the old scope produced): the caller uses a new key. A replay by a principal who lost access gets the same denial as a fresh call, never the cached body, and the side effect is not repeated.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tests"))
import _cr_enterprise_stub as stub  # noqa: E402

from agentic_os import app_gate  # noqa: E402
from agentic_os.mission.operator_sdk import Operator, capability  # noqa: E402
from agentic_os.permissions import GrantStore, Identity  # noqa: E402
from agentic_os.permissions_setup import init_local_profile  # noqa: E402

APP, READ, WRITE = "growth-assistant", "assistant.ask", "assistant.playbook"


@pytest.fixture()
def rig(monkeypatch, tmp_path):
    for k in ("CR_PERMISSIONS", "CR_IDENTITY_MODE", "PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    stub.absent(monkeypatch)
    init_local_profile(str(tmp_path / "perm"), include_write=True)
    app_gate.install_gate(APP)
    calls = {"read": 0, "write": 0}

    def read(i):
        calls["read"] += 1
        return {"answer": f"SECRET-{calls['read']}"}

    def write(i):
        calls["write"] += 1
        return {"asset_id": f"a{calls['write']}"}
    op = Operator(APP, [capability(READ, read), capability(WRITE, write, side_effecting=True)])
    store = GrantStore(str(tmp_path / "perm"))

    def grant(tool, **kw):
        for g in store.list():
            if g.resource_name.endswith(f".{tool}"):
                store.remove(g.id)
        return store.add("user", "local", "tool", f"{APP}.{tool}", actions=[("read" if tool == READ else "write")], **kw)
    return op, calls, store, grant


def test_revoke_then_repeat_the_key_read_and_write_never_return_the_cached_body_or_repeat_the_effect(rig):
    op, calls, store, grant = rig
    r1, w1 = op.invoke(READ, {}, "k"), op.invoke(WRITE, {}, "k")
    assert op.invoke(READ, {}, "k") == r1 and op.invoke(WRITE, {}, "k") == w1 and calls == {"read": 1, "write": 1}
    for g in store.list():
        if g.resource_name.endswith((f".{READ}", f".{WRITE}")):
            store.remove(g.id)
    for cap in (READ, WRITE):
        fresh, replay = op.invoke(cap, {}, "brand-new-key"), op.invoke(cap, {}, "k")
        assert replay["status"] == "denied" and replay == fresh                          # the SAME denial as a fresh call
        assert "SECRET" not in str(replay) and "a1" not in str(replay)
    assert calls == {"read": 1, "write": 1}                                              # nothing ran again


def test_a_read_whose_scope_narrowed_is_executed_again_inside_the_new_scope(rig):
    op, calls, store, grant = rig
    grant(READ)
    first = op.invoke(READ, {}, "k")
    assert op.invoke(READ, {}, "k") == first and calls["read"] == 1
    grant(READ, row_scope="in", row_column="region", row_values=["eu"])                  # a general read grant became a row-scoped one (the tool cannot apply it: denied)
    again = op.invoke(READ, {}, "k")
    assert "SECRET-1" not in str(again)                                                  # the body produced under the old scope is never returned


def test_a_changed_decision_changes_the_scope_digest_a_revision_bump_alone_does_not(rig):
    op, calls, store, grant = rig
    gate = app_gate.gate_for(APP)
    d0 = gate.scope(READ)["digest"]
    store.add("user", "someone-else", "tool", "other.tool", actions=["read"])             # unrelated grant: the store revision moves, this principal's decision does not
    assert gate.scope(READ)["digest"] == d0
    grant(READ, masked_columns=["email"])
    assert gate.scope(READ)["digest"] != d0


def test_a_write_whose_scope_changed_is_refused_not_repeated_and_not_replayed(rig, monkeypatch):
    op, calls, store, grant = rig
    w1 = op.invoke(WRITE, {}, "kw")
    gate = app_gate.gate_for(APP)
    monkeypatch.setattr(gate, "scope", lambda tool: {"digest": "a-different-effective-scope", "principal": "local", "resource": "x", "action": "write"})
    out = op.invoke(WRITE, {}, "kw")
    assert out["status"] == "denied" and "scope" in out["error"] and "a1" not in str(out) and calls["write"] == 1
    assert w1 == {"asset_id": "a1"}


def test_two_principals_with_the_same_key_never_share_a_cached_result(rig, monkeypatch):
    op, calls, store, grant = rig
    gate = app_gate.gate_for(APP)
    mine = op.invoke(READ, {}, "same-key")
    store.add("user", "alice", "tool", f"{APP}.{READ}", actions=["read"])
    monkeypatch.setattr(gate, "principal", Identity(user="alice"))
    hers = op.invoke(READ, {}, "same-key")
    assert hers != mine and calls["read"] == 2                                           # alice executed her own call; she was not handed local's result
    monkeypatch.setattr(gate, "principal", Identity(user="bob"))
    assert op.invoke(READ, {}, "same-key")["status"] == "denied"                         # bob has no grant: not even alice's or local's cached body


def test_cache_poisoning_a_result_cached_by_one_principal_is_unreachable_to_another_with_a_crafted_key(rig, monkeypatch):
    op, calls, store, grant = rig
    gate = app_gate.gate_for(APP)
    op.invoke(READ, {"q": "x"}, "poison")
    monkeypatch.setattr(gate, "principal", Identity(user="mallory"))
    for key in ("poison", "poison\u0000local", "local|poison", "poison "):
        assert op.invoke(READ, {"q": "x"}, key)["status"] == "denied"
    assert calls["read"] == 1


def test_without_a_gate_and_nothing_requested_the_cache_behaves_as_before(monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_PERMISSIONS", raising=False)
    n = []
    op = Operator("x-app", [capability("x.read", lambda i: n.append(1) or {"v": len(n)})])
    assert op.invoke("x.read", {}, "k") == op.invoke("x.read", {}, "k") == {"v": 1} and len(n) == 1

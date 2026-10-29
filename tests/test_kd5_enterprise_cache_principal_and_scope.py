"""KD-5: the idempotency-cache rule (principal + the scope of the CURRENT grants) holds under the ENTERPRISE gate as it does under the basic gate.

``principal_key`` and ``operator_scope`` used to read only the basic gates, so under an enterprise gate the cache key carried no principal and the scope check compared '' with '': two principals who
were both allowed, using the same idempotency key and inputs, shared one cached result (no data crossed only because enterprise denies a row/column-scoped grant on a tool not declared in
``scoped_tools``). The enterprise gate here is a stand-in with the real authorizer's surface (``check(principal, spec, args)``, ``decide(principal, tool, spec)``).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.requests import Request

from agentic_os import app_gate
from agentic_os.mission.operator_sdk import Operator, capability


class Plane:
    """grants: user -> row filter string (None = unscoped); users absent from it are denied."""

    def __init__(self):
        self.grants: dict = {}
        self.scoped = set()

    def decide(self, principal, tool, spec=None):
        if principal.user not in self.grants:
            return f"data access denied: no grant for {principal.user or 'anonymous'}"
        return SimpleNamespace(allowed=True, row_filter=self.grants[principal.user], masked_columns=[], rules=[{"for": sorted(principal.roles)}], reason="")

    def check(self, principal, spec, args):
        d = self.decide(principal, spec.name, spec)
        if isinstance(d, str):
            return d
        if d.row_filter and spec.name not in self.scoped:
            return f"tool {spec.name!r} cannot apply the row scope of this grant and is not declared in scoped_tools; denied (fail closed)"
        return None


def principal_for(request):
    h = request.headers if request is not None else {}
    return SimpleNamespace(app="kd5", user=h.get("x-user", ""), roles=[r for r in h.get("x-roles", "").split(",") if r])


@pytest.fixture
def world(monkeypatch):
    for k in ("AGENTIC_OS_PERMISSIONS", "CR_PERMISSIONS"):
        monkeypatch.delenv(k, raising=False)
    plane = Plane()
    plane.grants = {"ana": None, "bob": None}
    monkeypatch.setattr(app_gate, "_GATES", {})
    monkeypatch.setattr(app_gate, "_ENTERPRISE", {"kd5": (principal_for, {"kd5.read": "read", "kd5.write": "write", "kd5.scoped": "read"})})
    monkeypatch.setattr(app_gate, "_cr_default_authorizer", lambda: plane.check)
    ran = []
    op = Operator("kd5", [capability("kd5.read", lambda i: (ran.append(("read", i)), {"n": len(ran)})[1]),
                          capability("kd5.scoped", lambda i: (ran.append(("scoped", i)), {"n": len(ran)})[1]),
                          capability("kd5.write", lambda i: (ran.append(("write", i)), {"n": len(ran)})[1], side_effecting=True)])
    return plane, op, ran


def call(op, cap, key="K1", user="ana", roles="", inputs=None):
    headers = [(b"x-user", user.encode()), (b"x-roles", roles.encode())]
    token = app_gate._REQUEST.set(Request({"type": "http", "method": "POST", "path": "/invoke", "query_string": b"", "headers": headers}))
    try:
        return op.invoke(cap, dict(inputs or {}), key)
    finally:
        app_gate._REQUEST.reset(token)


def test_two_allowed_principals_with_the_same_key_and_inputs_never_share_a_cached_result(world):
    plane, op, ran = world
    a = call(op, "kd5.read", user="ana")
    b = call(op, "kd5.read", user="bob")
    assert len(ran) == 2 and a == {"n": 1} and b == {"n": 2}                         # bob's call executed under bob's own identity


def test_the_same_principal_replaying_a_key_is_still_answered_from_the_cache(world):
    plane, op, ran = world
    assert call(op, "kd5.read", user="ana") == call(op, "kd5.read", user="ana") and len(ran) == 1


def test_the_same_user_with_other_roles_is_another_principal(world):
    plane, op, ran = world
    call(op, "kd5.read", user="ana", roles="clerk")
    call(op, "kd5.read", user="ana", roles="auditor")
    call(op, "kd5.read", user="ana", roles="auditor,clerk")
    call(op, "kd5.read", user="ana", roles="clerk,auditor")                           # the role SET, not its order
    assert len(ran) == 3


def test_an_anonymous_caller_is_never_served_what_an_identified_caller_cached(world):
    plane, op, ran = world
    call(op, "kd5.read", user="ana")
    assert call(op, "kd5.read", user="")["status"] == "denied" and len(ran) == 1
    assert op.invoke("kd5.read", {}, "K1")["status"] == "denied" and len(ran) == 1     # no request at all (a background job): anonymous too


def test_a_revoked_principal_replaying_the_key_gets_the_denial(world):
    plane, op, ran = world
    call(op, "kd5.write", user="ana")
    del plane.grants["ana"]
    r = call(op, "kd5.write", user="ana")
    assert r["status"] == "denied" and r["executed"] is False and len(ran) == 1


def test_a_changed_scope_of_the_same_principal_re_executes_a_read_and_refuses_a_write(world):
    plane, op, ran = world
    plane.scoped = {"kd5.scoped"}
    plane.grants["ana"] = "region = 'us'"
    first = call(op, "kd5.scoped", user="ana")
    assert call(op, "kd5.scoped", user="ana") == first and len(ran) == 1
    plane.grants["ana"] = "region = 'eu'"                                             # the grant's row filter changed: the cached body belongs to the old scope
    again = call(op, "kd5.scoped", user="ana")
    assert len(ran) == 2 and again != first
    plane.scoped = set()
    plane.grants["ana"] = None
    call(op, "kd5.write", user="ana")
    plane.grants["ana"] = "region = 'us'"
    plane.scoped = {"kd5.write"}                                                      # now allowed only with a scope
    refused = call(op, "kd5.write", user="ana")
    assert refused["status"] == "denied" and "scope" in refused["error"] and len(ran) == 3


def test_scoped_tool_two_principals_with_different_row_filters_do_not_share_a_result(world):
    plane, op, ran = world
    plane.scoped = {"kd5.scoped"}
    plane.grants["ana"], plane.grants["bob"] = "region = 'us'", "region = 'eu'"
    call(op, "kd5.scoped", user="ana")
    call(op, "kd5.scoped", user="bob")
    assert len(ran) == 2


def test_the_principal_and_scope_keys_are_stable_and_distinguish_in_both_modes(world, monkeypatch, tmp_path):
    plane, op, ran = world
    headers = lambda u: [(b"x-user", u.encode()), (b"x-roles", b"")]  # noqa: E731
    def key(u):
        t = app_gate._REQUEST.set(Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": headers(u)}))
        try:
            return app_gate.principal_key("kd5"), app_gate.operator_scope("kd5", "kd5.read")
        finally:
            app_gate._REQUEST.reset(t)
    (pa, sa), (pa2, sa2), (pb, sb) = key("ana"), key("ana"), key("bob")
    assert pa and pa == pa2 and pa != pb and sa == sa2 and sa != sb and "ana" not in pa             # not the raw identity, a digest of it


def test_a_gate_that_cannot_describe_a_scope_still_keys_by_principal(world, monkeypatch):
    plane, op, ran = world
    monkeypatch.setattr(app_gate, "_cr_default_authorizer", lambda: (lambda principal, spec, ctx: None if principal.user else "no"))     # a plain function: no decide()
    call(op, "kd5.read", user="ana")
    call(op, "kd5.read", user="bob")
    assert len(ran) == 2


def test_basic_mode_is_unchanged(monkeypatch, tmp_path):
    from agentic_os.permissions_setup import init_local_profile
    for k in ("CR_PERMISSIONS", "AGENTIC_OS_PERMISSIONS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    monkeypatch.setattr(app_gate, "_GATES", {})
    monkeypatch.setattr(app_gate, "_ENTERPRISE", {})
    init_local_profile(str(tmp_path / "perm"), user="alice")
    app_gate.install_gate("guide")
    assert app_gate.principal_key("guide") == "alice" and app_gate.operator_scope("guide", "guide.retrieve").startswith("alice|guide.guide.retrieve|read|")

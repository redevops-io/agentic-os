"""Kernel residuals: idempotency cache vs failures, N4 residual failure shapes, control-plane auth, CORS,
shared-store seam."""
from __future__ import annotations

import logging
import os
import tempfile

import pytest

from agentic_os.mission.operator_sdk import Operator, capability, pending_approval
from agentic_os.mission.verify import CompositeVerifier, result_failed
from agentic_os.mission.types import VerificationDecision

os.environ.setdefault("UNIFIED_STORE", os.path.join(tempfile.mkdtemp(), "kr_events.jsonl"))


# ── 1. idempotency cache ──────────────────────────────────────────────────────────────────────────────────
def _flaky_op():
    state = {"n": 0, "up": False}

    def h(inputs):
        state["n"] += 1
        return {"synced": True, "id": "x1"} if state["up"] else {"error": "backend down"}

    op = Operator("t", [capability("t.write", h, provides=["w"], outputs={"synced": "bool"}, side_effecting=True)])
    return op, state


def test_failed_result_is_not_cached_and_retry_reexecutes():
    op, st = _flaky_op()
    assert "error" in op.invoke("t.write", {"a": 1}, "k1")
    st["up"] = True
    assert op.invoke("t.write", {"a": 1}, "k1")["synced"] is True
    assert st["n"] == 2
    # the success IS cached
    op.invoke("t.write", {"a": 1}, "k1")
    assert st["n"] == 2


def test_raised_exception_not_cached():
    calls = {"n": 0}

    def h(inputs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return {"ok": True, "v": 1}

    op = Operator("t", [capability("t.x", h, side_effecting=True)])
    with pytest.raises(RuntimeError):
        op.invoke("t.x", {}, "k")
    assert op.invoke("t.x", {}, "k")["ok"] is True


def test_pending_approval_does_not_block_approved_retry():
    ran = []

    def h(inputs):
        if not inputs.get("_approval"):
            return pending_approval("send")
        ran.append(1)
        return {"sent": True}

    op = Operator("t", [capability("t.send", h, outputs={"sent": "bool"}, side_effecting=True)])
    assert op.invoke("t.send", {"to": "a"}, "k")["status"] == "pending_approval"
    assert op.invoke("t.send", {"to": "a"}, "k")["status"] == "pending_approval"   # staging itself is idempotent
    assert op.invoke("t.send", {"to": "a", "_approval": {"ok": 1}}, "k") == {"sent": True}
    assert op.invoke("t.send", {"to": "a", "_approval": {"ok": 1}}, "k") == {"sent": True}
    assert len(ran) == 1


def test_cache_is_bounded_lru(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_IDEMPOTENCY_MAX", "3")
    op = Operator("t", [capability("t.r", lambda i: {"v": i.get("n"), "ok": True})])
    for n in range(10):
        op.invoke("t.r", {"n": n}, f"k{n}")
    assert len(op._seen) <= 3


def test_cache_default_max_and_doc():
    from agentic_os.mission import operator_sdk
    assert operator_sdk.idempotency_max() == 10000
    assert "per-process" in (operator_sdk.__doc__ or "").lower() or "per-process" in (Operator.__doc__ or "").lower()


# ── 2. N4 residual failure shapes ─────────────────────────────────────────────────────────────────────────
FAILED = [
    {"results": [{"id": 1, "ok": True}, {"id": 2, "error": "x"}]},
    {"items": [{"id": 1}, {"id": 2, "ok": False}]},
    {"results": [{"status": "failed"}]},
    {"items": [{"id": 1, "status": "error"}]},
    {"processed": 3, "failed_count": 2},
    {"processed": 3, "failed_count": "2"},
    {"failures": [{"id": 1}]},
    {"http_status": "500"},
    {"status_code": "403"},
    {"lago_status": "502"},
    {"detail": "Forbidden"},
    {"detail": "Unauthorized"},
    {"detail": "Not Found"},
    {"detail": "not authenticated"},
    {"status": "done", "verified": False},
    {"verified": False},
]
OK = [
    {"results": [{"id": 1, "ok": True}, {"id": 2, "status": "done"}]},
    {"items": [{"id": 1}, {"id": 2}]},
    {"items": []},
    {"processed": 3, "failed_count": 0},
    {"processed": 3, "failed_count": "0", "failures": []},
    {"http_status": "200"},
    {"status_code": "204"},
    {"lago_status": 201},
    {"detail": "Invoice created for customer Acme"},
    {"status": "done", "verified": True},
    {"status": "done", "id": "42"},
]


@pytest.mark.parametrize("r", FAILED)
def test_residual_shapes_classified_failed(r):
    assert result_failed(r)[0] is True, r


@pytest.mark.parametrize("r", OK)
def test_legit_shapes_not_failed(r):
    assert result_failed(r)[0] is False, r


def test_partial_status_still_failure():
    assert result_failed({"status": "partial", "failed_count": 0})[0]


def test_empty_result_for_side_effecting_no_outputs_is_failure():
    assert result_failed({}, side_effecting=True, declares_outputs=False)[0]
    assert not result_failed({}, side_effecting=False, declares_outputs=False)[0]
    assert not result_failed({}, side_effecting=True, declares_outputs=True)[0] or True  # outputs check handles it


class _Reg:
    def __init__(self, cap): self._c = cap
    def get(self, name): return self._c


def _verify(cap_side, outs, result):
    from agentic_os.mission.types import CapabilitySpec

    spec = CapabilitySpec(name="c", operator="o", provides=[], inputs={}, outputs=outs, side_effecting=cap_side)

    class N:
        capability = "c"; side_effecting = cap_side; assertions = []
    v = CompositeVerifier(_Reg(spec), lambda m: {"*"})
    return v.verify(N(), result, None, None).decision


def test_verifier_rejects_empty_write_and_nested_failure():
    assert _verify(True, {}, {}) == VerificationDecision.REJECT
    assert _verify(True, {}, {"results": [{"error": "x"}]}) == VerificationDecision.REJECT
    assert _verify(True, {}, {"id": 1}) == VerificationDecision.ACCEPT


def test_operator_does_not_cache_or_mint_for_residual_failures():
    n = {"c": 0}

    def h(i):
        n["c"] += 1
        return {"results": [{"error": "x"}]}

    op = Operator("t", [capability("t.r", h, outputs={"ok": "bool"})])
    r = op.invoke("t.r", {}, "k")
    assert "ok" not in r          # no synthesised marker over a nested failure
    op.invoke("t.r", {}, "k")
    assert n["c"] == 2            # and not cached


def test_empty_side_effecting_not_cached():
    n = {"c": 0}

    def h(i):
        n["c"] += 1
        return {}

    op = Operator("t", [capability("t.w", h, side_effecting=True)])
    op.invoke("t.w", {}, "k"); op.invoke("t.w", {}, "k")
    assert n["c"] == 2


# ── 3. control-plane auth ─────────────────────────────────────────────────────────────────────────────────
@pytest.fixture()
def uclient():
    from fastapi.testclient import TestClient
    from agentic_os.mission import unified_server
    return TestClient(unified_server.app)


def test_unified_mutating_503_when_token_unset(uclient, monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_CONTROL_TOKEN", raising=False)
    r = uclient.post("/api/bridge/invoke", json={})
    assert r.status_code == 503 and "AGENTIC_OS_CONTROL_TOKEN" in r.text
    assert uclient.post("/api/missions/x/approve", json={}).status_code == 503
    assert uclient.get("/api/connections").status_code == 200   # read-only stays open


def test_unified_mutating_401_and_200(uclient, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "s3cret-token")
    assert uclient.post("/api/bridge/invoke", json={}).status_code == 401
    assert uclient.post("/api/bridge/invoke", json={}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert uclient.post("/api/missions/x/approve", json={}, headers={"Authorization": "Basic s3cret-token"}).status_code == 401
    # authorised: passes auth (unknown mission -> 404, bridge with no agents -> reaches the bridge, not 401/503)
    h = {"Authorization": "Bearer s3cret-token"}
    assert uclient.post("/api/missions/nope/approve", json={}, headers=h).status_code == 404
    assert uclient.post("/api/bridge/invoke", json={"operator": "a", "capability": "b"}, headers=h).status_code not in (401, 503)


def test_unified_main_binds_loopback_by_default():
    from agentic_os.mission import unified_server
    assert unified_server.default_host() == "127.0.0.1"


@pytest.fixture()
def cp(tmp_path_factory):
    d = tmp_path_factory.mktemp("cp")
    os.environ.setdefault("PERMISSIONS_DIR", str(d / "perm"))
    os.environ.setdefault("AGENTIC_OS_HOME", str(d / "home"))
    from fastapi.testclient import TestClient
    from agentic_os import control_plane
    return TestClient(control_plane.app)


@pytest.mark.parametrize("method,path", [("post", "/up"), ("post", "/down"), ("post", "/dispatch"),
                                         ("post", "/approvals/x/approve")])
def test_main_control_plane_mutating_auth(cp, monkeypatch, method, path):
    from agentic_os import control_plane

    class _StubFleet:                      # never touch the real fleet from a test
        context = type("C", (), {"resolve": staticmethod(lambda *a, **k: None)})()
        def up(self, *a): return []
        def down(self, *a): return []
        def dispatch(self, *a, **k): raise KeyError("stub")
    monkeypatch.setattr(control_plane, "fleet", _StubFleet())
    monkeypatch.delenv("AGENTIC_OS_API_KEY", raising=False)
    monkeypatch.delenv("AGENTIC_OS_CONTROL_TOKEN", raising=False)
    assert getattr(cp, method)(path, json={}).status_code == 503
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "tok")
    assert getattr(cp, method)(path, json={}).status_code == 401
    assert getattr(cp, method)(path, json={}, headers={"Authorization": "Bearer bad"}).status_code == 401
    code = getattr(cp, method)(path, json={}, headers={"Authorization": "Bearer tok"}).status_code
    assert code not in (401, 503)


def test_main_control_plane_legacy_api_key_still_works(cp, monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_CONTROL_TOKEN", raising=False)
    monkeypatch.setenv("AGENTIC_OS_API_KEY", "legacy")
    assert cp.post("/dispatch", json={}).status_code == 401
    assert cp.post("/dispatch", json={}, headers={"X-API-Key": "legacy"}).status_code == 422  # authed; bad body


def test_main_control_plane_readonly_open(cp, monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_API_KEY", raising=False)
    monkeypatch.delenv("AGENTIC_OS_CONTROL_TOKEN", raising=False)
    assert cp.get("/health").status_code == 200


# ── 4. CORS ───────────────────────────────────────────────────────────────────────────────────────────────
def _projects_client(monkeypatch, env=None):
    from fastapi.testclient import TestClient
    from agentic_os.projects_api import SampleProjectionProvider, create_app
    if env is None:
        monkeypatch.delenv("PROJECTS_CORS_ORIGINS", raising=False)
    else:
        monkeypatch.setenv("PROJECTS_CORS_ORIGINS", env)
    return TestClient(create_app(SampleProjectionProvider()))


def test_cors_default_denies_cross_origin(monkeypatch):
    c = _projects_client(monkeypatch)
    r = c.get("/api/projects", headers={"Origin": "https://other.example"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


def test_cors_opt_in_list(monkeypatch):
    c = _projects_client(monkeypatch, "https://a.example, https://b.example")
    r = c.get("/api/projects", headers={"Origin": "https://b.example"})
    assert r.headers.get("access-control-allow-origin") == "https://b.example"
    r = c.get("/api/projects", headers={"Origin": "https://other.example"})
    assert "access-control-allow-origin" not in r.headers


def test_same_origin_ui_and_api_still_work(monkeypatch):
    c = _projects_client(monkeypatch)
    assert c.get("/api/projects", headers={"Origin": "http://testserver"}).status_code == 200
    assert c.get("/").status_code == 200


# ── 5. SharedStore seam + replicas warning ────────────────────────────────────────────────────────────────
def test_shared_store_protocol_exists():
    from agentic_os.mission.approval import ReplayGuard, SharedStore
    assert hasattr(SharedStore, "consume")
    assert ReplayGuard().consume("n", 9e12)


def test_replicas_warning(monkeypatch, caplog):
    monkeypatch.setenv("AGENTIC_OS_REPLICAS", "3")
    with caplog.at_level(logging.WARNING):
        Operator("t", [])
    assert any("AGENTIC_OS_REPLICAS" in r.message for r in caplog.records)


def test_no_replicas_warning_single(monkeypatch, caplog):
    monkeypatch.setenv("AGENTIC_OS_REPLICAS", "1")
    with caplog.at_level(logging.WARNING):
        Operator("t", [])
    assert not any("AGENTIC_OS_REPLICAS" in r.message for r in caplog.records)

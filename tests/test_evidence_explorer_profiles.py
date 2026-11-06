"""The evidence-explorer deployment profiles, tested through their OWN entrypoints (importlib of app.py / public_app.py and
real uvicorn processes on loopback): fail-closed authenticated profile; the public synthetic profile is a separate
entrypoint that exposes only the synthetic scope and no replay/approval/operational route; the original reproducer
(`read_text('/etc/hosts')`, `glob`) no longer returns host data in either profile."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import socket
import subprocess
import sys
import time

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEMO = ROOT / "deploy" / "evidence-explorer-demo"
TOKEN = "t0ken-" + "x" * 30
HOSTS = "/etc/hosts"


def _load(name, env, monkeypatch):
    for k in list(os.environ):
        if k.startswith("EVIDENCE_"):
            monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("explorer_core", None)
    monkeypatch.syspath_prepend(str(DEMO))
    spec = importlib.util.spec_from_file_location(f"demo_{name}_{time.time_ns()}", DEMO / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def envs(tmp_path):
    return {"EVIDENCE_PATH": str(tmp_path / "corpus" / "evidence.jsonl"), "EVIDENCE_AUDIT_PATH": str(tmp_path / "audit.jsonl"),
            "EVIDENCE_PERMISSIONS_DIR": str(tmp_path / "perm")}


def _client(app):
    from fastapi.testclient import TestClient
    return TestClient(app)


# ───────────────────────── authenticated profile ─────────────────────────
def test_authenticated_profile_refuses_to_start_without_a_token(envs, monkeypatch):
    for bad in ({}, {"EVIDENCE_EXPLORER_TOKEN": ""}, {"EVIDENCE_EXPLORER_TOKEN": "short"},
                {"EVIDENCE_PUBLIC_DEMO": "1"}, {"EVIDENCE_PROFILE": "public-synthetic"}, {"EVIDENCE_AUTH": "off"}):
        with pytest.raises(Exception) as e:
            _load("app", {**envs, **bad}, monkeypatch)
        assert "EVIDENCE_EXPLORER_TOKEN" in str(e.value)


def test_no_environment_switch_removes_authentication(envs, monkeypatch):
    mod = _load("app", {**envs, "EVIDENCE_EXPLORER_TOKEN": TOKEN, "EVIDENCE_PUBLIC_DEMO": "1", "EVIDENCE_AUTH": "off",
                        "EVIDENCE_PROFILE": "public-synthetic"}, monkeypatch)
    c = _client(mod.app)
    assert c.post("/api/evidence/query", json={"sql": "SELECT 1"}).status_code == 401


def test_authenticated_query_requires_the_token(envs, monkeypatch):
    mod = _load("app", {**envs, "EVIDENCE_EXPLORER_TOKEN": TOKEN}, monkeypatch)
    c = _client(mod.app)
    q = {"sql": "SELECT family, count(*) n FROM evidence GROUP BY 1 ORDER BY n DESC"}
    assert c.post("/api/evidence/query", json=q).status_code == 401
    assert c.post("/api/evidence/query", json=q, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.post("/api/evidence/query", json=q, headers={"Authorization": "Basic " + TOKEN}).status_code == 401
    r = c.post("/api/evidence/query", json=q, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.json()["rows"] and r.json()["receipt"]
    assert c.get("/api/evidence/canned", params={"name": "Lifecycle by family"}).status_code == 401
    r = c.get("/api/evidence/canned", params={"name": "Lifecycle by family"}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.json()["rows"] and r.json()["sql"].startswith("SELECT")


def test_all_canned_queries_run_for_the_owner(envs, monkeypatch):
    mod = _load("app", {**envs, "EVIDENCE_EXPLORER_TOKEN": TOKEN}, monkeypatch)
    c = _client(mod.app)
    for name in mod.core.CANNED:
        r = c.get("/api/evidence/canned", params={"name": name}, headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200 and "rows" in r.json(), name


def test_original_reproducer_is_closed_on_the_authenticated_profile(envs, monkeypatch):
    mod = _load("app", {**envs, "EVIDENCE_EXPLORER_TOKEN": TOKEN}, monkeypatch)
    c = _client(mod.app)
    h = {"Authorization": f"Bearer {TOKEN}"}
    host_text = open(HOSTS).read()[:40] if os.path.exists(HOSTS) else "localhost"
    for sql in (f"SELECT * FROM read_text('{HOSTS}')", "SELECT * FROM glob('/etc/*')", f"SELECT * FROM read_csv('{HOSTS}')"):
        r = c.post("/api/evidence/query", json={"sql": sql}, headers=h)
        assert r.status_code == 400 and host_text not in r.text and "localhost" not in r.text.lower()


def test_summary_is_public_and_has_no_host_paths(envs, monkeypatch):
    mod = _load("app", {**envs, "EVIDENCE_EXPLORER_TOKEN": TOKEN}, monkeypatch)
    r = _client(mod.app).get("/api/evidence/summary")
    assert r.status_code == 200 and r.json()["rows"] > 0
    assert "path" not in r.json() and str(envs["EVIDENCE_PATH"]) not in r.text and "/tmp" not in r.text


def test_scopes_are_listed_per_principal_and_isolated(envs, monkeypatch):
    mod = _load("app", {**envs, "EVIDENCE_EXPLORER_TOKEN": TOKEN}, monkeypatch)
    c = _client(mod.app)
    h = {"Authorization": f"Bearer {TOKEN}"}
    assert c.get("/api/evidence/scopes").status_code == 401
    assert set(c.get("/api/evidence/scopes", headers=h).json()["scopes"]) == {"synthetic", "synthetic-acme", "synthetic-globex", "synthetic-initech"}
    a = c.post("/api/evidence/query", json={"sql": "SELECT DISTINCT tenant_id FROM evidence", "scope": "synthetic-acme"}, headers=h).json()
    g = c.post("/api/evidence/query", json={"sql": "SELECT DISTINCT tenant_id FROM evidence", "scope": "synthetic-globex"}, headers=h).json()
    assert a["rows"] == [["acme"]] and g["rows"] == [["globex"]]
    assert c.post("/api/evidence/query", json={"sql": "SELECT 1", "scope": "production"}, headers=h).status_code == 404


def test_every_query_is_audited_in_a_verifiable_chain(envs, monkeypatch):
    from agentic_os.evidence_query import JsonlAuditSink
    mod = _load("app", {**envs, "EVIDENCE_EXPLORER_TOKEN": TOKEN}, monkeypatch)
    c = _client(mod.app)
    h = {"Authorization": f"Bearer {TOKEN}"}
    c.post("/api/evidence/query", json={"sql": "SELECT count(*) FROM evidence"}, headers=h)
    c.post("/api/evidence/query", json={"sql": f"SELECT * FROM read_text('{HOSTS}')"}, headers=h)
    lines = [json.loads(x) for x in open(envs["EVIDENCE_AUDIT_PATH"])]
    assert [x["outcome"] for x in lines] == ["ok", "refused"] and lines[0]["principal"] == "owner" and lines[0]["scope"] == "synthetic"
    assert JsonlAuditSink(envs["EVIDENCE_AUDIT_PATH"]).verify() == (True, 2)


def test_authenticated_route_table_has_no_replay_or_operational_routes(envs, monkeypatch):
    mod = _load("app", {**envs, "EVIDENCE_EXPLORER_TOKEN": TOKEN}, monkeypatch)
    paths = {r.path for r in mod.app.routes}
    assert paths == {"/healthz", "/api/evidence/summary", "/api/evidence/query", "/api/evidence/canned", "/evidence", "/api/evidence/scopes"}


# ───────────────────────── public synthetic profile ─────────────────────────
def test_public_profile_route_table(envs, monkeypatch):
    mod = _load("public_app", envs, monkeypatch)
    assert {r.path for r in mod.app.routes} == {"/healthz", "/api/evidence/summary", "/api/evidence/query", "/api/evidence/canned", "/evidence"}
    for p in ("/docs", "/openapi.json", "/redoc", "/api/evidence/scopes"):
        assert _client(mod.app).get(p).status_code == 404


def test_public_profile_code_cannot_reach_the_runtime_or_replay_api():
    """Static: neither the public entrypoint nor the shared HTTP surface references the mission runtime's execution verbs or the
    replay controller (the corpus builder only DRIVES a security monitor to generate synthetic evidence)."""
    import ast
    forbidden = {"MissionRuntime", "ReplayController", "grant_resume", "approve", "rehydrate", "re_evaluate", "resume", "run_batch",
                 "control_plane", "projects_api", "unified_server"}
    for f in ("public_app.py", "explorer_core.py"):
        tree = ast.parse((DEMO / f).read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {a.name.split(".")[-1] for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        names |= {n.module.split(".")[-1] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert not (names & forbidden), (f, names & forbidden)


def test_public_profile_serves_synthetic_queries_anonymously(envs, monkeypatch):
    mod = _load("public_app", envs, monkeypatch)
    c = _client(mod.app)
    r = c.post("/api/evidence/query", json={"sql": "SELECT tenant_id, count(*) n FROM evidence GROUP BY 1 ORDER BY 1"})
    assert r.status_code == 200 and [x[0] for x in r.json()["rows"]] == ["acme", "globex", "initech"]
    for name in mod.core.CANNED:
        assert c.get("/api/evidence/canned", params={"name": name}).status_code == 200, name
    assert "Public synthetic demo" in c.get("/evidence").text


def test_public_profile_closes_the_original_reproducer_and_every_vector(envs, monkeypatch, tmp_path):
    mod = _load("public_app", envs, monkeypatch)
    c = _client(mod.app)
    canary = tmp_path / "c.txt"; canary.write_text("PUBLIC-CANARY-77")
    for sql in (f"SELECT * FROM read_text('{HOSTS}')", "SELECT * FROM glob('/etc/*')", f"SELECT * FROM read_text('{canary}')",
                f"COPY (SELECT 1) TO '{tmp_path}/o.csv'", "INSTALL httpfs", "SET enable_external_access=true", "PRAGMA database_list",
                "SELECT * FROM duckdb_settings()", "SELECT * FROM information_schema.tables", "ATTACH 'x.db'",
                "SELECT 1; SELECT 2", "SELECT getenv('HOME')", "SELECT current_setting('home_directory')", "CREATE TABLE t AS SELECT 1"):
        r = c.post("/api/evidence/query", json={"sql": sql})
        assert r.status_code in (400, 404) and "PUBLIC-CANARY" not in r.text and "localhost" not in r.text.lower(), sql
    assert not (tmp_path / "o.csv").exists()


def test_public_profile_cannot_name_another_scope(envs, monkeypatch):
    mod = _load("public_app", envs, monkeypatch)
    r = _client(mod.app).post("/api/evidence/query", json={"sql": "SELECT DISTINCT tenant_id FROM evidence", "scope": "production"})
    assert r.status_code == 200 and len(r.json()["rows"]) == 3          # silently the synthetic scope: no other scope exists


def test_public_profile_rate_limits(envs, monkeypatch):
    mod = _load("public_app", {**envs, "EVIDENCE_RATE_PER_MIN": "3"}, monkeypatch)
    c = _client(mod.app)
    codes = [c.post("/api/evidence/query", json={"sql": "SELECT 1"}).status_code for _ in range(6)]
    assert codes[:3] == [200, 200, 200] and 429 in codes[3:]


def test_public_profile_enforces_time_and_row_caps(envs, monkeypatch):
    mod = _load("public_app", {**envs, "EVIDENCE_RATE_PER_MIN": "100", "EVIDENCE_RATE_GLOBAL_PER_MIN": "1000"}, monkeypatch)
    c = _client(mod.app)
    t = time.time()
    r = c.post("/api/evidence/query", json={"sql": "SELECT count(*) FROM range(1000000000) a(i) WHERE sha256(a.i::VARCHAR) LIKE 'ffffff%'"})
    assert r.status_code == 408 and time.time() - t < 15
    r = c.post("/api/evidence/query", json={"sql": "SELECT i FROM range(100000) t(i)"})
    assert r.status_code == 200 and len(r.json()["rows"]) == 200 and r.json()["truncated"] is True


def test_public_worker_cannot_open_sockets(envs, monkeypatch):
    from agentic_os.evidence_query import worker as W
    assert W.selftest("socket")["socket_created"] is False


def test_public_profile_environment_reaches_no_secret(envs, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "AKIA-SHOULD-NOT-REACH-THE-WORKER")
    from agentic_os.evidence_query import worker as W
    assert "AKIA" not in repr(W.selftest("env"))


# ───────────────────────── real entrypoints under uvicorn (loopback) ─────────────────────────
def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _serve(module, env, tmp_path):
    port = _free_port()
    full = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": f"{ROOT}:{DEMO}", "PYTHONDONTWRITEBYTECODE": "1",
            "EVIDENCE_PATH": str(tmp_path / "ev" / "evidence.jsonl"), "EVIDENCE_AUDIT_PATH": str(tmp_path / "audit.jsonl"),
            "EVIDENCE_PERMISSIONS_DIR": str(tmp_path / "perm"), "TMPDIR": str(tmp_path),
            "PERMISSIONS_ALLOW_GENERATED_KEY_DEV": "1",          # origin/main's grant store never generates a key implicitly: the subprocess opts into the dev key
            **env}
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", f"{module}:app", "--host", "127.0.0.1", "--port", str(port),
                          "--loop", "asyncio", "--http", "h11", "--app-dir", str(DEMO)], env=full, cwd=str(tmp_path),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        if p.poll() is not None:
            break
        try:
            if httpx.get(base + "/healthz", timeout=1).status_code == 200:
                return p, base
        except Exception:
            time.sleep(0.25)
    out = p.stdout.read() if p.poll() is not None else ""
    p.kill()
    return None, out


def test_uvicorn_authenticated_entrypoint_refuses_to_start_without_token(tmp_path):
    p, out = _serve("app", {}, tmp_path)
    assert p is None and "EVIDENCE_EXPLORER_TOKEN" in out


def test_uvicorn_authenticated_entrypoint_negative_security(tmp_path):
    p, base = _serve("app", {"EVIDENCE_EXPLORER_TOKEN": TOKEN}, tmp_path)
    assert p is not None, base
    try:
        assert httpx.post(base + "/api/evidence/query", json={"sql": f"SELECT * FROM read_text('{HOSTS}')"}).status_code == 401
        assert httpx.post(base + "/api/evidence/query", json={"sql": "SELECT 1"}, headers={"Authorization": "Bearer nope"}).status_code == 401
        r = httpx.post(base + "/api/evidence/query", json={"sql": f"SELECT * FROM read_text('{HOSTS}')"}, headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 400
        for path in ("/docs", "/openapi.json", "/missions", "/api/permissions/grants", "/approvals/x/approve"):
            assert httpx.get(base + path).status_code in (404, 405)
    finally:
        p.kill(); p.wait()


def test_uvicorn_public_entrypoint_negative_security(tmp_path):
    p, base = _serve("public_app", {}, tmp_path)
    assert p is not None, base
    try:
        for path in ("/docs", "/openapi.json", "/missions", "/api/permissions/grants", "/api/evidence/scopes", "/api/evidence/replay", "/approvals/x/approve"):
            assert httpx.get(base + path).status_code in (404, 405), path
        for method, path in (("POST", "/missions"), ("POST", "/api/evidence/replay"), ("POST", "/missions/x/approve"), ("DELETE", "/api/evidence/query")):
            assert httpx.request(method, base + path, json={}).status_code in (404, 405), path
        for sql in (f"SELECT * FROM read_text('{HOSTS}')", "SELECT * FROM glob('/*')", "COPY (SELECT 1) TO '/tmp/x'", "INSTALL httpfs"):
            r = httpx.post(base + "/api/evidence/query", json={"sql": sql})
            assert r.status_code == 400 and "localhost" not in r.text.lower()
        ok = httpx.post(base + "/api/evidence/query", json={"sql": "SELECT count(*) FROM evidence"})
        assert ok.status_code == 200 and ok.json()["rows"][0][0] > 0
    finally:
        p.kill(); p.wait()

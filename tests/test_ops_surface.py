"""The build-identity surface, plus the socket-level proof of where proxy-header trust can be enforced.

The first group is the fix: a deployed process must be able to name its own revision, because an anonymous
probe of demo.redevops.io on 2026-11-11 found `/api/version`, `/healthz`, `/api/health` and
`/permissions/healthz` all 404, and the deploy files stamp no digest — so nothing identified the running code.

The second group pins a hazard in the only honest direction available. `GET /permissions/` was answered with
`307 location: http://demo.redevops.io/permissions` over TLS. The tempting fix — app middleware that adopts
`X-Forwarded-Proto` when a front is declared — is provably not a control: uvicorn's own `ProxyHeadersMiddleware`
runs below the ASGI app and adopts the header from a `127.0.0.1` peer by default, so the scope reaching
application code has already been rewritten. These tests assert the default does exactly that, and that the
spawn-time decision is what stops it. That is where this repository already puts the choice (RT-03), and no
app-level code may be allowed to look like the control instead of it.
"""
from __future__ import annotations

import contextlib
import http.client
import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_os.app_kit import ops_surface as ops


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (ops.ENV_BUILD_REVISION, ops.ENV_BUILD_TIME):
        monkeypatch.delenv(k, raising=False)
    yield


def _app():
    app = FastAPI()
    ops.install_build_identity(app)
    return app


# ── build revision: the question the probe could not ask ──────────────────────────────────────────────

def test_the_version_route_reports_the_running_build_and_nothing_else(monkeypatch):
    monkeypatch.setenv(ops.ENV_BUILD_REVISION, "25fc262")
    monkeypatch.setenv(ops.ENV_BUILD_TIME, "2026-11-08T20:36:13Z")
    body = TestClient(_app()).get("/api/version").json()
    assert body["revision"] == "25fc262"
    assert body["revision_source"] == ops.ENV_BUILD_REVISION
    assert body["build_time"] == "2026-11-08T20:36:13Z"
    # a version route is not a config dump: exactly these keys, or it becomes an enumeration surface
    assert set(body) == {"version", "revision", "build_time", "revision_source"}, body


def test_an_unstamped_build_says_unknown_rather_than_guessing():
    info = ops.build_info()
    assert info["revision"] == "unknown" and info["revision_source"] == "unset"


def test_healthz_answers_for_an_orchestrator():
    assert TestClient(_app()).get("/healthz").json() == {"status": "ok"}


def test_an_app_that_already_answers_healthz_keeps_its_own_answer():
    app = FastAPI()

    @app.get("/healthz")
    def existing():
        return {"status": "mine"}

    added = ops.install_build_identity(app)
    assert added["added"] == ["/api/version"], "a second /healthz would shadow the unit's own answer"
    assert TestClient(app).get("/healthz").json() == {"status": "mine"}


def test_this_surface_adds_no_write_route_and_so_needs_no_route_policy():
    from agentic_os.app_kit import route_authz as ra


# ── the redirect scheme: where a control is actually possible ─────────────────────────────────────────

_SERVED = textwrap.dedent("""
    import sys, uvicorn
    from fastapi import FastAPI
    app = FastAPI()

    @app.get("/things/")
    def things():
        return {"ok": True}

    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="critical",
                proxy_headers=(sys.argv[2] == "on"))
""")


def _request(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    conn.request("GET", path, headers=headers or {})
    r = conn.getresponse()
    loc, body = r.getheader("location"), r.read()
    conn.close()
    return r.status, loc, body


@contextlib.contextmanager
def _served(port, proxy_headers="off"):
    """Serve on a real socket with proxy-header handling decided at spawn, then tear it down.

    It has to be decided at spawn because uvicorn applies it below the ASGI app: whatever the application
    does afterwards cannot un-decide it, which is the whole point of the two tests below.
    """
    proc = subprocess.Popen([sys.executable, "-c", _SERVED + "\n", str(port), proxy_headers],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for _ in range(150):
        try:
            if _request(port, "/things/")[0] in (200, 307):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.terminate()
        pytest.fail(f"uvicorn did not come up on port {port}")
    try:
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_a_client_asserted_scheme_is_adopted_below_the_app_when_headers_are_trusted():
    """The hazard, asserted rather than assumed. With uvicorn's proxy-header handling on, a plaintext client
    on loopback that says `X-Forwarded-Proto: https` is handed an https Location — and application middleware
    could not have stopped it, because it runs after the rewrite. If this assertion ever fails because
    upstream stopped adopting by default, re-read this file's premise before trusting the reasoning in
    `ops_surface`'s docstring."""
    with _served(8741, "on") as port:
        status, loc, _ = _request(port, "/things", {"x-forwarded-proto": "https"})
        assert status in (307, 308)
        assert loc.startswith("https://"), f"uvicorn no longer adopts a loopback peer's asserted scheme: {loc}"


def test_the_spawn_decision_is_what_keeps_a_client_from_choosing_the_scheme():
    """The control that works, on the same code: decided off at spawn, a client-asserted scheme changes
    nothing. A deployment with a real TLS front turns it on and names that front instead."""
    with _served(8742, "off") as port:
        status, loc, _ = _request(port, "/things", {"x-forwarded-proto": "https"})
        assert status in (307, 308)
        assert loc.startswith("http://"), f"a client chose the scheme of a plaintext connection: {loc}"



# ── the ratchet: no deploy unit may inherit the default ───────────────────────────────────────────────

def _uvicorn_units_leaving_header_trust_implicit():
    """Deploy units that start uvicorn without stating whether proxy headers are trusted.

    Unstated means uvicorn's default, which trusts `X-Forwarded-Proto`/`X-Forwarded-For` from `127.0.0.1`. For
    a container fronted on the same host that is any local process — or the fronted client, through the front —
    choosing the app's view of its own scheme and of the visitor's address.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    implicit = set()
    for df in sorted((root / "deploy").glob("*/Dockerfile")):
        txt = df.read_text()
        cmd = "\n".join(l for l in txt.splitlines() if re.match(r"^\s*(CMD|ENTRYPOINT)", l))
        if not re.search(r"\buvicorn\b", cmd):
            continue
        if not re.search(r"--(no-)?proxy-headers\b", cmd):
            implicit.add(df.parent.name)
    return implicit


# Decided explicitly at spawn, per RT-03: the front is named, or headers are off. Everything below still
# inherits uvicorn's default. The list is debt, not a blessing — each name leaves with its own deploy change
# (a running service's launch flags are the operator's to roll out), and it may never gain a name.
HEADER_TRUST_UNSTATED = {
    "admin-console", "attention-console", "evidence-explorer-demo", "refcore-billing",
    "security-showcase-demo", "sidekick-devops", "unsubscribe-service", "world-canvas-demo",
}


def test_no_new_deploy_unit_may_inherit_proxy_header_trust():
    unstated = _uvicorn_units_leaving_header_trust_implicit()
    assert unstated <= HEADER_TRUST_UNSTATED, (
        f"new uvicorn unit(s) leave proxy-header trust implicit: {sorted(unstated - HEADER_TRUST_UNSTATED)}. "
        "State --no-proxy-headers, or --proxy-headers with --forwarded-allow-ips naming the front (RT-03).")


def test_the_ratchet_only_shrinks_and_the_fronted_demo_stays_decided():
    unstated = _uvicorn_units_leaving_header_trust_implicit()
    assert "permissions-showcase-demo" not in unstated, "RT-03 regressed: the fronted demo trusts headers again"
    assert unstated <= HEADER_TRUST_UNSTATED


def test_a_unit_that_closes_its_debt_is_removed_from_the_list_not_left_stale():
    unstated = _uvicorn_units_leaving_header_trust_implicit()
    stale = HEADER_TRUST_UNSTATED - unstated
    assert not stale, f"{sorted(stale)} now decide(s) header trust at spawn: drop them from the debt list"


# ── the fronted unit reports its own build ────────────────────────────────────────────────────────────

_ROOT = pathlib.Path(__file__).resolve().parents[1]

_LOAD_DEMO = textwrap.dedent("""
    import importlib.util, json, os, tempfile
    os.environ.setdefault("PERMISSIONS_DIR", tempfile.mkdtemp(prefix="perm-demo-"))
    spec = importlib.util.spec_from_file_location("perm_demo_app", "deploy/permissions-showcase-demo/app.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    from fastapi.testclient import TestClient
    c = TestClient(m.app)
    print(json.dumps({"status": c.get("/api/version").status_code,
                      "body": c.get("/api/version").json(),
                      "health": c.get("/healthz").json()}))
""")


def _load_demo():
    """Import the deploy unit the way its container does, in a fresh process, with this repo on the path.

    A subprocess rather than an in-process import: the unit reads its environment at import, and the whole
    point is what a *served* process says about itself. Stripped first, then set — an `AGENTIC_OS_*` left over
    from the outer environment would otherwise be the thing under test.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PERMISSIONS_", "AGENTIC_OS_"))}
    env.update(PYTHONPATH=str(_ROOT), AGENTIC_OS_BUILD_REVISION="deadbee",
               AGENTIC_OS_BUILD_TIME="2026-11-11T00:00:00+00:00")
    p = subprocess.run([sys.executable, "-c", _LOAD_DEMO], env=env, cwd=str(_ROOT),
                       capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr[-900:]
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_the_fronted_demo_answers_the_question_the_probe_could_not_ask():
    out = _load_demo()
    assert out["status"] == 200, "GET /api/version on the deployed unit"
    assert out["body"]["revision"] == "deadbee" and out["body"]["revision_source"] == ops.ENV_BUILD_REVISION
    # the unit's own liveness answer survives the install: /healthz is still {"ok": true}, not {"status": "ok"}
    assert out["health"] == {"ok": True}, "install_build_identity replaced the unit's existing /healthz"


def test_the_deploy_path_stamps_verifies_and_never_claims_a_clean_commit_it_did_not_build():
    """Three files have to agree for a revision to mean anything: the Dockerfile must accept the stamp, the
    compose file must pass it, and deploy.sh must compute it from the tree it actually rsyncs — dirty included."""
    unit = _ROOT / "deploy" / "permissions-showcase-demo"
    df = (unit / "Dockerfile").read_text()
    assert "ARG AGENTIC_OS_BUILD_REVISION" in df and 'ENV AGENTIC_OS_BUILD_REVISION' in df
    compose = (unit / "permissions-demo.compose.yml").read_text()
    assert "AGENTIC_OS_BUILD_REVISION:" in compose, "compose must forward the build arg or the stamp is lost"
    sh = (unit / "deploy.sh").read_text()
    assert "rev-parse --short HEAD" in sh and "status --porcelain" in sh, \
        "deploy.sh must name a dirty tree as dirty rather than stamping the commit it does not match"
    assert "api/version" in sh, "deploy.sh must ask the running container what it is, not assume the build took"



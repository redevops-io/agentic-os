"""The enforcement posture on real sockets, through a same-host reverse proxy.

`tests/test_route_authz_exposure_gate.py` decides the posture from scope values; this module proves the two
claims only a real TCP connection can prove, because a proxy changes nothing except who opened the socket:

1. An app fronted by a same-host proxy sees a loopback peer, so without a declaration the policy is advisory
   and the handler runs — the residual gap, asserted rather than described.
2. Declaring the front (uvicorn's FORWARDED_ALLOW_IPS, which any proxy-fronted deployment already sets to
   trust X-Forwarded-For) turns that same request into a refusal, client/proxy/sockets unchanged.

Nothing here binds a non-loopback address: dialling one from a developer host is firewall-blocked, so the
routable-peer cases stay in the scope-value tests where they can be asserted honestly.
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

import pytest

HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = HERE.parent

_APP_SRC = '''
from fastapi import FastAPI, Request
from agentic_os.app_kit import route_authz as ra
from agentic_os.app_kit.route_authz import RoutePolicy as P

app = FastAPI()


@app.get("/read")
async def read_route():
    return {"ok": True}


@app.post("/act")
async def act(request: Request):
    await request.body()
    return {"ran": True}


ra.install(app, "topo", {"POST /act": P("topo.act", "authority_required")})
'''


@dataclasses.dataclass
class Topology:
    """How to reach a running app: through the same-host proxy, straight at it, or reading a public path."""
    post: Callable[[], int]      # authority-required write via the proxy
    read: Callable[[str], int]   # anonymous public read via the proxy
    direct: Callable[[], int]    # authority-required write straight to the app, no proxy


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _pump(a, b):
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
@pytest.fixture()
def proxied_app():
    """Starts the app and a same-host forwarding proxy on real loopback sockets.

    Returns a factory taking the deployment's declaration (`front="forwarded"`, `"behind"` or `None`), because
    the posture variables are read by the *server process*: they have to be in its environment when it starts,
    so a test that set them after launch would be asserting nothing. Each call boots a fresh server.
    """
    base_env = {k: v for k, v in os.environ.items()
                if k not in ("AGENTIC_OS_PROFILE", "AGENTIC_OS_ROUTE_AUTHZ_ENFORCE", "FORWARDED_ALLOW_IPS",
                             "AGENTIC_OS_BEHIND_PROXY", "AGENTIC_OS_BIND_HOST")}
    src = HERE / "_route_authz_topo_app.py"
    src.write_text(_APP_SRC)
    live = []

    def serve(front=None):
        app_port, proxy_port = _free_port(), _free_port()
        env = {**base_env, "PYTHONPATH": f"{REPO_ROOT}{os.pathsep}{HERE}"}
        if front == "forwarded":
            env["FORWARDED_ALLOW_IPS"] = "127.0.0.1"
        elif front == "behind":
            env["AGENTIC_OS_BEHIND_PROXY"] = "1"
        elif front == "direct":
            env["AGENTIC_OS_BEHIND_PROXY"] = "0"
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "_route_authz_topo_app:app", "--host", "127.0.0.1",
             "--port", str(app_port), "--log-level", "warning"], cwd=str(HERE), env=env)
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", proxy_port))
        listener.listen(8)

        def accept_loop():
            while True:
                try:
                    client, _ = listener.accept()
                except OSError:
                    return
                up = socket.create_connection(("127.0.0.1", app_port), timeout=5)
                threading.Thread(target=_pump, args=(client, up), daemon=True).start()
                threading.Thread(target=_pump, args=(up, client), daemon=True).start()

        threading.Thread(target=accept_loop, daemon=True).start()
        live.append((server, listener))

        def ready() -> bool:
            for _ in range(160):
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{app_port}/read", timeout=1)
                    return True
                except urllib.error.HTTPError:
                    return True
                except Exception:
                    time.sleep(0.25)
            return False

        if not ready():
            pytest.skip("uvicorn did not come up on this host")

        def post() -> int:
            """The client dials the proxy, never the app; the app's peer is the proxy's loopback address."""
            req = urllib.request.Request(
                f"http://127.0.0.1:{proxy_port}/act", data=b'{"goal":"sync the account"}', method="POST",
                headers={"content-type": "application/json", "x-forwarded-for": "203.0.113.7",
                         "cf-connecting-ip": "203.0.113.7", "host": "demo.example"})
            try:
                return urllib.request.urlopen(req, timeout=10).status
            except urllib.error.HTTPError as e:
                return e.code

        def get(path: str) -> int:
            try:
                return urllib.request.urlopen(f"http://127.0.0.1:{proxy_port}{path}", timeout=10).status
            except urllib.error.HTTPError as e:
                return e.code

        def direct() -> int:
            """No proxy in the path: the client dials the app, so the peer really is a local client."""
            req = urllib.request.Request(
                f"http://127.0.0.1:{app_port}/act", data=b'{"goal":"sync the account"}', method="POST",
                headers={"content-type": "application/json"})
            try:
                return urllib.request.urlopen(req, timeout=10).status
            except urllib.error.HTTPError as e:
                return e.code

        return Topology(post=post, read=get, direct=direct)

    try:
        yield serve
    finally:
        for server, listener in live:
            listener.close()
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
        src.unlink(missing_ok=True)


def test_an_undeclared_same_host_proxy_is_enforced_not_trusted(proxied_app):
    """The bypass this item exists to close, checked on real sockets.

    A same-host forwarding proxy connects over 127.0.0.1, so the accepted socket cannot tell it from a
    developer's own request. Provenance is therefore *unknown*, and unknown provenance enforces: the
    authority-required write is refused and the handler never runs. Before the fix this request was allowed.
    """
    t = proxied_app()
    assert t.post() == 401, "an undeclared loopback peer must not be trusted as local"
    assert t.read("/read") == 200, "while the anonymous public read journey keeps working, enforced or not"


def test_direct_access_with_unknown_provenance_is_also_enforced(proxied_app):
    """The other half of the same rule, on the same running server: dial the app itself, no proxy in the
    path. A genuine local client and a same-host relay look identical here, so both are treated as unknown
    provenance and enforced — which is exactly why locality has to be declared, not assumed."""
    t = proxied_app()
    assert t.direct() == 401
    assert t.post() == 401


def test_declaring_the_front_refuses_the_same_request_end_to_end(proxied_app):
    """Same code, same client, same proxy topology; the operator's statement about the front changes nothing
    about the outcome now that unknown provenance already enforces — it only makes the reason explicit."""
    t = proxied_app(front="forwarded")
    assert t.post() == 401, "the authority-required write must be refused, not recorded"
    assert t.read("/read") == 200, "and the public read journey keeps working through the same declared front"


def test_the_explicit_behind_proxy_declaration_is_equivalent(proxied_app):
    """For a deployment that does not parse X-Forwarded-For at all, AGENTIC_OS_BEHIND_PROXY=1 says the same
    thing about the topology and must have the same effect."""
    assert proxied_app(front="behind").post() == 401


def test_the_only_advisory_path_is_an_explicit_direct_statement(proxied_app):
    """What the advisory posture now costs: a written claim that nothing forwards to this process.

    This is the deployment that says "only this machine can reach me". Shown on the same proxy fixture so the
    risk is visible: make that statement and a same-host proxy's forwarded write is allowed through again.
    The compliance check exists so nobody makes it by accident.
    """
    t = proxied_app(front="direct")
    assert t.direct() == 200, "a machine-local client in a declared-direct deployment still gets advisory"
    assert t.post() == 200, "…and so does anything relayed to that loopback socket: the statement is the risk"
    assert t.read("/read") == 200



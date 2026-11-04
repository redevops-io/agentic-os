"""The observed attack on the unauthenticated loopback sidecar API (audit finding 4), end to end.

Deliberately imports nothing from the new capability modules, so the same file runs against the frozen
feeca065 tree, where it FAILS (the cross-origin anonymous POST is answered 200 with file counts and
carries `Access-Control-Allow-Origin: *`), and against the fixed tree, where it passes.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi.testclient import TestClient

from agentic_os.projects_api import SampleProjectionProvider, create_app

ROOT = Path(__file__).resolve().parents[1]
EVIL = "https://evil.example"


def _env(port: int) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("APPROVAL_SECRET", "AGENT_GATEWAY_TOKEN", "SSH_AUTH_SOCK", "SSH_AGENT_PID")
           and not k.startswith(("PROJECTS_", "AGENTIC_OS_"))}
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PROJECTS_PORT"] = str(port)
    return env


def test_legacy_open_app_no_longer_offers_wildcard_cors():
    c = TestClient(create_app(SampleProjectionProvider()))
    r = c.get("/api/projects", headers={"Origin": EVIL})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
    r = c.options("/api/sources/confirm", headers={"Origin": EVIL, "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_real_serve_process_refuses_cross_origin_anonymous_confirm(tmp_path):
    folder = tmp_path / "walkme"
    folder.mkdir()
    (folder / "f1.txt").write_text("x")
    (folder / "f2.txt").write_text("y")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    proc = subprocess.Popen([sys.executable, "-c", "from agentic_os.projects_api import main; main()"],
                            env=_env(port), cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.time() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                assert proc.poll() is None and time.time() < deadline, "serve did not start"
                time.sleep(0.2)
        body = {"project_id": "customer-ops", "confirmed_by": "x",
                "sources": [{"kind": "files", "location": str(folder), "name": "x"}]}
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/sources/confirm", method="POST",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Origin": EVIL})
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            status, text, hdrs = resp.status, resp.read().decode(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            status, text, hdrs = e.code, e.read().decode(), dict(e.headers)
        assert status in (401, 403), (status, text[:300])
        assert not any(k.lower().startswith("access-control-") for k in hdrs)
        assert "f1.txt" not in text and '"files"' not in text and "file_count" not in text
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        proc.stdout.close()
        proc.stderr.close()

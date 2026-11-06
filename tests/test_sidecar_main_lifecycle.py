"""The sidecar's half of the launcher lifecycle contract (R0-prime defect b).

`serve --exit-on-stdin-eof` shuts uvicorn down cleanly when its stdin reaches EOF. The launcher keeps
the write end of that pipe open while it lives, so closing it (Stop / quit) or dying (crash, SIGKILL)
becomes a graceful sidecar shutdown, which lets the PyInstaller bootloader remove its `_MEI*` dir.
Without the flag stdin EOF must change nothing (running from a terminal, CI, systemd: stdin is often
/dev/null).
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / "packaging" / "sidecar_main.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _wait(pred, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


def _start(tmp_path: Path, port: int, *flags: str, stdin) -> subprocess.Popen:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
           "PYTHONPATH": str(REPO), "PROJECTS_PORT": str(port), "PROJECTS_HOST": "127.0.0.1"}
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return subprocess.Popen([sys.executable, str(ENTRY), "serve", *flags], env=env, cwd=tmp_path,
                            stdin=stdin, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_closing_stdin_stops_the_server_cleanly(tmp_path):
    port = _free_port()
    p = _start(tmp_path, port, "--exit-on-stdin-eof", stdin=subprocess.PIPE)
    try:
        assert _wait(lambda: _listening(port), 60), "server never listened"
        assert p.poll() is None
        p.stdin.close()  # what the launcher does on Stop / quit, or the OS does when it dies
        assert _wait(lambda: p.poll() is not None, 15), "server did not stop on stdin EOF"
        assert _wait(lambda: not _listening(port), 5), "port still open after the server exited"
    finally:
        if p.poll() is None:
            p.kill()
        p.wait(timeout=10)


def test_stdin_eof_is_ignored_without_the_flag(tmp_path):
    port = _free_port()
    p = _start(tmp_path, port, stdin=subprocess.DEVNULL)  # immediate EOF on stdin
    try:
        assert _wait(lambda: _listening(port), 60), "server never listened"
        time.sleep(1.5)
        assert p.poll() is None, "stdin EOF must not stop a server started without the flag"
        assert _listening(port)
    finally:
        p.kill()
        p.wait(timeout=10)

"""A bad PROJECTS_DRILLDOWN_* configuration is refused with ONE line, not a traceback (D-NEW-5).

0.6.0-rc1 refused correctly (exit 1, no listener, no launch code) but the frozen sidecar printed a Python traceback and a
PyInstaller "Failed to execute script" line, because the app is built when agentic_os.projects_api is imported. The entry points
(the sidecar's `serve` and the `agentic-os-projects` console script) now go through agentic_os.projects_entry, which prints the
fixed message and exits 1. Nothing is bound, written or printed on stdout before the refusal.
"""
from __future__ import annotations

import importlib.util
import io
import os
import socket
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "SENTINEL-VALUE-must-not-be-echoed"


def _env(tmp_path, **extra):
    env = {k: os.environ[k] for k in ("PATH", "TMPDIR", "LANG", "SYSTEMROOT") if k in os.environ}
    env.update({"HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1", "AGENTIC_OS_HANDOFF": "stdout"})
    env.update(extra)
    return env


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


BAD = {
    "partial": lambda t: {"PROJECTS_DRILLDOWN_EVENT_PATH": str(t / SENTINEL)},
    "backend-only": lambda t: {"PROJECTS_DRILLDOWN_EVENT_BACKEND": "jsonl"},
    "memory-backend": lambda t: {"PROJECTS_DRILLDOWN_EVENT_PATH": str(t / SENTINEL), "PROJECTS_DRILLDOWN_PROJECT": "acme",
                                 "PROJECTS_DRILLDOWN_GRANTS_DIR": str(t / "g"), "PROJECTS_DRILLDOWN_AUDIT_PATH": str(t / "a.jsonl"),
                                 "PROJECTS_DRILLDOWN_EVENT_BACKEND": "memory"},
    "bad-project": lambda t: {"PROJECTS_DRILLDOWN_EVENT_PATH": str(t / SENTINEL), "PROJECTS_DRILLDOWN_PROJECT": "Bad Project!",
                              "PROJECTS_DRILLDOWN_GRANTS_DIR": str(t / "g"), "PROJECTS_DRILLDOWN_AUDIT_PATH": str(t / "a.jsonl")},
}


def _assert_one_line_refusal(p, port=None):
    assert p.returncode == 1, (p.returncode, p.stderr)
    assert p.stdout == ""                                             # no launch code, no hand-off record, no secret
    lines = [ln for ln in p.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1, p.stderr
    assert lines[0].startswith("refusing to start: "), lines[0]
    assert "Traceback" not in p.stderr and "PYI" not in p.stderr and "Failed to execute" not in p.stderr
    assert SENTINEL not in p.stderr and "File \"" not in p.stderr
    if port is not None:
        assert not _listening(port)


@pytest.mark.parametrize("kind", sorted(BAD))
def test_entry_module_refuses_with_one_line_exit_1_and_no_listener(tmp_path, kind):
    port = _free_port()
    env = _env(tmp_path, PROJECTS_PORT=str(port), **BAD[kind](tmp_path))
    p = subprocess.run([sys.executable, "-c", "import sys; from agentic_os.projects_entry import main; sys.exit(main())"],
                       env=env, capture_output=True, text=True, timeout=60, cwd=tmp_path)
    _assert_one_line_refusal(p, port)


def test_the_message_names_what_to_set_not_what_was_set(tmp_path):
    env = _env(tmp_path, PROJECTS_PORT="0", **BAD["partial"](tmp_path))
    p = subprocess.run([sys.executable, "-c", "import sys; from agentic_os.projects_entry import main; sys.exit(main())"],
                       env=env, capture_output=True, text=True, timeout=60, cwd=tmp_path)
    assert "PROJECTS_DRILLDOWN_PROJECT" in p.stderr and "partly configured" in p.stderr


@pytest.mark.parametrize("kind", sorted(BAD))
def test_sidecar_serve_refuses_the_same_way(tmp_path, kind):
    """As the launcher runs it: `serve --exit-on-stdin-eof` with the write end of stdin held open."""
    port = _free_port()
    env = _env(tmp_path, PROJECTS_PORT=str(port), **BAD[kind](tmp_path))
    proc = subprocess.Popen([sys.executable, str(ROOT / "packaging" / "sidecar_main.py"), "serve", "--exit-on-stdin-eof"],
                            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=tmp_path)
    try:
        proc.wait(timeout=60)                        # stdin stays open (as the launcher keeps it); communicate() would close it
        out, err = proc.stdout.read(), proc.stderr.read()
    finally:
        proc.kill()
        proc.stdin.close()
    _assert_one_line_refusal(subprocess.CompletedProcess(proc.args, proc.returncode, out, err), port)


def test_the_console_script_points_at_the_entry_module():
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["agentic-os-projects"] == "agentic_os.projects_entry:main"


def test_sidecar_main_uses_the_entry_module():
    src = (ROOT / "packaging" / "sidecar_main.py").read_text()
    assert "agentic_os.projects_entry" in src and "from agentic_os.projects_api import main" not in src


def test_a_good_configuration_still_reaches_the_server(monkeypatch):
    import agentic_os.projects_api as api
    from agentic_os import projects_entry
    called = []
    monkeypatch.setattr(api, "main", lambda: called.append(1))
    assert projects_entry.main() == 0 and called == [1]


def test_only_the_drilldown_configuration_error_is_turned_into_a_message(monkeypatch, capsys):
    """Any other failure keeps its traceback: this does not hide real defects."""
    from agentic_os import projects_entry
    import agentic_os.projects_api as api

    def boom():
        raise RuntimeError("real defect")
    monkeypatch.setattr(api, "main", boom)
    with pytest.raises(RuntimeError):
        projects_entry.main()


def test_message_is_one_line_and_bounded(capsys):
    from agentic_os import projects_entry
    assert projects_entry.refusal_line(ValueError("a\nb" + "x" * 1000)).count("\n") == 0
    assert len(projects_entry.refusal_line(ValueError("x" * 1000))) <= 340

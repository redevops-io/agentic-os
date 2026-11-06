"""Parent side of the sandboxed query worker.

One short-lived child process per query: ``python -I _worker.py`` with an EMPTY environment, a private empty scratch
directory as its working directory (destroyed afterwards), its own process group (killed on timeout), bounded stdout, and
- where the host provides it - an OS sandbox that denies network and every file write outside the scratch directory and
restricts file reads to the interpreter and the worker's own code:

* macOS: ``sandbox-exec`` (Seatbelt). Verified on this host by tests.
* Linux: ``bwrap`` (bubblewrap) when installed, else ``unshare --net`` (network only). Implemented, NOT verified on a
  Linux host by this change's test run (reported as such).

``os_sandbox``: ``auto`` (use it when present), ``required`` (refuse to run without it), ``off``.
The OS sandbox is a second layer. The first layers are in the worker itself (no path to any artifact is ever sent, DuckDB
external access disabled, configuration locked, no extension loading, read-only SELECT)."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, List, Optional

from .limits import Limits

_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_worker.py")
_live = 0
_live_lock = threading.Lock()


class WorkerError(Exception):
    """``code`` in: statement_rejected | timeout | limit | error | sandbox_unavailable. ``public`` is safe to show."""

    def __init__(self, code: str, public: str = ""):
        self.code = code
        super().__init__(public or code)


@dataclass
class WorkerResult:
    columns: List[str]
    rows: List[list]
    truncated: bool


def live_workers() -> int:
    with _live_lock:
        return _live


# ───────────────────────── OS sandbox ─────────────────────────
_PROBED: dict = {}


def _detect_backend() -> Optional[str]:
    """The OS sandbox backend that is both present and actually works on this host (probed once)."""
    if "b" not in _PROBED:
        cand = _candidate_backend()
        if cand is not None:
            try:
                sc = tempfile.mkdtemp(prefix="aos-evq-probe-")
                try:
                    r = subprocess.run(_wrap([sys.executable, "-I", "-c", "pass"], sc, cand), env={}, cwd=sc, capture_output=True, timeout=20)
                    ok = r.returncode == 0
                finally:
                    shutil.rmtree(sc, ignore_errors=True)
            except Exception:                              # noqa: BLE001
                ok = False
            cand = cand if ok else None
        _PROBED["b"] = cand
    return _PROBED["b"]


def _candidate_backend() -> Optional[str]:
    if sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec"):
        return "sandbox-exec"
    if sys.platform.startswith("linux"):
        if shutil.which("bwrap"):
            return "bwrap"
        if shutil.which("unshare"):
            return "unshare"
    return None


def sandbox_status() -> dict:
    be = _detect_backend()
    return {"backend": be or "none",
            "network_blocked": be in ("sandbox-exec", "bwrap", "unshare"),
            "file_read_blocked": be in ("sandbox-exec", "bwrap"),
            "file_write_blocked": be in ("sandbox-exec", "bwrap")}


def _sbpl_quote(path: str) -> str:
    if '"' in path or "\\" in path or "\n" in path:
        raise WorkerError("sandbox_unavailable", "unsafe path for the sandbox profile")
    return '"' + path + '"'


def _read_roots() -> List[str]:
    import sysconfig
    roots = {sys.prefix, sys.base_prefix, os.path.dirname(os.path.dirname(os.path.realpath(sys.executable))),
             os.path.dirname(_WORKER), sysconfig.get_paths().get("purelib", ""), sysconfig.get_paths().get("platlib", "")}
    try:
        import duckdb
        roots.add(os.path.dirname(os.path.dirname(os.path.realpath(duckdb.__file__))))
    except Exception:                                      # noqa: BLE001
        pass
    out = []
    for r in roots:
        if r and os.path.isdir(r):
            out.append(os.path.realpath(r))
    return sorted(set(out))


def _wrap(argv: List[str], scratch: str, backend: Optional[str]) -> List[str]:
    if backend == "sandbox-exec":
        scratch_real = os.path.realpath(scratch)
        allow_read = ["/usr", "/System", "/Library", "/opt/homebrew", "/dev", "/private/var/db", "/private/var/folders"]
        parts = ["(version 1)", "(allow default)", "(deny network*)", "(deny file-write*)",
                 f"(allow file-write* (subpath {_sbpl_quote(scratch_real)}) (literal \"/dev/null\"))",
                 "(deny file-read-data (subpath \"/\"))",
                 "(allow file-read-data (literal \"/\") " + " ".join(f"(subpath {_sbpl_quote(p)})" for p in allow_read + _read_roots())
                 + f" (subpath {_sbpl_quote(scratch_real)}))"]
        return ["/usr/bin/sandbox-exec", "-p", "\n".join(parts)] + argv
    if backend == "bwrap":
        cmd = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
               "--proc", "/proc", "--dev", "/dev", "--bind", scratch, scratch, "--chdir", scratch]
        for p in ["/usr", "/lib", "/lib64", "/bin", "/sbin"] + _read_roots():
            if os.path.exists(p):
                cmd += ["--ro-bind", p, p]
        return cmd + argv
    if backend == "unshare":
        return ["unshare", "--net", "--map-root-user"] + argv
    return argv


# ───────────────────────── run ─────────────────────────
def _spawn_and_collect(request: dict, limits: Limits, os_sandbox: str) -> dict:
    global _live
    backend = None if os_sandbox == "off" else _detect_backend()
    if os_sandbox == "required" and backend is None:
        raise WorkerError("sandbox_unavailable", "the required OS sandbox is not available on this host")
    scratch = tempfile.mkdtemp(prefix="aos-evq-")
    os.chmod(scratch, 0o700)
    cap = limits.max_result_bytes + 4096
    try:
        argv = _wrap([sys.executable, "-I", _WORKER], scratch, backend)
        with _live_lock:
            _live += 1
        try:
            p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 cwd=scratch, env={}, start_new_session=True, close_fds=True)
        except OSError:
            with _live_lock:
                _live -= 1
            raise WorkerError("error", "the query worker could not start") from None
        chunks: List[bytes] = []
        over = {"flag": False}

        def reader():
            total = 0
            while True:
                b = p.stdout.read(65536)
                if not b:
                    break
                total += len(b)
                if total > cap:
                    over["flag"] = True
                    _kill(p)
                    break
                chunks.append(b)

        def feeder():
            try:
                p.stdin.write(json.dumps(request, default=str).encode())
                p.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        t_r = threading.Thread(target=reader, daemon=True)
        t_f = threading.Thread(target=feeder, daemon=True)
        t_r.start(); t_f.start()
        try:
            p.wait(timeout=limits.timeout_s)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill(p)
            p.wait()
        t_r.join(5); t_f.join(5)
        with _live_lock:
            _live -= 1
        if timed_out:
            raise WorkerError("timeout")
        if over["flag"]:
            raise WorkerError("limit")
        data = b"".join(chunks)
        if not data:
            raise WorkerError("limit" if p.returncode in (-signal.SIGKILL, -signal.SIGXCPU, -signal.SIGSEGV, 3) else "error")
        try:
            return json.loads(data)
        except ValueError:
            raise WorkerError("error") from None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _kill(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            p.kill()
        except Exception:                                  # noqa: BLE001
            pass


def execute(data, sql: str, limits: Limits = Limits(), *, os_sandbox: str = "auto",
            _unsafe_disable_guard: bool = False) -> WorkerResult:
    """Run ``sql`` over ``data`` (a :class:`~.catalog.ScopeData`) in a fresh sandboxed worker.
    ``_unsafe_disable_guard`` exists ONLY so tests can prove the sandbox holds without the statement guard."""
    req = data.request(sql, limits)
    req["guard"] = not _unsafe_disable_guard
    out = _spawn_and_collect(req, limits, os_sandbox)
    if not out.get("ok"):
        code = out.get("code", "error")
        raise WorkerError(code, out.get("message", ""))
    return WorkerResult(out["columns"], out["rows"], bool(out.get("truncated")))


def selftest(probe: str, *, os_sandbox: str = "auto", **kw: Any) -> dict:
    """Fixed introspection probes run inside a real worker (never arbitrary code)."""
    req = {"mode": "selftest", "probe": probe, "limits": {"timeout_s": 15}, **kw}
    out = _spawn_and_collect(req, Limits(timeout_s=15), os_sandbox)
    if out.get("ok") is False:
        raise WorkerError(out.get("code", "error"))
    return out

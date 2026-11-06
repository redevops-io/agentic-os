"""The query worker. Runs as ``python -I _worker.py`` in its own process, with an empty environment, a private empty
scratch directory as cwd and (where the host offers it) an OS sandbox around it.

It receives ONE JSON request on stdin (already-loaded scoped rows, optional views, the statement, limits) and writes ONE
JSON response to stdout. It is given no path to any artifact, no credential and no network. DuckDB is opened in memory
with ``enable_external_access=false`` and the configuration is then LOCKED; the worker validates the statement again
with the same parser-based guard (defence in depth - the sandbox is the boundary, and ``guard=false`` exists only so
the test-suite can prove the sandbox holds without it). Standard library + duckdb only; imports no agentic_os code."""
from __future__ import annotations

import json
import os
import re
import resource
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOCKET_MODULES = ("socket", "_socket", "ssl", "_ssl", "http", "urllib", "ftplib", "smtplib", "telnetlib")


def _block_network(keep_raw: bool = False) -> None:
    """Python-level block (defence in depth; the OS sandbox is the real network barrier where available)."""
    import socket as _s

    def _deny(*a, **k):
        raise OSError("network disabled in the evidence worker")
    _s.socket = _deny                                    # type: ignore[assignment]
    _s.create_connection = _deny                         # type: ignore[assignment]
    _s.getaddrinfo = _deny                               # type: ignore[assignment]
    if not keep_raw:
        for name in ("_socket", "ssl", "_ssl"):
            sys.modules[name] = None                     # type: ignore[assignment]


def _apply_rlimits(req: dict) -> None:
    lim = req.get("limits", {})
    cpu = int(lim.get("timeout_s", 10)) + 2
    for name, val in (("RLIMIT_CORE", 0), ("RLIMIT_FSIZE", 0), ("RLIMIT_CPU", cpu), ("RLIMIT_NOFILE", 256)):
        try:
            soft_hard = getattr(resource, name)
            resource.setrlimit(soft_hard, (val, val))
        except (ValueError, OSError):
            pass
    # RLIMIT_AS is deliberately NOT set: DuckDB/glibc reserve large virtual ranges, so an address-space cap makes ordinary queries
    # fail intermittently (seen on Linux). Memory is bounded by DuckDB's memory_limit, the RSS watchdog below and the container cgroup.


def _start_watchdog(memory_mb: int) -> None:
    """RSS watchdog: DuckDB's memory_limit does not cover every allocation, and RLIMIT_AS is not enforced on macOS."""
    cap = (memory_mb * 2 + 200) * 1024 * 1024
    div = 1 if sys.platform == "darwin" else 1024

    def run():
        while True:
            time.sleep(0.05)
            if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * div > cap:
                os.write(1, b'{"ok": false, "code": "limit"}')
                os._exit(3)
    threading.Thread(target=run, daemon=True).start()


_PATH = re.compile(r"(?<![\w.])(?:[A-Za-z]:)?(?:/[^\s\"'`),;:]+)+")


def _clean(msg: str) -> str:
    first = str(msg).strip().splitlines()[0] if str(msg).strip() else "query failed"
    first = _PATH.sub("<path>", first)
    return first[:300]


def _classify(e: Exception) -> str:
    name = type(e).__name__
    text = str(e)
    if ("Out of Memory" in text or name == "OutOfMemoryException" or "memory_limit" in text
            or "File too large" in text or "Could not write file" in text):
        return "limit"
    return "error"


def _create(con, ds: dict) -> None:
    cols = ds["columns"]
    ddl = ", ".join(f'"{c}" {t}' for c, t in cols)
    con.execute(f'CREATE TABLE "{ds["name"]}" ({ddl})')
    rows = ds["rows"]
    if rows:
        names = [c for c, _ in cols]
        con.executemany(f'INSERT INTO "{ds["name"]}" VALUES ({", ".join("?" for _ in names)})',
                        [[r.get(n) for n in names] for r in rows])


def _settings(con) -> dict:
    names = ("enable_external_access", "lock_configuration", "autoinstall_known_extensions", "autoload_known_extensions",
             "allow_community_extensions", "allow_unsigned_extensions", "threads", "memory_limit")
    out = {}
    for n in names:
        v = con.execute("SELECT value FROM duckdb_settings() WHERE name = ?", [n]).fetchone()[0]
        out[n] = v
    return {"enable_external_access": str(out["enable_external_access"]).lower() == "true",
            "lock_configuration": str(out["lock_configuration"]).lower() == "true",
            "autoinstall_known_extensions": str(out["autoinstall_known_extensions"]).lower() == "true",
            "autoload_known_extensions": str(out["autoload_known_extensions"]).lower() == "true",
            "allow_community_extensions": str(out["allow_community_extensions"]).lower() == "true",
            "allow_unsigned_extensions": str(out["allow_unsigned_extensions"]).lower() == "true",
            "threads": int(out["threads"]), "memory_limit": out["memory_limit"]}


def _connect(req: dict):
    import duckdb
    lim = req.get("limits", {})
    scratch = os.getcwd()
    con = duckdb.connect(":memory:", config={
        "enable_external_access": False,
        "autoinstall_known_extensions": False, "autoload_known_extensions": False,
        "allow_community_extensions": False, "allow_unsigned_extensions": False,
        "threads": int(lim.get("threads", 1)), "memory_limit": f"{int(lim.get('memory_mb', 256))}MB",
        "max_temp_directory_size": "0B", "temp_directory": os.path.join(scratch, "tmp"),
        "home_directory": "", "secret_directory": "",
    })
    return con


def _selftest(req: dict) -> dict:
    probe = req["probe"]
    if probe == "env":
        return {"env": dict(os.environ)}
    if probe == "rlimits":
        return {"fsize": resource.getrlimit(resource.RLIMIT_FSIZE)[0], "core": resource.getrlimit(resource.RLIMIT_CORE)[0],
                "nofile": resource.getrlimit(resource.RLIMIT_NOFILE)[0], "cpu": resource.getrlimit(resource.RLIMIT_CPU)[0]}
    if probe == "alloc":                                  # exercises the RSS watchdog (the watchdog kills this process)
        _start_watchdog(int(req.get("memory_mb", 64)))
        hold = []
        for _ in range(int(req["mb"])):
            hold.append(bytearray(b"x" * (1024 * 1024)))
            time.sleep(0.002)
        time.sleep(0.3)
        return {"allocated": len(hold)}
    if probe == "pid":
        return {"pid": os.getpid()}
    if probe == "cwd":
        return {"cwd": os.getcwd(), "listing": sorted(os.listdir(".")),
                "is_private": (os.stat(".").st_mode & 0o077) == 0}
    if probe == "settings":
        import duckdb
        con = _connect({"limits": {}})
        con.execute("SET lock_configuration = true")
        s = _settings(con)
        s["duckdb_version"] = duckdb.__version__
        return s
    if probe == "socket":
        try:
            import socket
            socket.socket()
            return {"socket_created": True}
        except Exception:                                # noqa: BLE001
            return {"socket_created": False}
    if probe == "os_network":
        import _socket                                    # imported before the python-level block
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(1.0)
        try:
            s.connect(("127.0.0.1", int(req["port"])))
            return {"connected": True}
        except Exception:                                # noqa: BLE001
            return {"connected": False}
    if probe == "os_files":
        out = {}
        try:
            with open(req["read"], "rb") as fh:
                out["read_ok"] = bool(fh.read(1))
        except Exception:                                # noqa: BLE001
            out["read_ok"] = False
        try:
            with open(req["write"], "wb") as fh:
                fh.write(b"x")
                fh.flush()
            out["write_ok"] = True
        except Exception:                                # noqa: BLE001
            out["write_ok"] = False
        return out
    return {"error": "unknown probe"}


def main() -> int:
    raw = sys.stdin.buffer.read()
    req = json.loads(raw)
    mode = req.get("mode", "query")
    if not (mode == "selftest" and req.get("probe") in ("os_network", "os_files")):   # those probes isolate the OS sandbox layer
        _apply_rlimits(req)
    if mode == "selftest":
        if req.get("probe") == "os_network":
            _block_network(keep_raw=True)
            import _socket as _keep                      # noqa: F401  (kept before the block removed it)
        else:
            _block_network()
        out = _selftest(req)
        sys.stdout.write(json.dumps(out))
        return 0
    _block_network()
    lim = req.get("limits", {})
    _start_watchdog(int(lim.get("memory_mb", 256)))
    sys.path.insert(0, _HERE)
    try:
        con = _connect(req)
        for ds in req["datasets"]:
            _create(con, ds)
        for v in req.get("views", []):
            if req.get("guard", True):
                import guard as _g
                _g.check_statement(v["sql"], {d["name"] for d in req["datasets"]} | {x["name"] for x in req.get("views", [])})
            con.execute(f'CREATE VIEW "{v["name"]}" AS {v["sql"]}')
        con.execute("SET lock_configuration = true")      # after data + views exist; nothing can relax anything now
        if req.get("guard", True):
            import guard as _g
            names = {d["name"] for d in req["datasets"]} | {v["name"] for v in req.get("views", [])}
            _g.check_statement(req["sql"], names, max_chars=int(lim.get("max_sql_chars", 8000)))
        cur = con.execute(req["sql"])
        cols = [d[0] for d in (cur.description or [])]
        max_rows = int(lim.get("max_rows", 500))
        rows = cur.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        rows = [list(r) for r in rows[:max_rows]]
        out = json.dumps({"ok": True, "columns": cols, "rows": rows, "truncated": truncated}, default=str)
        if len(out.encode()) > int(lim.get("max_result_bytes", 2_000_000)):
            sys.stdout.write(json.dumps({"ok": False, "code": "limit"}))
            return 0
        sys.stdout.write(out)
        return 0
    except BaseException as e:                            # noqa: BLE001
        name = type(e).__name__
        if name == "StatementRejected":
            sys.stdout.write(json.dumps({"ok": False, "code": "statement_rejected"}))
        else:
            sys.stdout.write(json.dumps({"ok": False, "code": _classify(e) if isinstance(e, Exception) else "error",
                                         "message": _clean(str(e))}))
        return 0


if __name__ == "__main__":
    sys.exit(main())

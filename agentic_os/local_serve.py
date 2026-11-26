"""``serve``: run the Projects UI + API behind the local capability gate (see ``local_capability``).

Two ways the capability reaches the browser, chosen by environment:

* Launcher mode, ``AGENTIC_OS_HANDOFF=stdout`` or ``AGENTIC_OS_HANDOFF=fd:<n>``: the sidecar writes ONE
  JSON line (the launch secret, the real port, a first launch code) to that channel and prints no URL.
  The launcher must keep that line out of every log. Loopback binding is mandatory in this mode.
* Standalone (default): the sidecar prints a one-time launch URL on stderr (ASCII only). The secret is
  not emitted anywhere (there is no launcher to hold it).

Other environment: ``PROJECTS_HOST`` (default 127.0.0.1), ``PROJECTS_PORT`` (default 8787, ``0`` picks a
free port), ``PROJECTS_BASE_PATH``, ``PROJECTS_ALLOWED_HOSTS`` (extra exact ``Host`` values, comma
separated, for a deliberate non-loopback deployment), ``AGENTIC_OS_LOCAL_CAPABILITY=off`` (explicit
opt-out for a server-hosted deployment that has its own edge controls; refused in launcher mode).
"""
from __future__ import annotations

import json
import os
import socket
import sys
from typing import Any, Dict, Optional, Tuple

ENV_HANDOFF = "AGENTIC_OS_HANDOFF"
ENV_OFF = "AGENTIC_OS_LOCAL_CAPABILITY"
ENV_ALLOWED_HOSTS = "PROJECTS_ALLOWED_HOSTS"
ENV_HANDOFF_CODE = "AGENTIC_OS_HANDOFF_LAUNCH_CODE"   # "1": also put a first launch code in the record
STANDALONE_CODE_TTL = 300.0       # a person copies the URL by hand; the launcher path uses 60 s
LAUNCHER_CODE_TTL = 60.0
GRACEFUL_SHUTDOWN_S = 2      # in-flight requests are cancelled after this long on shutdown
KEEP_ALIVE_S = 5
WANTED_NOFILE = 4096
_LOOPBACK = {"127.0.0.1": "127.0.0.1", "localhost": "127.0.0.1", "::1": "::1"}


def _bind(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):       # Windows: SO_REUSEADDR would allow port hijacking
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    sock.setblocking(False)
    return sock


def _raise_nofile() -> None:
    """Best effort: a GUI-launched process often has a soft limit of 256 descriptors."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = WANTED_NOFILE if hard == resource.RLIM_INFINITY else min(hard, WANTED_NOFILE)
        if soft != resource.RLIM_INFINITY and soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except (ImportError, ValueError, OSError):
        pass                                     # Windows has no resource module; keep the defaults


def _handoff_writer(spec: str):
    """Return (write(line), close()) for ``stdout`` or ``fd:<n>``; raises ValueError otherwise.
    The line is written as BYTES ending in a single LF: a text-mode stream on Windows would
    translate it to CR LF."""
    if spec == "stdout":
        def write(line: str) -> None:
            raw = line.encode("ascii") + b"\n"
            out = getattr(sys.stdout, "buffer", None)
            if out is not None:
                sys.stdout.flush()
                out.write(raw)
                out.flush()
            else:                                   # no binary layer (unusual): newline translation is all we have
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
        return write, (lambda: None)
    if spec.startswith("fd:") and spec[3:].isdigit():
        stream = os.fdopen(int(spec[3:]), "wb", closefd=True)

        def write_fd(line: str) -> None:
            stream.write(line.encode("ascii") + b"\n")
            stream.flush()
        return write_fd, stream.close
    raise ValueError(f"{ENV_HANDOFF} must be 'stdout' or 'fd:<n>', got {spec!r}")


def _url_host(host: str) -> str:
    if host in ("0.0.0.0", "", "localhost"):
        return "127.0.0.1"
    if host == "::":
        return "[::1]"
    return f"[{host}]" if ":" in host else host


def _err(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def serve(env: Optional[Dict[str, str]] = None) -> None:
    import uvicorn
    from .local_capability import LocalCapability
    from .projects_api import _ui_dir, mounted_app

    env = dict(os.environ if env is None else env)
    host = env.get("PROJECTS_HOST", "127.0.0.1").strip() or "127.0.0.1"
    # This is the direct, unproxied local-capability server (route_authz.enforces(), agentic_os/app_kit/
    # route_authz.py): nothing in this module puts a reverse proxy in front of it, so its real bind host and
    # "nothing forwards to me" are both true statements to make here, once, rather than leaving every request
    # to a loopback bind enforce for lack of a declaration. setdefault: a caller that already declared
    # something (e.g. because it IS fronted by a proxy for some reason) keeps that choice.
    os.environ.setdefault("AGENTIC_OS_BIND_HOST", host)
    os.environ.setdefault("AGENTIC_OS_BEHIND_PROXY", "0")
    port = int(env.get("PROJECTS_PORT", "8787"))
    base = env.get("PROJECTS_BASE_PATH", "").rstrip("/")
    handoff_spec = env.get(ENV_HANDOFF, "").strip().lower()
    off = env.get(ENV_OFF, "").strip().lower() == "off"
    served = "UI + API" if _ui_dir() is not None else "API only (no UI bundle found)"

    write = close = None
    if handoff_spec:
        if off:
            _err(f"refusing to start: {ENV_OFF}=off cannot be combined with {ENV_HANDOFF}")
            raise SystemExit(2)
        try:
            write, close = _handoff_writer(handoff_spec)
        except (ValueError, OSError) as e:
            _err(f"refusing to start: {e}")
            raise SystemExit(2)
        if host not in _LOOPBACK:
            _err("refusing to start: launcher mode binds a loopback address only "
                 f"(got PROJECTS_HOST={host!r})")
            raise SystemExit(2)
        host = _LOOPBACK[host]

    try:
        sock = _bind(host, port)
    except OSError as e:
        if write is not None:       # tell the launcher WHY (e.g. a stale sidecar owns the port); no secret
            write(json.dumps({"v": 1, "event": "error", "code": "bind_failed", "detail": str(e)[:200]},
                             separators=(",", ":")))
            close()
        _err(f"cannot bind {host}:{port}: {e}")
        raise SystemExit(1)
    real_port = sock.getsockname()[1]

    if off:
        # the hosted path of origin/main's `agentic-os-projects`: generate missing platform secrets once (non-fatal, docs/SECRETS.md)
        from agentic_os.secrets.bootstrap import ensure_secrets
        ensure_secrets()
        _err("WARNING: AGENTIC_OS_LOCAL_CAPABILITY=off: the API is served WITHOUT the local capability "
             "gate. Only do this behind your own authentication; never on a desktop.")
        app = mounted_app(base)
    else:
        extra = [h for h in env.get(ENV_ALLOWED_HOSTS, "").split(",") if h.strip()]
        cap = LocalCapability(port=real_port, extra_hosts=extra,
                              launch_code_ttl=LAUNCHER_CODE_TTL if write else STANDALONE_CODE_TTL)
        app = mounted_app(base, capability=cap)
        if write is not None:
            with_code = env.get(ENV_HANDOFF_CODE, "").strip() == "1"
            write(json.dumps(cap.launcher_handoff(host, real_port, base, include_launch_code=with_code),
                             separators=(",", ":")))
            close()
        else:
            code = cap.issue_launch_code()
            _err(f"ReDevOps Projects - {served} on http://{_url_host(host)}:{real_port}{base or ''}")
            _err("Open this one-time URL in your browser (single use, expires in "
                 f"{int(STANDALONE_CODE_TTL)} s; the part after # never leaves your browser):")
            _err(f"  http://{_url_host(host)}:{real_port}{base}/#lc={code}")
            if host not in _LOOPBACK:
                _err(f"WARNING: bound to {host}: reachable from other machines over plain HTTP; "
                     f"set {ENV_ALLOWED_HOSTS} to the host names you serve.")

    if off and not handoff_spec:
        print(f"ReDevOps Projects - {served} on http://{_url_host(host)}:{real_port}{base or ''}", file=sys.stderr)
    _raise_nofile()
    # Local availability limits: a client that stalls mid-request must not keep the process from exiting
    # (the launcher gives it a short grace period and then force-kills the group), and idle or stalled
    # connections must not exhaust file descriptors.
    served_app = _OnShutdown(app, lambda: _unpublish(env)) if (env.get("AGENTIC_OS_LOCAL_AGENTS_DIR") or "").strip() else app
    config = uvicorn.Config(served_app, log_level="info", proxy_headers=False,
                            access_log=(write is None or handoff_spec != "stdout"),
                            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S, timeout_keep_alive=KEEP_ALIVE_S)
    # Published for connected agents' bridges (agentic_os/local_agents.py): where this instance listens. Loopback only, removed on exit.
    agents_dir = (env.get("AGENTIC_OS_LOCAL_AGENTS_DIR") or "").strip()
    publish = bool(agents_dir) and host in _LOOPBACK and not off
    if publish:
        from .local_agents import publish_gateway, unpublish_gateway
        try:
            publish_gateway(agents_dir, port=real_port, version=_app_version())
            try:
                from .local_agents import installed_bridge
                installed_bridge(agents_dir)          # an upgraded app refreshes the bridge that connected agents already point at
            except Exception:  # noqa: BLE001 — the bridge is refreshed again the next time an agent is connected
                pass
        except OSError as e:
            _err(f"the local agent gateway could not be published ({e}); connected agents will not find this instance")
            publish = False
    try:
        uvicorn.Server(config).run(sockets=[sock])
    finally:
        if publish:
            unpublish_gateway(agents_dir)


def _app_version() -> str:
    try:
        from importlib.metadata import version
        return version("agentic-os")
    except Exception:  # noqa: BLE001
        return ""


def _unpublish(env) -> None:
    d = (env.get("AGENTIC_OS_LOCAL_AGENTS_DIR") or "").strip()
    if d:
        from .local_agents import unpublish_gateway
        unpublish_gateway(d)


class _OnShutdown:
    """Run one callback when the server shuts down gracefully (the ASGI lifespan shutdown message), then pass everything on. A stop by
    signal ends the process as soon as uvicorn's run() returns, so cleanup that must happen belongs here, not after run()."""

    def __init__(self, app, callback) -> None:
        self.app, self.callback = app, callback

    async def __call__(self, scope, receive, send):
        if scope["type"] != "lifespan":
            await self.app(scope, receive, send)
            return

        async def watched():
            message = await receive()
            if message["type"] == "lifespan.shutdown":
                try:
                    self.callback()
                except Exception:  # noqa: BLE001  (cleanup must not block the shutdown)
                    pass
            return message
        await self.app(scope, watched, send)

"""`edge-sentinel` command line (P1 quickstart).

Deliberately importable with the standard library alone: FastAPI, uvicorn and the kernel are imported
lazily inside `serve`, so `edge-sentinel --help` works in a bare venv on any machine — including from
inside this source directory, which is exactly where the old flat quickstart died (a directory holding
`operator.py` on `sys.path[0]` shadows the standard library and breaks `import uvicorn` before the app
ever runs; see tests/test_p1_quickstart.py and the kernel's membrane fix in CHANGELOG.md).

`serve` is the path that needs the agentic-os kernel: `edge_sentinel.app` imports
`agentic_os.app_gate` at module level, so a standalone `pip install edge-sentinel` gets you the CLI,
the tests that do not need the kernel, and a clear refusal with a pointer for `serve`. Replacing that
hard import with the seam (audit item P2) is deliberately NOT done here — it is gated on counsel
(D1/D2 of DECISIONS_D1-D5_2026-11-07.md), and inventing a bypass would cross a licensing boundary.
"""
from __future__ import annotations

import argparse
import sys


def main(argv: "list[str] | None" = None) -> int:
    from . import __version__

    parser = argparse.ArgumentParser(
        prog="edge-sentinel",
        description="SOC dashboard on a real CrowdSec core — human in the loop before any block.")
    parser.add_argument("--version", action="version", version=f"edge-sentinel {__version__}")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="run the dashboard (requires the agentic-os kernel)")
    serve.add_argument("--host", default=None,
                       help="bind interface; default SENTINEL_BIND_HOST or 127.0.0.1. A reachable bind "
                            "is refused unless SENTINEL_ALLOW_EXPOSURE=1 AND an operator credential "
                            "(or the named unauthenticated-demo opt-out) are set — see security.py.")
    serve.add_argument("--port", type=int, default=None,
                       help="default PORT environment or 8203")
    args = parser.parse_args(argv)

    if args.command != "serve":
        parser.print_help()
        return 0

    try:
        from . import app as _app_mod          # kernel imports land here, lazily
    except ImportError as missing:              # noqa: PERF203 - one clear refusal beats a traceback
        sys.stderr.write(
            f"edge-sentinel: `serve` needs the agentic-os kernel ({missing}).\n"
            "Install agentic-os (the kernel repo) alongside this package, or run the dashboard from an\n"
            "agentic-os checkout. The app-gate import becomes optional only with the kernel seam\n"
            "(audit item P2), which is gated on the counsel answer — this refusal is documented, not a bug.\n")
        return 2

    import uvicorn

    from . import security

    host = args.host or security.resolve_bind_host()
    port = args.port or _app_mod.PORT
    try:
        security.auth_required_guard()
        security.exposure_guard(host)           # P9: credentials-or-opt-out, by name — this CLI first
        from agentic_os.app_gate import require_explicit_setting_when_exposed as _exposed
        _exposed(host)                          # ... and the kernel's own permissions gate
    except security.ExposureRefused as refused:
        sys.exit(f"edge-sentinel: startup refused: {refused}")

    uvicorn.run(_app_mod.app, host=host, port=port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
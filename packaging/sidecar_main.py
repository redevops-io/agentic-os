"""Frozen entry for the ReDevOps native sidecar (PyInstaller).

One self-contained binary the Tauri launcher spawns — no system Python, no Git, no Docker.
Subcommands:
    serve      (default) → the Projects control plane (UI + API) on 127.0.0.1:8787
    bootstrap            → device-readiness / one-click install brain (posture + LLM + install)

The native-primary path: `serve` runs the whole control plane (missions, connectors/OAuth,
proactive intelligence, permissions) directly from the frozen binary. Docker is never required
here — it's an advanced capability the launcher enables separately.
"""
import runpy
import sys


def _utf8_stdio() -> None:
    """Make stdout/stderr UTF-8 whatever the locale or console code page is.

    The frozen interpreter ignores PYTHONIOENCODING / PYTHONUTF8 (PyInstaller starts CPython in
    isolated mode), so with stdout redirected to a pipe or file (which is how the launcher runs it)
    Windows picks the ANSI code page (cp1252) and POSIX picks the locale (latin-1, ASCII, cp1251 ...).
    The bootstrap report prints U+2014 and U+2192, which those codecs cannot encode, so the command
    died with UnicodeEncodeError instead of returning its verdict. The launcher decodes the output
    as UTF-8, so say so explicitly. `errors="replace"` keeps a stray unencodable character from ever
    turning a verdict into a crash. Reconfiguring in place also covers every later reference to
    sys.stdout / sys.stderr (uvicorn's log handlers, print, tracebacks).
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass  # no stream (windowed build) or not a TextIOWrapper


def main() -> None:
    _utf8_stdio()
    argv = sys.argv[1:]
    cmd = argv[0] if argv else "serve"
    if cmd == "bootstrap":
        sys.argv = ["agentic_os.mission.bootstrap", *argv[1:]]
        runpy.run_module("agentic_os.mission.bootstrap", run_name="__main__")
        return
    # default: serve the control plane
    from agentic_os.projects_api import main as serve
    serve()


if __name__ == "__main__":
    main()

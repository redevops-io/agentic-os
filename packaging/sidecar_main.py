"""Frozen entry for the ReDevOps native sidecar (PyInstaller).

One self-contained binary the Tauri launcher spawns — no system Python, no Git, no Docker.
Commands (fail closed: anything else prints the usage on stderr and exits 2 WITHOUT starting anything):
    serve [--exit-on-stdin-eof]
               the Projects control plane (UI + API) on 127.0.0.1:8787
               `--exit-on-stdin-eof`: shut down cleanly when stdin reaches EOF (the launcher
               keeps the write end of that pipe open for as long as it lives)
    bootstrap  device-readiness / one-click install brain (posture + LLM + install)
    help, -h, --help
               print the usage on stdout and exit 0
With no argument, or with an unknown command or option, nothing is started: no server, no port, no launch code, no file is
touched (v0.3.2 and RC2 treated `--help` as `serve`, started a server and printed a one-time launch URL).

The native-primary path: `serve` runs the whole control plane (missions, connectors/OAuth,
proactive intelligence, permissions) directly from the frozen binary. Docker is never required
here — it's an advanced capability the launcher enables separately.
"""
import os
import runpy
import signal
import sys
import threading


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


def _watch_stdin() -> None:
    """Lifetime tether (launcher -> sidecar): block on stdin and, at EOF, ask uvicorn to shut down.

    The launcher keeps the write end of our stdin pipe open for as long as it lives, so EOF means the
    launcher closed it (Stop / quit) OR died (crash, SIGKILL). Shutting down through uvicorn's own
    signal path is a graceful stop, which lets the PyInstaller bootloader delete its `_MEI*` temp dir;
    a hard kill of the bootloader would leave the Python child serving and the temp dir behind.
    Bytes read before EOF are discarded. (SECURITY-HANDSHAKE SEAM: a per-launch secret could arrive
    as the first bytes of this stream.)
    """
    try:
        while os.read(0, 4096):
            pass
    except OSError:
        pass  # a broken pipe is EOF as well
    # uvicorn installed handlers for these in the main thread; raise_signal trips them from here.
    signal.raise_signal(signal.SIGBREAK if sys.platform == "win32" else signal.SIGTERM)


USAGE = """usage: redevops-sidecar <command> [options]

commands:
  serve [--exit-on-stdin-eof]   run the Projects control plane (UI + API) on 127.0.0.1
                                (--exit-on-stdin-eof: stop cleanly when standard input reaches end of file)
  bootstrap                     print the device-readiness report (exit 0 = verified)
  help, -h, --help              print this text and exit 0

Any other command or option prints this text on standard error and exits 2; nothing is started.
"""
HELP_FLAGS = ("-h", "--help")
SERVE_FLAGS = ("--exit-on-stdin-eof",)


def parse_args(argv):
    """Pure parser. Returns (action, flags): action is one of "serve", "bootstrap", "help" (usage on stdout, exit 0) or
    "error" (usage on stderr, exit 2). Nothing here touches the filesystem, a socket or any stream."""
    if not argv:
        return "error", ()
    cmd, rest = argv[0], list(argv[1:])
    if cmd in HELP_FLAGS or cmd == "help":
        return "help", ()
    if cmd == "serve":
        allowed = SERVE_FLAGS
    elif cmd == "bootstrap":
        allowed = ()
    else:
        return "error", ()
    if any(a not in allowed + HELP_FLAGS for a in rest):
        return "error", ()
    if any(a in HELP_FLAGS for a in rest):
        return "help", ()
    return cmd, tuple(rest)


def main(argv=None) -> int:
    # Encoding FIRST: every later write (the usage text, the bootstrap report, uvicorn's log lines, tracebacks) goes through the
    # re-configured streams, so nothing printed here can raise UnicodeEncodeError on a cp1252 or latin-1 console or pipe.
    _utf8_stdio()
    argv = sys.argv[1:] if argv is None else list(argv)
    action, flags = parse_args(argv)
    if action == "help":
        sys.stdout.write(USAGE)
        sys.stdout.flush()
        return 0
    if action == "error":
        sys.stderr.write(USAGE)
        sys.stderr.flush()
        return 2
    if action == "bootstrap":
        sys.argv = ["agentic_os.mission.bootstrap"]
        runpy.run_module("agentic_os.mission.bootstrap", run_name="__main__")
        return 0
    if "--exit-on-stdin-eof" in flags:
        threading.Thread(target=_watch_stdin, name="launcher-tether", daemon=True).start()
    from agentic_os.projects_api import main as serve
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())

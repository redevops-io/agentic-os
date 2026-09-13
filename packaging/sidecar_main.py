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


def main() -> None:
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

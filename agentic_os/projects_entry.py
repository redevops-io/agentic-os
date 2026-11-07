"""Entry point of the Projects server for the sidecar's ``serve`` and the ``agentic-os-projects`` console script.

``agentic_os.projects_api`` builds the app when it is imported, so an invalid ``PROJECTS_DRILLDOWN_*`` configuration
(``DrilldownConfigError``) surfaced as a Python traceback (and, in the frozen sidecar, a PyInstaller "Failed to execute script" line). The
refusal itself is right: exit 1, nothing bound, no launch code, no hand-off record. This wrapper keeps all of that and prints ONE line
on stderr: the error's own fixed text, which names the environment keys to set and never a value. Every other exception keeps its
traceback.
"""
from __future__ import annotations

import sys

MAX_LINE = 300


def refusal_line(err: BaseException) -> str:
    """``refusing to start: <first line of the message>``, at most MAX_LINE characters of message."""
    text = (str(err).strip().splitlines() or ["invalid configuration"])[0]
    return "refusing to start: " + text[:MAX_LINE]


def main() -> int:
    from .drilldown.mount import DrilldownConfigError
    try:
        from .projects_api import main as serve        # importing builds the app: a bad drill-down configuration raises here
    except DrilldownConfigError as e:
        sys.stderr.write(refusal_line(e) + "\n")
        sys.stderr.flush()
        return 1
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())

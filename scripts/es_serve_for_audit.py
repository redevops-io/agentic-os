"""Serve Edge Sentinel for the UI audit.

Two constraints the launcher has to respect, both learned the hard way in this repo:
* the app package directory is `apps/edge-sentinel` (hyphen), so it is only reachable through
  importlib with the dotted-hyphen name — that is exactly what apps/*'s own tests do;
* `apps/edge-sentinel` itself must NOT be on sys.path, because its operator.py shadows the stdlib
  `operator` module and breaks Python before the app starts (audit item P1).
"""
import importlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent          # the repo root, wherever this checkout lives
sys.path[:0] = [str(REPO), str(REPO / "apps")]

import uvicorn  # noqa: E402  (after sys.path is set, before the app imports the kernel)

app = importlib.import_module("edge-sentinel.app").app
uvicorn.run(app, host="127.0.0.1", port=8299, log_level="warning")

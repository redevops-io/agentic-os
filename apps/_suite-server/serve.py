"""Base-aware entrypoint for the suite-server image.

Builds the suite app (composition over the canonical operators) and, when SUITE_BASE is set, mounts
it under that path prefix — so it can sit behind a path-routing proxy that does NOT strip the prefix
(e.g. cloudflared routing demo.redevops.io/suites -> here). SUITE picks the domain suite.
"""
from __future__ import annotations

import importlib
import os

import uvicorn
from fastapi import FastAPI

from agentic_os import suites as _suites
from agentic_os.app_kit.suite_server import build_suite_app, operators_for_suite

_name = os.environ.get("SUITE", "revenue")
_suite = {s.name: s for s in _suites.DOMAIN_SUITES}[_name]
# Permissions (FR-01): this process builds the operators of every app in the suite WITHOUT importing their app modules, so it installs their gates itself. With
# AGENTIC_OS_PERMISSIONS on each app is gated (restricted until `agentic-os permissions init`); with CR_PERMISSIONS / CR_IDENTITY_* it follows the enterprise rule
# (it refuses to start without the package); with nothing requested nothing changes. An app with no tool table is denied everything under basic permissions.
from agentic_os.app_gate import install_gate as _install_gate  # noqa: E402
for _a in _suite.apps:
    _install_gate(_a)
_imp = lambda a: importlib.import_module(f"{a}.manifest")  # noqa: E731
_app = build_suite_app(_suite, operators_for_suite(_suite, _imp),
                       manifests={a: _imp(a).MANIFEST for a in _suite.apps})

_base = os.environ.get("SUITE_BASE", "").rstrip("/")
if _base:
    _parent = FastAPI(title=f"suite:{_name}")
    _parent.mount(_base, _app)
    _app = _parent

if __name__ == "__main__":
    uvicorn.run(_app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8300")))

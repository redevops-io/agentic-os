"""Discovery of every app's Operator via its own ``build_*_operator`` builder (no network, no app server)."""
from __future__ import annotations

import importlib
from pathlib import Path

APPS_DIR = Path(__file__).resolve().parents[2] / "apps"
OPERATOR_APPS = sorted(p.parent.name for p in APPS_DIR.glob("*/operator.py"))


def build_operator(app: str):
    """Import ``<app>.operator`` and call its ``build_*_operator()``. Raises ImportError if the app needs a
    package that is not installed (video-ad needs ``reel``) -- callers turn that into a skip."""
    mod = importlib.import_module(f"{app}.operator")
    builders = [getattr(mod, n) for n in dir(mod) if n.startswith("build_") and n.endswith("_operator")]
    assert builders, f"{app}/operator.py exposes no build_*_operator()"
    return builders[0]()

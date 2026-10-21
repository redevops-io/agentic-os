"""Wheel-build hook: make sure the pinned Projects UI is in agentic_os/projects_ui/ before it is packaged.

Runs for editable installs too (CI and dev checkouts need the bundle to serve the UI); it is a no-op once the pinned
sha256 is already in place. Set AGENTIC_OS_SKIP_UI_FETCH=1 to skip (offline builds that already have a bundle).
"""
import importlib.util
import json
import os
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class ProjectsUiHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version, build_data):
        if os.environ.get("AGENTIC_OS_SKIP_UI_FETCH") == "1":
            return
        root = Path(self.root)
        spec = importlib.util.spec_from_file_location("fetch_projects_ui", root / "scripts" / "fetch_projects_ui.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        lock = json.loads((root / "packaging" / "projects_ui.lock.json").read_text())
        status = mod.fetch(lock, root / "agentic_os" / "projects_ui")
        self.app.display_info(f"projects UI: {status}")

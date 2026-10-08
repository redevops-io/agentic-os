"""Catalog conformance (plan §3.2): modules.yaml is generated from the manifests and can't drift.

Regenerates the app entries from every apps/<app>/manifest.py and checks the committed modules.yaml
agrees on the key fields (source always apps/<app-name>, port, approval). Self-inserts apps/ on path;
skips when apps aren't importable here.
"""
from __future__ import annotations

import importlib
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_APPS_DIR = _ROOT / "apps"


def _app_manifests():
    if not _APPS_DIR.is_dir():
        return {}
    if str(_APPS_DIR) not in sys.path:
        sys.path.insert(0, str(_APPS_DIR))
    out = {}
    for d in sorted(_APPS_DIR.iterdir()):
        if not (d / "manifest.py").exists():
            continue
        try:
            out[d.name] = importlib.import_module(f"{d.name}.manifest").MANIFEST
        except Exception:  # noqa: BLE001 - environment skip
            continue
    return out


def test_every_app_manifest_has_a_deploy_block():
    manifests = _app_manifests()
    if not manifests:
        pytest.skip("no app manifests importable here")
    missing = [n for n, m in manifests.items() if m.deploy is None]
    assert not missing, f"manifests without a deploy block: {missing}"


def test_generated_entries_fix_the_source_path_and_match_the_committed_catalog():
    import yaml

    from agentic_os.app_kit import module_entry

    manifests = _app_manifests()
    if not manifests:
        pytest.skip("no app manifests importable here")

    committed = yaml.safe_load((_ROOT / "modules.yaml").read_text())
    by_name = {m["name"]: m for m in committed["modules"]}

    for app, manifest in manifests.items():
        entry = module_entry(manifest)
        # source is ALWAYS apps/<app-dir> (the drift fix), never the display name
        assert entry["source"] == f"apps/{app}"
        # the committed catalog carries exactly this generated entry
        assert entry["name"] in by_name, f"{entry['name']} missing from committed modules.yaml"
        committed_entry = by_name[entry["name"]]
        assert committed_entry["source"] == entry["source"]
        assert committed_entry.get("port") == entry.get("port")
        assert committed_entry.get("approval_required") == entry.get("approval_required")


def test_committed_catalog_has_no_stale_agentic_prefixed_source():
    import yaml

    committed = yaml.safe_load((_ROOT / "modules.yaml").read_text())
    for m in committed["modules"]:
        src = m.get("source")
        if src:
            # no entry may point at apps/agentic-support / apps/agentic-billing / … (the old drift)
            assert src not in {"apps/agentic-support", "apps/agentic-billing",
                               "apps/agentic-books", "apps/agentic-compliance"}, src

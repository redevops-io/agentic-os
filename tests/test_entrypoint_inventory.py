"""F20 reachability gate: a new way to cause an effect cannot appear unreviewed.

* apps/*: every non-REST entry type (WebSocket, background thread/task, lifecycle hook, scheduler library, webhook-shaped
  route, mounted sub-app) must have a reviewed entry in apps/other_entrypoints.json.
* agentic_os/**: the write routes that show no authentication marker are pinned in audit/kernel_unauthenticated_routes.json.
  A NEW one fails CI; fixing one requires shrinking the baseline, so the list only ever gets shorter."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("audit_inventory", ROOT / "scripts" / "audit_operator_capabilities.py")
inv = importlib.util.module_from_spec(spec)
sys.modules["audit_inventory"] = inv
spec.loader.exec_module(inv)


def _key(e):
    return (f"{e['app']}/{e['file']}", e["kind"], e["name"])


def test_every_non_rest_entry_type_in_the_apps_is_reviewed():
    reviewed = {_key(e) for e in json.loads((ROOT / "apps" / "other_entrypoints.json").read_text())["entries"]}
    found = {_key(e) for e in inv.other_entrypoints()}
    assert found - reviewed == set(), f"new entry point(s) with no reviewed classification: {sorted(found - reviewed)}"
    assert reviewed - found == set(), f"reviewed entry point(s) that no longer exist (remove them): {sorted(reviewed - found)}"


def test_every_reviewed_entry_point_has_a_real_rationale():
    for e in json.loads((ROOT / "apps" / "other_entrypoints.json").read_text())["entries"]:
        assert len(e["rationale"]) > 40, e


def test_kernel_unauthenticated_write_routes_are_pinned_and_only_shrink():
    base = {(r["file"], r["route"], r["handler"]) for r in
            json.loads((ROOT / "audit" / "kernel_unauthenticated_routes.json").read_text())["routes"]}
    now = {(r["file"], r["route"], r["handler"]) for r in inv.kernel_routes() if not r["auth_marker"]}
    assert now - base == set(), f"NEW write route(s) in agentic_os with no authentication marker: {sorted(now - base)}"
    assert base - now == set(), f"routes now authenticated or removed: shrink audit/kernel_unauthenticated_routes.json: {sorted(base - now)}"

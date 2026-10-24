#!/usr/bin/env python3
"""Inventory of side-effecting capabilities and direct write routes in apps/*, parsed with `ast` (no imports, no execution).

Classifies each side-effecting capability by what stands between a caller and its handler:
  approval_required  declared mandatory approval (runtime gate; the HTTP /invoke stages it)
  gated()            the handler is wrapped in approval_gate.gated (stages unless a verified approval is present)
  operator boundary  reachable only through Operator /invoke, which in the production profile requires an approval or an
                     execution authorization (mission/operator_sdk.py); NOT covered if the app also exposes the same
                     handler on its own REST route
It also counts each app's own write routes (POST/PUT/PATCH/DELETE) because those bypass the operator entirely.

Usage: python scripts/audit_operator_capabilities.py [--md audit/operator_capability_inventory.md]
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WRITE_METHODS = {"post", "put", "patch", "delete"}


def kw(call: ast.Call, name: str):
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def is_true(node) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def capabilities(path: Path) -> list[dict]:
    out = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "capability"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        handler = node.args[1] if len(node.args) > 1 else kw(node, "handler")
        gated = isinstance(handler, ast.Call) and isinstance(handler.func, ast.Name) and handler.func.id == "gated"
        out.append({"name": node.args[0].value, "side_effecting": is_true(kw(node, "side_effecting")),
                    "approval_required": is_true(kw(node, "approval_required")), "gated": gated,
                    "has_undo": kw(node, "undo") is not None})
    return out


def write_routes(app_dir: Path) -> list[str]:
    routes = []
    for path in sorted(app_dir.glob("*.py")):
        if path.name == "operator.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in node.decorator_list:
                    if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in WRITE_METHODS
                            and d.args and isinstance(d.args[0], ast.Constant)):
                        routes.append(f"{d.func.attr.upper()} {d.args[0].value}")
    return routes


AUTH_MARKERS = ("Depends(", "GATE", "require_", "_authorize", "resolve_caller", "admin_only", "authorization", "x_api_key",
                "mission_gate", "_auth")


def other_entrypoints() -> list:
    """Entry types besides REST write routes that can cause effects in apps/*: WebSocket routes, background threads/tasks,
    lifecycle hooks, scheduler libraries, webhook/callback-shaped routes, mounted sub-applications. Each needs a
    reviewed classification in apps/other_entrypoints.json."""
    found = []
    sched = ("apscheduler", "schedule.every", "threading.Timer", "BackgroundScheduler", "celery", "croniter")
    for path in sorted((ROOT / "apps").glob("*/*.py")):
        if path.name.startswith("test_") or path.name in ("seed.py", "conftest.py"):
            continue
        app = path.parent.name
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in node.decorator_list:
                    t = ast.get_source_segment(src, d) or ""
                    if t.startswith(("app.websocket(", "router.websocket(")) or ".on_event(" in t or ".add_api_route(" in t:
                        found.append({"app": app, "file": path.name, "kind": "websocket" if "websocket" in t else "lifecycle_hook",
                                      "name": node.name})
                    if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in WRITE_METHODS
                            and d.args and isinstance(d.args[0], ast.Constant)
                            and any(w in str(d.args[0].value).lower() for w in ("webhook", "callback", "hook", "notify"))):
                        found.append({"app": app, "file": path.name, "kind": "webhook_route", "name": node.name})
            if isinstance(node, ast.Call):
                f = ast.get_source_segment(src, node.func) or ""
                if f in ("threading.Thread", "Thread") or f.endswith(".create_task") or f.endswith(".add_task"):
                    found.append({"app": app, "file": path.name, "kind": "background_thread_or_task", "name": f})
                if f.endswith(".mount"):
                    found.append({"app": app, "file": path.name, "kind": "mounted_subapp", "name": f})
        for k in sched:
            if k in src and path.name != "safe_fetch.py":
                found.append({"app": app, "file": path.name, "kind": "scheduler_library", "name": k})
    uniq = {(f["app"], f["file"], f["kind"], f["name"]): f for f in found}
    return [uniq[k] for k in sorted(uniq)]


def kernel_routes() -> list:
    """Write routes of the kernel services (agentic_os/**), with whether any authentication marker appears on the handler
    or its decorators. A heuristic: no marker means 'needs review', not 'proven open'."""
    out = []
    for path in sorted((ROOT / "agentic_os").rglob("*.py")):
        rel = path.relative_to(ROOT / "agentic_os").as_posix()
        if "tests" in path.parts or path.name.startswith("test_"):
            continue
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        # a module that installs the route-authorization layer on its app is enforced route by route in production; the CI test of that
        # module (every write route has a policy) is what keeps the claim true
        file_guarded = "_route_authz.install(" in src
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in n.decorator_list:
                    if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                            and d.func.attr in WRITE_METHODS | {"websocket"} and d.args and isinstance(d.args[0], ast.Constant)):
                        seg = (ast.get_source_segment(src, n) or "") + " ".join(ast.get_source_segment(src, x) or "" for x in n.decorator_list)
                        out.append({"file": rel, "route": f"{d.func.attr.upper()} {d.args[0].value}", "handler": n.name,
                                    "auth_marker": file_guarded or any(m in seg for m in AUTH_MARKERS)})
    return out


def inventory() -> dict:
    apps = {}
    for op in sorted((ROOT / "apps").glob("*/operator.py")):
        app_dir = op.parent
        caps = [c for c in capabilities(op) if c["side_effecting"]]
        apps[app_dir.name] = {"capabilities": caps, "write_routes": write_routes(app_dir)}
    return apps


def render(inv: dict) -> str:
    tot = sum(len(a["capabilities"]) for a in inv.values())
    gated = sum(c["gated"] for a in inv.values() for c in a["capabilities"])
    mand = sum(c["approval_required"] for a in inv.values() for c in a["capabilities"])
    handler_level = sum(1 for a in inv.values() for c in a["capabilities"] if c["gated"])
    declared_only = sum(1 for a in inv.values() for c in a["capabilities"] if c["approval_required"] and not c["gated"])
    bare = sum(1 for a in inv.values() for c in a["capabilities"] if not c["gated"] and not c["approval_required"])
    routes = sum(len(a["write_routes"]) for a in inv.values())
    lines = ["# Operator capability inventory (generated)", "",
             "Generated by `scripts/audit_operator_capabilities.py` (AST only; nothing imported or executed).", "",
             f"* Side-effecting capabilities: **{tot}** across {len(inv)} apps",
             f"* Wrapped in `gated()`: **{gated}**; declared `approval_required`: **{mand}**",
             f"* Handler-level protection (`gated()`): **{handler_level}**. Declared `approval_required` WITHOUT `gated()`: **{declared_only}** "
             "(the mission runtime honours the flag; the operator's HTTP `/invoke` does not enforce it by itself). "
             f"Neither: **{bare}**.",
             f"* Direct write routes on the apps' own REST surfaces (POST/PUT/PATCH/DELETE; these bypass the operator): **{routes}**", "",
             "| App | Side-effecting capabilities | gated() | approval_required | no undo | own write routes |", "|---|---|---|---|---|---|"]
    for name, a in inv.items():
        cs = a["capabilities"]
        lines.append(f"| {name} | {len(cs)} | {sum(c['gated'] for c in cs)} | {sum(c['approval_required'] for c in cs)} | "
                     f"{sum(not c['has_undo'] for c in cs)} | {len(a['write_routes'])} |")
    lines += ["", "## Capabilities", "", "| App | Capability | gated() | approval_required | undo |", "|---|---|---|---|---|"]
    for name, a in inv.items():
        for c in a["capabilities"]:
            lines.append(f"| {name} | `{c['name']}` | {'yes' if c['gated'] else ''} | {'yes' if c['approval_required'] else ''} | "
                         f"{'yes' if c['has_undo'] else 'NO'} |")
    ent = other_entrypoints()
    lines += ["", "## Other entry types in apps/* (WebSocket, background jobs, hooks, schedulers, webhooks, mounts)", ""]
    lines += [f"* `{e['app']}/{e['file']}`: {e['kind']} ({e['name']})" for e in ent] or ["* none"]
    kr = kernel_routes()
    noauth = [r for r in kr if not r["auth_marker"]]
    lines += ["", "## Kernel service write routes (agentic_os/**)", "",
              f"{len(kr)} write routes; **{len(noauth)}** show no authentication marker on the handler (heuristic: needs review, not proven open).", "",
              "| File | Route | Handler | Auth marker |", "|---|---|---|---|"]
    lines += [f"| {r['file']} | `{r['route']}` | {r['handler']} | {'yes' if r['auth_marker'] else '**no**'} |" for r in kr]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--md")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    inv = inventory()
    if a.json:
        json.dump(inv, sys.stdout, indent=2)
    out = render(inv)
    if a.md:
        Path(a.md).write_text(out, encoding="utf-8")
    elif not a.json:
        print(out)

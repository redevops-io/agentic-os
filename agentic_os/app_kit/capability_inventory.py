"""A machine-readable capability manifest for every app, generated from the code that runs - not written by hand.

    python -m agentic_os.app_kit.capability_inventory --write     # regenerate (needs a source checkout: it imports apps/)
    python -m agentic_os.app_kit.capability_inventory --check     # exit 1 if a committed manifest is stale

One manifest per app (``capability_manifests/<app>.json``, schema ``rails.app-capabilities/1``) joins what the repository
already declares:

* the app's **Operator** capabilities - typed inputs and outputs, side effects, approval, reversibility;
* the permission gate's **tool table** (``agentic_os.tool_actions``) - the read / write / delete class of each tool;
* every **HTTP route** the running application object serves, including lazily included routers, with the gate decision the
  route table records for it (``apps/_route_table.py``);
* what an **external agent** can reach through the Governed Agent Gateway - read from the gateway's own registry.

An operation is ``external_access.gateway = true`` only when the gateway registry holds a capability of that name. Reaching a
route with the operator's own credential is NOT external-agent access: it has no agent identity, grant, budget or audit.

``gateway_candidate = true`` marks what could be offered next with the least new work: not reachable yet, a read, typed
inputs and outputs, a GET route, declared data classifications (egress rules act on them) and not a person's decision.
Every other unreachable read says in ``gateway_blockers`` what it lacks. It is a list for a person to work through:
nothing is registered because of it.
An app that cannot be loaded is reported as ``not_loadable``; it never appears as an app with nothing to offer.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = "rails.app-capabilities/1"
HERE = Path(__file__).resolve().parent
MANIFEST_DIR = HERE / "capability_manifests"
REPORT = "docs/CAPABILITY_COVERAGE.md"
#: the work surface itself: its application is built by the package, not by a directory under apps/
PROJECTS = "projects"
#: composes other apps' ASGI applications and adds no operation of its own
COMPOSITE = {"_suite-server"}
_RECORD = "\x00"


def repo_root() -> Path:
    root = HERE.parents[1]
    if not (root / "apps").is_dir():
        raise SystemExit("this needs a source checkout of agentic-os: the apps/ directory is not next to the package")
    return root


def app_names(root: Path) -> List[str]:
    return sorted([PROJECTS] + [d.name for d in (root / "apps").iterdir()
                                if d.is_dir() and d.name not in COMPOSITE and not d.name.startswith((".", "__"))])


# ── reading one app (run in its own process: apps share module names and install process-wide gates) ─────────
def walk_routes(routes, out: set) -> None:
    for r in routes:
        if hasattr(r, "effective_candidates"):                        # a lazily included router has no path of its own
            walk_routes(r.effective_candidates(), out)
            continue
        route = getattr(r, "original_route", r)
        path = getattr(r, "path", "") or getattr(getattr(r, "starlette_route", None), "path", "") or getattr(route, "path", "")
        if not path:
            continue
        if "WebSocket" in type(route).__name__:
            out.add(("WS", path))
        for method in (getattr(route, "methods", None) or ()):
            if method not in ("HEAD", "OPTIONS"):
                out.add((method, path))


def _error(exc: BaseException) -> str:
    """The kind of failure and a missing module's name. Never the full message: it can carry paths of the machine."""
    missing = getattr(exc, "name", None) if isinstance(exc, ImportError) else None
    return type(exc).__name__ + (f": {missing}" if missing else "")


def _operators(module) -> list:
    from agentic_os.mission.operator_sdk import Operator
    found = [v for v in vars(module).values() if isinstance(v, Operator)]
    if found:
        return found
    for name, fn in sorted(vars(module).items()):
        if (re.match(r"^build_\w*operator$", name) and inspect.isfunction(fn) and fn.__module__ == module.__name__
                and all(p.default is not p.empty or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
                        for p in inspect.signature(fn).parameters.values())):
            found.append(fn())
    return found


def _capability(spec) -> Dict[str, Any]:
    compensation = getattr(spec, "compensation", None)
    supported = getattr(compensation, "supported", None) if compensation is not None else None
    if supported or getattr(spec, "undo", None):
        reversibility = {"kind": "compensable", "with": getattr(compensation, "capability", None) or spec.undo}
    elif getattr(spec, "irreversible", False):
        reversibility = {"kind": "irreversible"}
    else:
        reversibility = {"kind": "undeclared"} if spec.side_effecting else {"kind": "not_applicable"}
    return {"side_effecting": bool(spec.side_effecting), "approval_required": bool(spec.approval_required),
            "reversibility": reversibility, "inputs": dict(sorted((spec.inputs or {}).items())),
            "outputs": dict(sorted((spec.outputs or {}).items())),
            "data_classifications": sorted(spec.data_classifications or [])}


def _write_policies(asgi) -> Dict[str, Any]:
    """The policy the app declares for each state-changing route (``route_authz``): who may perform it."""
    return {key: {"capability": p.capability, "access": p.access, "permission": p.permission, "agent_actions": sorted(p.agent_actions)}
            for key, p in sorted((getattr(asgi.state, "route_policies", None) or {}).items())}


def read_projects() -> Dict[str, Any]:
    out: Dict[str, Any] = {"app": PROJECTS, "routes": [], "operator": {}, "tools": {}, "problems": [], "ui_calls": [], "write_policies": {}}
    try:
        asgi = importlib.import_module("agentic_os.projects_api").create_app()
        routes: set = set()
        walk_routes(asgi.router.routes, routes)
        out["routes"], out["write_policies"] = sorted(routes), _write_policies(asgi)
    except BaseException as exc:  # noqa: BLE001
        out["problems"].append(f"the Projects application could not be built ({_error(exc)})")
    out["problems"].append("the Projects web UI is built from another repository (redevops-projects): its calls are not inventoried")
    return out


def read_app(root: Path, app: str) -> Dict[str, Any]:
    sys.path[:0] = [str(root), str(root / "apps")]
    if app == PROJECTS:
        return read_projects()
    out: Dict[str, Any] = {"app": app, "routes": [], "operator": {}, "tools": {}, "problems": [], "write_policies": {}}
    folder = root / "apps" / app
    if (folder / "app.py").exists():
        try:
            module = importlib.import_module(f"{app}.app")
            routes: set = set()
            walk_routes(module.app.router.routes, routes)
            out["routes"] = sorted(routes)
            out["write_policies"] = _write_policies(module.app)
        except BaseException as exc:  # noqa: BLE001 - reported, never read as "no routes"
            out["problems"].append(f"app.py could not be loaded ({_error(exc)}): its HTTP routes are not inventoried")
    if (folder / "operator.py").exists():
        try:
            operators = _operators(importlib.import_module(f"{app}.operator"))
            if not operators:
                out["problems"].append("operator.py builds no operator without arguments: its capabilities are not inventoried")
            for op in operators:
                for spec in op.manifest.capabilities:
                    out["operator"][spec.name] = _capability(spec)
        except BaseException as exc:  # noqa: BLE001
            out["problems"].append(f"operator.py could not be loaded ({_error(exc)}): its capabilities are not inventoried")
    if (folder / "server.py").exists() and not (folder / "app.py").exists():
        try:
            tools = getattr(importlib.import_module(f"{app}.server"), "_TOOLS", None)
            if isinstance(tools, dict):
                out["service_tools"] = sorted(tools)
            else:
                out["problems"].append("server.py exposes no tool table this inventory can read")
        except BaseException as exc:  # noqa: BLE001
            out["problems"].append(f"server.py could not be loaded ({_error(exc)}): its tools are not inventoried")
    from agentic_os.tool_actions import TOOL_ACTIONS
    out["tools"] = dict(sorted(TOOL_ACTIONS.get(app, {}).items()))
    out["ui_calls"] = ui_calls(folder)
    return out


def probe(root: Path, app: str) -> Dict[str, Any]:
    env = {"HOME": os.environ.get("HOME", "/"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "AGENTIC_OS_PERMISSIONS": "off", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(root)}
    run = subprocess.run([sys.executable, "-m", __spec__.name if __spec__ else "agentic_os.app_kit.capability_inventory",
                          "--read", app], cwd=str(root), env=env, capture_output=True, text=True, timeout=180)
    line = next((x[1:] for x in run.stdout.splitlines() if x.startswith(_RECORD)), None)
    if line is None:
        raise RuntimeError(f"{app}: the probe reported nothing (exit {run.returncode})")
    return json.loads(line)


# ── what the app's own UI calls ─────────────────────────────────────────────────────────────────────────────
#: a literal path: rooted (``/x``) or relative to the page (``api/x``, ``agent/x``), optionally after a ``${base}`` placeholder
_PATH = r"""[`'"](?:\$\{[^}`'"]*\})?((?:/|(?:api|agent|v1)/)[A-Za-z0-9_\-./]*)"""
_CALL_JS = re.compile(r"""(?:\bfetch|\bapi|\bget|\bpost|\bput|\bdel|getJSON|postJSON)\(\s*""" + _PATH)
_CALL_INLINE = re.compile(r"""\bfetch\(\s*""" + _PATH)
_FORM = re.compile(r"""<form[^>]{0,200}?\baction=\\?""" + _PATH)


def ui_calls(folder: Path) -> List[str]:
    """Paths the app's front-end code requests: its .js / .html files, and ``fetch`` in HTML pages written inside Python.
    A literal prefix only: what the code appends at run time is not visible, so ``/api/x/`` stands for ``/api/x/<something>``."""
    found: set = set()
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or "node_modules" in path.parts or path.name.startswith("test_") or "tests" in path.parts:
            continue
        if path.suffix in (".js", ".html"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            found |= set(_CALL_JS.findall(text)) | set(_FORM.findall(text))
        elif path.suffix == ".py":
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "<script" in text or "<form" in text:
                found |= set(_CALL_INLINE.findall(text)) | set(_FORM.findall(text))
    return sorted({c if c.startswith("/") else "/" + c for c in found} - {"/"})


def match_ui_call(call: str, routes: List[tuple]) -> Optional[str]:
    """The route template a UI call reaches, or None. A call written relative to an API base (``/cases`` for
    ``/api/v1/cases``) matches under that base; a call ending in ``/`` matches a route that continues with a parameter."""
    paths = [p for _, p in routes]
    for candidate in (call, "/api/v1" + call):
        for path in paths:
            if _matches(path, candidate) or (candidate.endswith("/") and (path.startswith(candidate) or path == candidate[:-1])):
                return path
    return None


# ── the gate's decision for each route, and the gateway's registry ──────────────────────────────────────────
def route_decisions(root: Path):
    sys.path.insert(0, str(root / "apps"))
    return importlib.import_module("_route_table")


def _matches(template: str, path: str) -> bool:
    from starlette.routing import compile_path
    return template == path or bool(compile_path(template)[0].match(path))


def classify_route(table, app: str, method: str, path: str) -> Dict[str, Any]:
    tools: List[Dict[str, Any]] = []
    for m, p, body, tool, _kind in table.ROUTES.get(app, []):
        if m == method and _matches(path, p.split("?")[0]):
            row = {"tool": tool}
            if isinstance(body, dict) and "action" in body:
                row["action"] = body["action"]                          # one route, many operations, told apart by the body
            if row not in tools:
                tools.append(row)
    if tools:
        return {"gate": "tool", "tools": sorted(tools, key=lambda r: (r["tool"], str(r.get("action"))))}
    for label, entries in (("public_by_design", table.PUBLIC.get(app, {})), ("operator_credential", table.OPERATOR_ROUTES),
                           ("framework", table.FRAMEWORK_ROUTES), ("assistant_console", getattr(table, "ENGINE_CONSOLES", {}).get(app, {})),
                           ("operator_authenticated", getattr(table, "OPERATOR_AUTH_ROUTES", {}).get(app, {}))):
        for entry in entries:
            if entry[0] == method and _matches(path, entry[1]) or entry == (method, path):
                return {"gate": label}
    return {"gate": "not_in_route_table"}


def gateway_capabilities() -> Dict[str, Dict[str, Any]]:
    """What the gateway the Projects API mounts offers an external agent: its read registry with the ports that mount gives it."""
    from agentic_os.agent_gateway.capabilities import build_read_registry

    class _Port:                                                        # registration needs a backend object, never a call
        def __getattr__(self, name):
            return lambda *a, **k: None

    registry = build_read_registry(projections=_Port(), missions=_Port(), sources=_Port(), dashboards=_Port())
    return {m.name: {"side_effecting": m.side_effecting, "permissions": sorted(m.permissions)} for m in registry.all()}


def _access(name: str, gateway: Dict[str, Any]) -> Dict[str, Any]:
    if name in gateway:
        return {"gateway": True}
    return {"gateway": False, "reason": "not registered in the Governed Agent Gateway"}


def gateway_blockers(op: Dict[str, Any]) -> Optional[List[str]]:
    """What stands between a read and being offered to an agent; ``None`` for an operation the question does not arise
    for (already reachable, a write, or a person's decision)."""
    if (op["external_access"]["gateway"] or op["action"] != "read" or op.get("side_effecting")
            or "human_only" in op.get("route_access", [])):
        return None
    return [why for why, blocked in (("no typed inputs and outputs", not op["typed"]),
                                     ("no GET route", not any(h["method"] == "GET" for h in op["http"])),
                                     ("no data classification", not op.get("data_classifications"))) if blocked]


def _gateway_of(app: str, gateway: Dict[str, Any]) -> Dict[str, Any]:
    """The gateway's capabilities belong to the platform: the Projects manifest lists them as its own operations."""
    return gateway if app == PROJECTS else {}


def build_manifest(raw: Dict[str, Any], table, gateway: Dict[str, Any]) -> Dict[str, Any]:
    app = raw["app"]
    routes = []
    reached: Dict[str, List[Dict[str, str]]] = {}
    for method, path in raw["routes"]:
        policy = raw.get("write_policies", {}).get(f"{method} {path}")
        if app == PROJECTS:
            decision = {"gate": "route_policy" if policy else ("projects_sign_in" if method == "GET" else "not_in_route_table")}
        else:
            decision = classify_route(table, app, method, path)
        row_out = {"method": method, "path": path, **decision}
        if policy:
            row_out["write_policy"] = policy
        routes.append(row_out)
        for row in decision.get("tools", []):
            entry = {"method": method, "path": path, **({"action": row["action"]} if "action" in row else {})}
            reached.setdefault(row["tool"], []).append(entry)
    operations = []
    declared_writes: Dict[str, List[Dict[str, Any]]] = {}
    if app == PROJECTS:                                             # its operations are what its write policies name, plus the gateway's
        for key, policy in raw.get("write_policies", {}).items():
            if policy["capability"]:
                method, path = key.split(" ", 1)
                declared_writes.setdefault(policy["capability"], []).append({"method": method, "path": path, "access": policy["access"]})
        for name, rows in declared_writes.items():
            reached[name] = [{"method": r["method"], "path": r["path"]} for r in rows]
    names = sorted(set(raw["operator"]) | set(raw["tools"]) | set(raw.get("service_tools", [])) | set(declared_writes) | set(_gateway_of(app, gateway)))
    for name in names:
        declared = [s for s, present in (("operator", name in raw["operator"]), ("gate", name in raw["tools"]),
                                         ("service", name in raw.get("service_tools", []))) if present]
        spec = raw["operator"].get(name)
        if name in declared_writes:
            declared.append("route_policy")
        if name in _gateway_of(app, gateway):
            declared.append("gateway")
        action = raw["tools"].get(name) or ("unclassified" if spec is None else ("write" if spec["side_effecting"] else "read"))
        source = "gate" if name in raw["tools"] else ("operator" if spec else "none")
        access_classes: List[str] = sorted({r["access"] for r in declared_writes.get(name, [])})
        if name in _gateway_of(app, gateway):
            action, source = ("write" if gateway[name]["side_effecting"] else "read"), "gateway"
        elif access_classes:
            action, source = ("read" if access_classes == ["read_only"] else "write"), "route_policy"
        op: Dict[str, Any] = {"id": name, "declared_by": declared, "action": action,
                              "action_source": source,
                              "typed": spec is not None, "http": reached.get(name, []), "external_access": _access(name, gateway)}
        policy_classes = access_classes or sorted({r["write_policy"]["access"] for r in routes if "write_policy" in r
                                                   and any(h["method"] == r["method"] and h["path"] == r["path"] for h in reached.get(name, []))})
        if policy_classes:
            op["route_access"] = policy_classes             # e.g. human_only: a person's decision, never an agent's
        if spec:
            op.update(spec)
        blockers = gateway_blockers(op)
        op["gateway_candidate"] = blockers == []
        if blockers:
            op["gateway_blockers"] = blockers
        operations.append(op)
    ui = [{"call": call, "route": match_ui_call(call, raw["routes"])} for call in raw.get("ui_calls", [])]
    manifest = {"schema": SCHEMA, "app": app,
                #: ``route`` is null when this app serves no such path: a call to a wrapped application, or one the scan cannot resolve
                "ui_calls": ui,
                "status": "incomplete" if raw["problems"] else "complete",
                "problems": raw["problems"], "operations": operations, "routes": routes,
                "summary": {"operations": len(operations), "typed": sum(o["typed"] for o in operations),
                            "writes_or_deletes": sum(o["action"] in ("write", "delete") for o in operations),
                            "reachable_by_external_agents": sum(o["external_access"]["gateway"] for o in operations),
                            "gateway_candidates": sum(o["gateway_candidate"] for o in operations),
                            "routes": len(routes), "ui_calls": len(ui), "ui_calls_not_matched": sum(u["route"] is None for u in ui),
                            "routes_without_a_gate_decision": sum(r["gate"] == "not_in_route_table" for r in routes)}}
    body = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    manifest["digest"] = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    return manifest


def generate(root: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    root = root or repo_root()
    table = route_decisions(root)
    gateway = gateway_capabilities()
    return {app: build_manifest(probe(root, app), table, gateway) for app in app_names(root)}


def render(manifest: Dict[str, Any]) -> str:
    return json.dumps(manifest, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def coverage_report(manifests: Dict[str, Dict[str, Any]]) -> str:
    """Every operation an external agent cannot reach, per app, in words a person can act on."""
    total = lambda key: sum(m["summary"][key] for m in manifests.values())
    lines = ["# What external agents can reach", "",
             "Generated by `python -m agentic_os.app_kit.capability_inventory --write` from the code; do not edit. The same facts,",
             "per app and machine readable, are in `agentic_os/app_kit/capability_manifests/`.", "",
             f"**{total('reachable_by_external_agents')} of {total('operations')} operations** can be invoked by an external agent through the Governed Agent Gateway.",
             f"{total('writes_or_deletes')} operations write or delete. The applications serve {total('routes')} HTTP routes; calling one with the operator's own",
             "credential is not agent access (no agent identity, grant, budget or audit).", "",
             "| App | Operations | Reachable by agents | Write or delete | Typed inputs/outputs | Inventory |",
             "|---|---|---|---|---|---|"]
    for app, m in sorted(manifests.items()):
        s = m["summary"]
        lines.append(f"| {app} | {s['operations']} | {s['reachable_by_external_agents']} | {s['writes_or_deletes']} | {s['typed']} | {m['status']} |")
    candidates = [(a, o["id"]) for a, m in sorted(manifests.items()) for o in m["operations"] if o["gateway_candidate"]]
    lines += ["", "## Candidates to offer next", "",
              "Reads with typed inputs and outputs, a GET route and declared data classifications that no agent can reach yet. "
              "Nothing is registered automatically: each needs a decision about its data, its grant and its tenant scoping.", ""]
    lines += [f"- {a}: `{n}`" for a, n in candidates] or ["- none yet"]
    blocked: Dict[str, List[str]] = {}
    for a, man in sorted(manifests.items()):
        for o in man["operations"]:
            if o.get("gateway_blockers"):
                blocked.setdefault(", ".join(o["gateway_blockers"]), []).append(f"`{o['id']}`")
    lines += ["", "### What the other unreachable reads lack", ""]
    lines += [f"- **{why}** ({len(names)}): " + ", ".join(sorted(names)) for why, names in sorted(blocked.items(), key=lambda kv: (kv[0].count(", "), kv[0]))] or ["- nothing"]
    gaps = [(a, p) for a, m in sorted(manifests.items()) for p in m["problems"]]
    if gaps:
        lines += ["", "## Not inventoried", ""] + [f"- **{a}**: {p}" for a, p in gaps]
    lines += ["", "## Operations not accessible to external agents", "",
              "`human only` marks an operation with a route the application declares as a person's decision: it must not become agent-callable.", ""]
    for app, m in sorted(manifests.items()):
        missing = [o for o in m["operations"] if not o["external_access"]["gateway"]]
        if not missing:
            continue
        lines += [f"### {app}", ""]
        for action in ("delete", "write", "read", "unclassified"):
            names = [f"`{o['id']}`" + (" (human only)" if "human_only" in o.get("route_access", []) else " (approval)" if o.get("approval_required") else "")
                     for o in missing if o["action"] == action]
            if names:
                lines.append(f"- {action}: " + ", ".join(names))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def load_all(directory: Path = MANIFEST_DIR) -> Dict[str, Dict[str, Any]]:
    """The committed manifests, for a running deployment (no source checkout needed)."""
    return {m["app"]: m for m in (json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json")))}


def stale(generated: Dict[str, Dict[str, Any]], directory: Path = MANIFEST_DIR, report: Optional[Path] = None) -> List[str]:
    want = {f"{app}.json": render(m) for app, m in generated.items()}
    have = {p.name: p.read_text(encoding="utf-8") for p in directory.glob("*.json")}
    changed = sorted(n for n in set(want) | set(have) if want.get(n) != have.get(n))
    report = report or repo_root() / REPORT
    if not report.exists() or report.read_text(encoding="utf-8") != coverage_report(generated):
        changed.append(REPORT)
    return changed


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agentic_os.app_kit.capability_inventory", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true")
    group.add_argument("--check", action="store_true")
    group.add_argument("--read", metavar="APP", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.read:
        sys.stdout.write(_RECORD + json.dumps(read_app(repo_root(), args.read)) + "\n")
        return 0
    generated = generate()
    if args.check:
        changed = stale(generated)
        for name in changed:
            print(f"stale: {name}", file=sys.stderr)
        return 1 if changed else 0
    MANIFEST_DIR.mkdir(exist_ok=True)
    for old in MANIFEST_DIR.glob("*.json"):
        old.unlink()
    for app, manifest in generated.items():
        (MANIFEST_DIR / f"{app}.json").write_text(render(manifest), encoding="utf-8")
    (repo_root() / REPORT).write_text(coverage_report(generated), encoding="utf-8")
    for app, m in generated.items():
        s = m["summary"]
        print("%-18s %-10s operations %3d (typed %2d, write/delete %2d) external %d | routes %3d, no gate decision %d" % (
            app, m["status"], s["operations"], s["typed"], s["writes_or_deletes"], s["reachable_by_external_agents"], s["routes"], s["routes_without_a_gate_decision"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

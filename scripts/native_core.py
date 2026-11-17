#!/usr/bin/env python3
"""The minimal native core: which modules it is, and which tests cover it (stdlib only).

The native lanes (`.github/workflows/native-core.yml`, Windows and macOS) install only what the governance core
needs (`cryptography`, `PyYAML`, `runtime-contracts`, plus `pytest`) and run the tests that cover it. This script
derives both lists from the code so they cannot drift:

* MODULES: the static import closure (top-level imports only, parent packages included) of the ROOTS below. Lazy,
  in-function imports are not followed: they are optional paths and the lane does not install their dependencies.
* TESTS: every test file under tests/ whose `agentic_os` imports all fall inside MODULES plus ADJACENT (the state
  stores and containment code that the governance core relies on, which the closure does not reach), that imports
  at least one of them, and whose third-party imports are limited to what the lane installs.
* PENDING_ON_WINDOWS: test files that cannot run on Windows yet because the code they cover does not import there;
  each is a feature to implement (not a POSIX-only property), listed with its reason, and the lane skips only these
  and only on Windows. The list is meant to shrink to empty.
* DESELECT: individual tests that need something the lane does not install (the web stack). Each has a reason; they
  are reviewed by hand and checked here only for existing.

The committed manifest is scripts/native_core_manifest.json.

    python scripts/native_core.py --check     # fail if the manifest, the workflow pin or the closure drifted
    python scripts/native_core.py --write     # regenerate the manifest (keeps DESELECT as committed)
    python scripts/native_core.py --run [pytest args...]   # run the lane's tests with the current interpreter
    python scripts/native_core.py --import-all             # import every agentic_os module (the import smoke of the all-modules lane)
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "scripts" / "native_core_manifest.json"
WORKFLOW = ROOT / ".github" / "workflows" / "native-core.yml"

ROOTS = (
    "agentic_os.mission.runtime", "agentic_os.mission.approval", "agentic_os.mission.authority",
    "agentic_os.mission.store", "agentic_os.mission.receipt", "agentic_os.mission.verify",
    "agentic_os.mission.executor", "agentic_os.mission.control_auth", "agentic_os.mission.profile",
    "agentic_os.permissions", "agentic_os.governance.policy", "agentic_os.governance.receipt",
    "agentic_os.agent_gateway.gateway", "agentic_os.agent_gateway.audit_store",
)

# Modules outside the closure that the core's tests also exercise: the basic permissions plane, the durable
# stores and the containment/posture code (all stdlib plus the packages above).
ADJACENT = frozenset({
    "agentic_os.permissions_setup", "agentic_os.permissions_profiles", "agentic_os.app_gate", "agentic_os.priority_engine",
    "agentic_os.trusted_import", "agentic_os.outcome_store", "agentic_os.mission.fork_join",
    "agentic_os.mission.membrane", "agentic_os.mission.device_posture", "agentic_os.mission.execution_backend",
    "agentic_os.mission.gate_notify",
    "agentic_os.policy_promotion", "agentic_os.intervention_record", "agentic_os.intelligence", "agentic_os.intelligence.value_store",
    "agentic_os.intelligence.email", "agentic_os.intelligence.email.bridge", "agentic_os.world", "agentic_os.world.outreach",
    "agentic_os.mission.apps_e2e_runner", "agentic_os.mission.apps_e2e", "agentic_os.mission.decision_provider", "agentic_os.revenue",
    "agentic_os.revenue.discovery_bridge", "agentic_os.mission.provisioning", "agentic_os.mission.installer", "agentic_os.secrets", "agentic_os.secrets.stores",
    "agentic_os.secrets.resolve", "agentic_os.secrets.factory", "agentic_os.secrets.bootstrap",
})

# What the native lane installs (import names). `pytest` and its plugin namespace are the runner.
LANE_THIRD_PARTY = frozenset({"pytest", "_pytest", "cryptography", "yaml", "runtime_contracts"})
# Imports of the test tree itself (helpers beside the tests).
LOCAL_ROOTS = frozenset({"tests", "_platform_facts", "_fork_support", "conftest"})


def _modules() -> dict[str, tuple[Path, bool]]:
    mods: dict[str, tuple[Path, bool]] = {}
    for p in (ROOT / "agentic_os").rglob("*.py"):
        rel = ".".join(p.relative_to(ROOT).with_suffix("").parts)
        pkg = rel.endswith(".__init__")
        if pkg:
            rel = rel[: -len(".__init__")]
        mods[rel] = (p, pkg)
    return mods


def _resolve(mods, cur: str, is_pkg: bool, level: int, module: str, names: list[str]) -> set[str]:
    if level:
        base = cur.split(".") if is_pkg else cur.split(".")[:-1]
        base = base[: len(base) - (level - 1)] if level > 1 else base
        full = ".".join(base + ([module] if module else []))
    else:
        full = module
    if not full.startswith("agentic_os"):
        return set()
    return {c for c in [full] + [f"{full}.{n}" for n in names] if c in mods}


def closure(mods=None) -> list[str]:
    """Top-level import closure of ROOTS inside agentic_os."""
    mods = mods or _modules()
    seen: set[str] = set()
    stack = list(ROOTS)
    while stack:
        m = stack.pop()
        if m in seen or m not in mods:
            continue
        seen.add(m)
        path, is_pkg = mods[m]
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def walk(node, in_func: bool) -> None:
            for ch in ast.iter_child_nodes(node):
                inner = in_func or isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef))
                if isinstance(ch, (ast.Import, ast.ImportFrom)):
                    if inner:
                        continue
                    if isinstance(ch, ast.Import):
                        stack.extend(a.name for a in ch.names if a.name in mods)
                    elif ch.level or (ch.module or "").startswith("agentic_os"):
                        stack.extend(_resolve(mods, m, is_pkg, ch.level, ch.module or "", [a.name for a in ch.names]))
                else:
                    walk(ch, inner)

        walk(tree, False)
        parts = m.split(".")
        stack.extend(".".join(parts[:i]) for i in range(1, len(parts)) if ".".join(parts[:i]) in mods)
    return sorted(seen)


def covering_tests(core: list[str], mods=None) -> list[str]:
    mods = mods or _modules()
    scope = set(core) | ADJACENT
    std = set(sys.stdlib_module_names)
    out = []
    for p in sorted((ROOT / "tests").rglob("test_*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        used: set[str] = set()
        third: set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    (used if a.name.startswith("agentic_os") else third).add(a.name if a.name.startswith("agentic_os") else a.name.split(".")[0])
            elif isinstance(n, ast.ImportFrom) and not n.level:
                m = n.module or ""
                if m.startswith("agentic_os"):
                    used.add(m)
                    used.update(f"{m}.{a.name}" for a in n.names)
                else:
                    third.add(m.split(".")[0])
        used = {u for u in used if u in mods}
        third = {t for t in third if t not in std and t not in LOCAL_ROOTS and t != "agentic_os"}
        if used & scope and used <= scope and third <= LANE_THIRD_PARTY:
            out.append(p.relative_to(ROOT).as_posix())
    return out


def build() -> dict:
    mods = _modules()
    core = closure(mods)
    old = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}
    return {
        "note": "generated by scripts/native_core.py --write; `deselect` and `pending_on_windows` are maintained by hand (reason per entry)",
        "roots": list(ROOTS),
        "adjacent": sorted(ADJACENT),
        "modules": core,
        "tests": covering_tests(core, mods),
        "deselect": old.get("deselect", {}),
        "pending_on_windows": old.get("pending_on_windows", {}),
    }


def pinned_runtime_contracts() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'"(runtime-contracts @ git\+[^"]+)"', text)
    if not m:
        raise SystemExit("native_core: runtime-contracts pin not found in pyproject.toml")
    return m.group(1)


def check() -> int:
    errors: list[str] = []
    want = build()
    have = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for key in ("roots", "adjacent", "modules", "tests"):
        if want[key] != have.get(key):
            a, b = set(want[key]), set(have.get(key, []))
            errors.append(f"manifest `{key}` is stale (run --write): missing {sorted(a - b)[:5]} extra {sorted(b - a)[:5]}")
    for node in have.get("deselect", {}):
        path = node.split("::")[0]
        if not (ROOT / path).is_file():
            errors.append(f"deselected test file does not exist: {node}")
        elif path not in have["tests"]:
            errors.append(f"deselected test is not in the native lane: {node}")
    for path in have.get("pending_on_windows", {}):
        if path not in have["tests"]:
            errors.append(f"pending_on_windows entry is not in the native lane: {path}")
    if WORKFLOW.exists():
        if pinned_runtime_contracts() not in WORKFLOW.read_text(encoding="utf-8"):
            errors.append("native-core.yml does not install runtime-contracts at the pin in pyproject.toml")
    else:
        errors.append("the native-core workflow is missing")
    for e in errors:
        print("native_core:", e)
    if not errors:
        print(f"native_core: ok ({len(have['modules'])} modules, {len(have['tests'])} test files, {len(have['deselect'])} deselected)")
    return 1 if errors else 0


def run(extra: list[str]) -> int:
    have = json.loads(MANIFEST.read_text(encoding="utf-8"))
    args = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *extra]
    for node in have["deselect"]:
        args += ["--deselect", node]
    pending = have.get("pending_on_windows", {}) if sys.platform == "win32" else {}
    args += [t for t in have["tests"] if t not in pending]
    env = dict(os.environ)
    env.pop("APPROVAL_SECRET", None)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT), env.get("PYTHONPATH", "")]))
    return subprocess.call(args, cwd=ROOT, env=env)


# Distributions that the all-modules lane does NOT install (an extra it leaves out, or a Linux-hosted integration). A module that needs one of
# them and finds it missing is reported as skipped; ANY other import failure (a missing POSIX module, a bad guard, a syntax error, a missing
# package the lane does install) is a failure. Every package the lane installs is deliberately absent from this list.
OPTIONAL_IMPORTS = frozenset({
    "psycopg", "redevops_rag", "sentence_transformers", "keyring", "agent_harness", "context_runtime", "reel",
})


def import_all() -> int:
    """Import every module of the package in this interpreter. Prints one line per failure and a summary; exit 1 on any failure."""
    import importlib
    import tempfile

    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("PERMISSIONS_ALLOW_GENERATED_KEY_DEV", "1")             # control_plane builds its grant store at import
    os.environ.setdefault("PERMISSIONS_DIR", tempfile.mkdtemp(prefix="aos-import-"))
    os.environ.setdefault("REDEVOPS_SECRETS_AUTO", "0")
    ok, skipped, failed = 0, [], []
    for name in sorted(_modules()):
        if name == "agentic_os._platform._win32" and os.name != "nt":
            continue                                                               # the Windows API module imports msvcrt by design
        try:
            importlib.import_module(name)
            ok += 1
        except ModuleNotFoundError as e:
            if (e.name or "").split(".")[0] in OPTIONAL_IMPORTS:
                skipped.append(f"{name} (optional: {e.name})")
            else:
                failed.append(f"{name}: {type(e).__name__}: {e}")
        except BaseException as e:  # noqa: BLE001 - an import must not raise anything
            failed.append(f"{name}: {type(e).__name__}: {e}")
    for line in failed:
        print("IMPORT FAILED", line)
    print(f"native_core: imported {ok} modules, skipped {len(skipped)} (optional dependency absent), failed {len(failed)}")
    for line in skipped:
        print("  skipped", line)
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--check"]:
        return check()
    if argv[:1] == ["--write"]:
        MANIFEST.write_text(json.dumps(build(), indent=1) + "\n", encoding="utf-8", newline="\n")
        return 0
    if argv[:1] == ["--run"]:
        return run(argv[1:])
    if argv[:1] == ["--import-all"]:
        return import_all()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

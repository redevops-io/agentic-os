"""knowledge-frontier is shipped but not exercised by the sidecar: prove nothing in the serve/bootstrap import
closure needs it, and that the PyInstaller spec leaves it out (decision D-LIC-14).

1. AST: only its two re-export shims import it.
2. Blocked-import run: bootstrap, `sidecar_main`'s serve import and the served API (UI + routes) all work while
   any import of `knowledge_frontier` / `agentic_os.knowledge_frontier*` raises.
3. Spec: executed with a stubbed PyInstaller, it removes the modules from the hidden imports and lists them in
   `excludes`.
4. Frozen (opt-in): REDEVOPS_FROZEN_SIDECAR=<path to the built redevops-sidecar> runs the real binary.
"""
from __future__ import annotations

import ast
import json
import os
import pkgutil
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "agentic_os"
SHIMS = {PKG / "knowledge_frontier.py", PKG / "knowledge_frontier_backtest.py"}
TARGETS = ("knowledge_frontier", "agentic_os.knowledge_frontier", "agentic_os.knowledge_frontier_backtest")


def _imports_target(tree: ast.AST, module_name: str) -> list[str]:
    hits = []
    pkg_parts = module_name.split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:                       # relative import: resolve against this module's package
                anchor = pkg_parts[: len(pkg_parts) - node.level + 1]
                base = ".".join(anchor + ([node.module] if node.module else []))
            names = [base] + [f"{base}.{a.name}" if base else a.name for a in node.names]
        else:
            continue
        hits += [n for n in names if n == "knowledge_frontier" or n.startswith("knowledge_frontier.")
                 or n.startswith("agentic_os.knowledge_frontier")]
    return hits


def test_only_the_two_shims_import_knowledge_frontier():
    offenders = {}
    for path in sorted(PKG.rglob("*.py")):
        if path in SHIMS:
            continue
        mod = ".".join(path.relative_to(ROOT).with_suffix("").parts)
        hits = _imports_target(ast.parse(path.read_text(encoding="utf-8")), mod)
        if hits:
            offenders[str(path.relative_to(ROOT))] = hits
    assert offenders == {}, offenders
    # and nothing builds the name dynamically
    for path in sorted(PKG.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        assert not ("import_module" in src and "knowledge_frontier" in src), path


_BLOCKED_RUN = textwrap.dedent('''
    import importlib.abc, runpy, sys

    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name == "knowledge_frontier" or name.startswith(("knowledge_frontier.", "agentic_os.knowledge_frontier")):
                raise ImportError("BLOCKED import of " + name)
    sys.meta_path.insert(0, Block())

    # serve closure: what packaging/sidecar_main.py imports for `serve`, then the served app
    from agentic_os.projects_api import main as serve, SampleProjectionProvider, create_app
    from fastapi.testclient import TestClient
    c = TestClient(create_app(SampleProjectionProvider()))
    codes = {p: c.get(p).status_code for p in
             ("/", "/api/projects", "/api/projects/customer-ops/overview", "/api/projects/customer-ops/apps",
              "/api/projects/customer-ops/missions", "/hero.jpg")}
    guides = c.get("/api/projects/customer-ops/apps").json()

    # bootstrap closure: `sidecar_main bootstrap` runs agentic_os.mission.bootstrap as __main__
    sys.argv = ["agentic_os.mission.bootstrap"]
    try:
        runpy.run_module("agentic_os.mission.bootstrap", run_name="__main__")
        rc = 0
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    loaded = sorted(m for m in sys.modules if m.startswith(("knowledge_frontier", "agentic_os.knowledge_frontier")))
    import json
    print("RESULT " + json.dumps({"codes": codes, "guides": len(guides), "bootstrap_rc": rc, "loaded": loaded}))
''')


def test_serve_and_bootstrap_run_with_knowledge_frontier_imports_blocked():
    env = {k: v for k, v in os.environ.items() if k not in ("APPROVAL_SECRET", "SSH_AUTH_SOCK", "SSH_AGENT_PID")}
    env["PYTHONPATH"] = str(ROOT)
    p = subprocess.run([sys.executable, "-c", _BLOCKED_RUN], capture_output=True, text=True, env=env, timeout=120,
                       cwd=ROOT)
    assert "BLOCKED import" not in p.stderr + p.stdout, p.stderr[-2000:]
    line = next((l for l in p.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, (p.stdout[-1500:], p.stderr[-1500:])
    res = json.loads(line[len("RESULT "):])
    assert all(code == 200 for code in res["codes"].values()), res
    assert res["guides"] >= 1 and res["bootstrap_rc"] in (0, 1, 2)     # the exit code is the device verdict
    assert res["loaded"] == []


def _run_spec():
    """Execute the PyInstaller spec with stubs; return (Analysis kwargs, hidden imports passed in)."""
    captured: dict = {}

    def collect_submodules(name):
        if name == "agentic_os":     # names only: walk the tree without importing it
            return ["agentic_os"] + [m.name for m in pkgutil.walk_packages([str(PKG)], "agentic_os.")]
        return [name]

    class Analysis:
        def __init__(self, scripts, **kw):
            captured.update(kw, scripts=scripts)
            self.pure = []; self.scripts = []; self.binaries = []; self.datas = kw.get("datas", [])

    stub = types.ModuleType("PyInstaller.utils.hooks")
    stub.collect_submodules = collect_submodules
    stub.collect_data_files = lambda name: []
    pkgs = {"PyInstaller": types.ModuleType("PyInstaller"), "PyInstaller.utils": types.ModuleType("PyInstaller.utils"),
            "PyInstaller.utils.hooks": stub}
    saved = {k: sys.modules.get(k) for k in pkgs}
    sys.modules.update(pkgs)
    try:
        code = Path(os.environ.get("SIDECAR_SPEC") or ROOT / "packaging" / "redevops-sidecar.spec").read_text(
            encoding="utf-8")
        exec(compile(code, "redevops-sidecar.spec", "exec"),
             {"Analysis": Analysis, "PYZ": lambda *a, **k: None, "EXE": lambda *a, **k: None, "__name__": "spec"})
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return captured


def test_spec_excludes_knowledge_frontier_and_keeps_everything_else():
    a = _run_spec()
    assert not set(TARGETS) & set(a["hiddenimports"])
    assert set(TARGETS) <= set(a["excludes"])
    # the rest of the closure is still collected, including the modules that serve/bootstrap import
    for needed in ("agentic_os.projects_api", "agentic_os.mission.bootstrap", "agentic_os.stack_knowledge",
                   "uvicorn.loops.auto"):
        assert needed in a["hiddenimports"], needed
    assert {"tkinter", "redevops_rag", "torch"} <= set(a["excludes"])      # earlier excludes kept


def test_spec_embeds_licences_only_from_a_complete_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("REDEVOPS_LICENSES_DIR", str(tmp_path))
    with pytest.raises(AssertionError, match="lacks LICENSE"):
        _run_spec()
    for n in ("LICENSE", "LICENSE-COMMONS-CLAUSE", "LICENSE.md", "LICENSING.md", "THIRD_PARTY_NOTICES.txt"):
        (tmp_path / n).write_text("x")
    assert (str(tmp_path), "Licenses") in _run_spec()["datas"]


@pytest.mark.skipif(not os.environ.get("REDEVOPS_FROZEN_SIDECAR"),
                    reason="set REDEVOPS_FROZEN_SIDECAR to a built redevops-sidecar to run the frozen check")
def test_frozen_sidecar_has_no_knowledge_frontier_and_still_serves(tmp_path):
    import time
    import urllib.error
    import urllib.request
    binary = os.environ["REDEVOPS_FROZEN_SIDECAR"]
    # the archive's module table names every frozen module: knowledge_frontier must not be in it
    assert b"knowledge_frontier" not in Path(binary).read_bytes()
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    boot = subprocess.run([binary, "bootstrap"], capture_output=True, text=True, env=env, timeout=120, cwd=tmp_path)
    assert "Traceback" not in boot.stdout + boot.stderr and "device readiness" in boot.stdout
    import re
    log = tmp_path / "serve.log"
    with open(log, "wb") as lf:
        # PROJECTS_PORT=0: the sidecar picks the port and prints the one-time URL (docs/LOCAL_API_SECURITY.md 5);
        # the API needs the session that code buys, the UI files do not.
        srv = subprocess.Popen([binary, "serve"], env={**env, "PROJECTS_PORT": "0"}, cwd=tmp_path,
                               stdout=lf, stderr=subprocess.STDOUT)
    try:
        m = None
        for _ in range(120):
            m = re.search(r"http://127\.0\.0\.1:(\d+)/#lc=([A-Za-z0-9_-]+)", log.read_text(errors="replace"))
            if m:
                break
            time.sleep(0.5)
        assert m, "the sidecar did not print its one-time URL"
        port, code = int(m.group(1)), m.group(2)
        base = f"http://127.0.0.1:{port}"
        assert urllib.request.urlopen(f"{base}/", timeout=5).status == 200
        assert urllib.request.urlopen(f"{base}/hero.jpg", timeout=5).status == 200
        try:
            urllib.request.urlopen(f"{base}/api/projects", timeout=5)
            raise AssertionError("/api/projects must need a session")
        except urllib.error.HTTPError as e:
            assert e.code == 401
        req = urllib.request.Request(f"{base}/api/local/session", data=json.dumps({"code": code}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        resp = urllib.request.urlopen(req, timeout=5)
        cookie = resp.headers["Set-Cookie"].split(";")[0]
        get = lambda path: urllib.request.urlopen(urllib.request.Request(f"{base}{path}", headers={"Cookie": cookie}), timeout=5)
        for path in ("/api/projects", "/api/projects/customer-ops/apps"):
            assert get(path).status == 200
        guides = json.load(get("/api/projects/customer-ops/apps"))
        assert len(guides) == 14
    finally:
        srv.terminate()
        srv.wait(timeout=20)
    out = log.read_text(errors="replace")
    assert "Traceback" not in out and "ModuleNotFoundError" not in out

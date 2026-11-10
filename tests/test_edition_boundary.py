"""The edition boundary (docs/EDITION_BOUNDARY.md) as checks that run in CI.

They work from names, hashes and symbols: they cannot read what a real enterprise adapter does. What they pin:
* no module outside a reviewed allow-list names an enterprise package, and every such import is lazy (inside a function);
* no module or top-level definition uses enterprise vocabulary (SSO, OIDC, SAML, SCIM, JWKS, ABAC, break-glass, tenant provisioning);
* no plug-in entry-point group and no enterprise dependency is declared, and the kernel reads no entry points;
* decision B3: a permissions Grant carries no tenant or revocation epoch and authorize() takes no tenant;
* decision B1: the legacy single-deployment tenant surface is exactly the catalogued one;
* the catalogue names tests that exist, and the document carries the licence wording rule.
"""
from __future__ import annotations

import ast
import inspect
import json
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CATALOGUE = json.loads((ROOT / "docs" / "edition_boundary.json").read_text())
DOC = (ROOT / "docs" / "EDITION_BOUNDARY.md").read_text()
ENTERPRISE = tuple(CATALOGUE["enterprise_packages"])
ALLOW = set(CATALOGUE["import_allowlist"])
VOCAB = re.compile(r"(?i)(^|_)(" + "|".join(CATALOGUE["enterprise_vocabulary"]) + r"\w*)(_|$)")


def source_files():
    for base in ("agentic_os", "apps"):
        for p in sorted((ROOT / base).rglob("*.py")):
            rel = p.relative_to(ROOT).as_posix()
            name = p.name
            if name.startswith("test_") or name in ("conftest.py",) or "/tests/" in rel or "/node_modules/" in rel:
                continue
            yield rel, p


def is_enterprise(name: str) -> bool:
    return any(name == e or name.startswith(e + ".") for e in ENTERPRISE)


def enterprise_uses(tree):
    """(lineno, in_function, how) for each import or import_module/__import__ call that names an enterprise package."""
    out = []

    def walk(node, in_fn):
        for child in ast.iter_child_nodes(node):
            fn = in_fn or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            if isinstance(child, ast.Import):
                out.extend((child.lineno, fn, "import") for a in child.names if is_enterprise(a.name))
            elif isinstance(child, ast.ImportFrom) and child.module and is_enterprise(child.module):
                out.append((child.lineno, fn, "from"))
            elif isinstance(child, ast.Call):
                f = child.func
                nm = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                if nm in ("import_module", "__import__") and child.args and isinstance(child.args[0], ast.Constant) \
                        and isinstance(child.args[0].value, str) and is_enterprise(child.args[0].value):
                    out.append((child.lineno, fn, nm))
            walk(child, fn)

    walk(tree, False)
    return out


def test_only_reviewed_modules_name_an_enterprise_package_and_only_lazily():
    seen = {}
    for rel, p in source_files():
        uses = enterprise_uses(ast.parse(p.read_text(encoding="utf-8")))
        if uses:
            seen[rel] = uses
    assert set(seen) <= ALLOW, f"unreviewed enterprise import in {sorted(set(seen) - ALLOW)}"
    for rel, uses in seen.items():
        assert all(in_fn for _ln, in_fn, _how in uses), f"{rel}: an enterprise import runs at import time"


def test_the_scanner_itself_sees_a_module_level_and_a_lazy_import():
    t = ast.parse("import context_runtime_enterprise\ndef f():\n    from runtime_security_enterprise.wiring import x\n"
                  "    importlib.import_module('credentials_enterprise.apps')\nimport json\n")
    assert [(u[1], u[2]) for u in enterprise_uses(t)] == [(False, "import"), (True, "from"), (True, "import_module")]


def test_no_module_or_top_level_definition_uses_enterprise_vocabulary():
    allowed = set(CATALOGUE["vocabulary_exceptions"])
    found = []
    for rel, p in source_files():
        if VOCAB.search(p.stem):
            found.append((rel, "file"))
        for node in ast.parse(p.read_text(encoding="utf-8")).body:
            names = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names = [node.name]
            elif isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            found += [(rel, n) for n in names if VOCAB.search(n)]
    assert [f for f in found if f"{f[0]}::{f[1]}" not in allowed] == []


def test_no_entry_point_group_and_no_enterprise_dependency():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = meta["project"]
    assert "entry-points" not in project and "gui-scripts" not in project
    deps = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        deps += extra
    assert not [d for d in deps if any(e.replace("_", "-") in d.lower().replace("_", "-") for e in ENTERPRISE)]
    for rel, p in source_files():
        t = p.read_text(encoding="utf-8")
        assert not re.search(r"\bentry_points\s*\(", t), f"{rel} reads plug-in entry points"


def test_grants_and_authorize_carry_no_tenant_or_epoch_b3():
    from dataclasses import fields

    from agentic_os import permissions

    names = {f.name for f in fields(permissions.Grant)}
    assert not {n for n in names if "tenant" in n or "epoch" in n}, names
    assert "tenant" not in inspect.signature(permissions.PermissionsPlane.authorize).parameters
    assert not {n for n in {f.name for f in fields(permissions.Identity)} if "tenant" in n or "epoch" in n}


def test_the_legacy_tenant_surface_is_exactly_the_catalogued_one_b1():
    pat = re.compile(r"AGENTIC_OS_(?:[A-Z_]*TENANT[A-Z_]*)")
    modules, variables = set(), set()
    for rel, p in source_files():
        hits = set(pat.findall(p.read_text(encoding="utf-8")))
        if hits:
            modules.add(rel)
            variables |= hits
    legacy = CATALOGUE["legacy_tenant_surface"]
    assert sorted(variables) == sorted(legacy["env_variables"]) and sorted(modules) == sorted(legacy["modules"]), \
        "the tenant surface changed: update docs/edition_boundary.json and docs/EDITION_BOUNDARY.md together"


def _test_exists(ref: str) -> bool:
    path, _, node = ref.partition("::")
    f = ROOT / path
    return f.is_file() and (not node or re.search(rf"def {re.escape(node)}\b", f.read_text()) is not None)


def test_every_catalogued_test_exists():
    refs = [e["test"] for e in CATALOGUE["community"] + CATALOGUE["enterprise_only"]]
    assert refs and [r for r in refs if not _test_exists(r)] == []


def test_the_document_records_the_decisions_and_the_licence_wording():
    for key in ("B1", "B2", "B3"):
        assert key in CATALOGUE["decisions"] and f"| {key} |" in DOC
    assert "source-available" in DOC
    assert not re.search(r"(?<!not )open[- ]source", DOC.replace("not open source", ""))
    for phrase in ("Distributed tenant identity", "permission-aware retrieval", "cross-host tenant authorization"):
        assert phrase in DOC or phrase.lower() in DOC.lower()


@pytest.mark.parametrize("rel", ["agentic_os/permissions.py"])
def test_the_permissions_plane_does_not_import_an_enterprise_package_at_all(rel):
    assert enterprise_uses(ast.parse((ROOT / rel).read_text())) == []

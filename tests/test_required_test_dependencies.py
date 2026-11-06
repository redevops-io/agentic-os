"""Required test dependencies must be present, and a missing one must FAIL a test, never skip it (convergence stage 4).

Owner decisions 2026-11-06: ``jsonschema`` is a REQUIRED test dependency and the contract tests must never silently skip; the suite runs
against the pinned DuckDB 1.5.5. This file pins the declaration (pyproject ``[dev]`` extra, the CI install line), the installed versions
and, statically, that no test skips on a missing REQUIRED module (``importorskip``, ``find_spec``/``ImportError`` -> skip).
Optional integrations (psycopg, playwright, agent_harness, redevops_connectors, fastmcp via the CI-installed mcp extra, node, helm, docker) may
still skip with a stated reason; they are listed in OPTIONAL so a new one is a conscious decision.
"""
from __future__ import annotations

import importlib
import importlib.metadata as md
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

#: importable module -> distribution. Absent => the suite is broken, not "skipped".
REQUIRED = {"jsonschema": "jsonschema", "duckdb": "duckdb", "yaml": "pyyaml", "httpx": "httpx", "fastapi": "fastapi",
            "uvicorn": "uvicorn", "cryptography": "cryptography", "pydantic": "pydantic", "runtime_contracts": "runtime-contracts"}
#: modules a test may legitimately skip on (optional extras / external tooling), each with the reason it is optional.
OPTIONAL = {"psycopg": "postgres extra; needs a server", "playwright": "browser tests; opt-in", "agent_harness": "external repository",
            "reel": "app-local package", "redevops_connectors": "projects/connectors extra", "fastmcp": "mcp extra; CI checks it separately",
            "credentials_enterprise": "private enterprise plane", "billing": "an app: needs apps/ on the path"}


@pytest.mark.parametrize("module", sorted(REQUIRED))
def test_required_test_dependency_is_installed(module):
    importlib.import_module(module)                     # ImportError here is a test FAILURE


def test_duckdb_is_the_pinned_version():
    assert md.version("duckdb") == "1.5.5", "the suite runs against the pinned DuckDB 1.5.5 (owner decision 2026-11-06)"


def test_jsonschema_is_new_enough_for_draft_2020_12():
    major, minor = (int(x) for x in md.version("jsonschema").split(".")[:2])
    assert (major, minor) >= (4, 18) and major < 5


def test_pyproject_declares_them_in_the_dev_extra():
    dev = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]["dev"]
    names = {re.split(r"[<>=!~ ]", d, 1)[0].lower() for d in dev}
    assert {"pytest", "jsonschema", "duckdb"} <= names, dev
    assert "duckdb==1.5.5" in dev


def test_ci_installs_the_dev_extra():
    wf = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    assert re.search(r'pip install -e "\.\[[^\]]*\bdev\b[^\]]*\]"', wf)
    assert "must run, not skip" in wf and "grep -q \"SKIPPED\"" in wf


def _py_tests():
    for p in sorted(TESTS.rglob("*.py")):
        if p.name != Path(__file__).name:
            yield p


def test_no_test_skips_on_a_missing_required_module():
    offenders = []
    for p in _py_tests():
        text = p.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r'importorskip\(\s*["\']([\w.]+)', text):
            top = m.group(1).split(".")[0]
            if top in REQUIRED:
                offenders.append(f"{p.relative_to(ROOT)}: importorskip({m.group(1)!r})")
            elif top not in OPTIONAL:
                offenders.append(f"{p.relative_to(ROOT)}: importorskip({m.group(1)!r}) is neither REQUIRED nor a listed OPTIONAL module")
        for m in re.finditer(r'find_spec\(\s*["\']([\w.]+)["\']\s*\)\s*is None', text):
            top = m.group(1).split(".")[0]
            if top in REQUIRED:
                offenders.append(f"{p.relative_to(ROOT)}: skip on find_spec({m.group(1)!r}) is None")
    assert offenders == [], offenders

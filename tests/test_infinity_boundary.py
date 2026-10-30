"""The optional Infinity RAP bridge (integrations/infinity) must not change the base install.

* the kernel and the Agent Gateway import without the bridge, without any Infinity, AWS, Bedrock, DSQL or CDK module;
* the root dependency set neither depends on nor offers an extra for the bridge, the wheel still contains only agentic_os, and the root lock has no trace of it;
* nothing under agentic_os/ refers to the optional package;
* the optional package depends on agentic-os and nothing else at run time, imports no cloud SDK, and its Rust harness is pinned to exact versions with checksums.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "integrations" / "infinity"
FORBIDDEN_IN_BASE = ("redevops_infinity", "boto3", "botocore", "aws_cdk", "aiobotocore", "anthropic", "openai", "infinity_agent_core")
CLOUD = re.compile(r"^\s*(?:import|from)\s+(boto3|botocore|aws_cdk|aiobotocore|aws_lambda_powertools|anthropic|openai|psycopg|asyncpg)\b", re.M)


def test_the_kernel_and_the_gateway_import_without_the_bridge_or_any_cloud_sdk():
    code = (
        "import sys, json\n"
        "import agentic_os\n"
        "import agentic_os.agent_gateway\n"
        "import agentic_os.agent_gateway.write_capabilities\n"
        "import agentic_os.mission.authority\n"
        "import agentic_os.mission.children\n"
        "import agentic_os.mission.store\n"
        "print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))\n")
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert out.returncode == 0, out.stderr[-2000:]
    loaded = set(json.loads(out.stdout.strip().splitlines()[-1]))
    assert not (loaded & set(FORBIDDEN_IN_BASE)), sorted(loaded & set(FORBIDDEN_IN_BASE))


def test_the_root_dependency_set_does_not_mention_the_bridge():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    reqs = list(meta["project"]["dependencies"])
    for extra, items in meta["project"].get("optional-dependencies", {}).items():
        assert not re.search(r"infinity|rap", extra), extra
        reqs += list(items)
    assert not [r for r in reqs if re.search(r"agentic-os-infinity|redevops-infinity|infinity-agent|rap-client", r, re.I)]
    assert meta["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["agentic_os"]


def test_the_root_lock_has_no_trace_of_the_bridge():
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8").lower()
    assert "agentic-os-infinity" not in lock and "redevops-infinity" not in lock


def test_no_kernel_source_refers_to_the_optional_package():
    hits = []
    for p in (ROOT / "agentic_os").rglob("*.py"):
        if re.search(r"redevops_infinity|agentic-os-infinity", p.read_text(encoding="utf-8", errors="replace")):
            hits.append(str(p.relative_to(ROOT)))
    assert hits == []


def test_the_package_depends_on_agentic_os_alone_and_imports_no_cloud_sdk():
    meta = tomllib.loads((PKG / "pyproject.toml").read_text(encoding="utf-8"))
    deps = meta["project"]["dependencies"]
    assert len(deps) == 1 and deps[0].startswith("agentic-os>=0.4.1"), deps
    bad = []
    for p in (PKG / "src").rglob("*.py"):
        if CLOUD.search(p.read_text(encoding="utf-8")):
            bad.append(str(p.relative_to(PKG)))
    assert bad == []


def test_importing_the_package_loads_no_cloud_sdk():
    code = "import sys, json, redevops_infinity; print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))"
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert out.returncode == 0, out.stderr[-2000:]
    loaded = set(json.loads(out.stdout.strip().splitlines()[-1]))
    assert not (loaded & {"boto3", "botocore", "aws_cdk", "anthropic", "openai", "infinity_agent_core"}), sorted(loaded)


def test_the_rust_harness_is_pinned_to_exact_versions_and_its_lock_carries_checksums():
    cargo = tomllib.loads((PKG / "harness" / "Cargo.toml").read_text(encoding="utf-8"))
    for name in ("infinity-agent-core", "infinity-provider-protocol", "infinity-rap-bridge", "rap-client", "rap-protocol"):
        assert cargo["dependencies"][name] == "=0.2.0", name
    lock = tomllib.loads((PKG / "harness" / "Cargo.lock").read_text(encoding="utf-8"))
    registry = [p for p in lock["package"] if str(p.get("source", "")).startswith("registry+")]
    assert registry and all(re.fullmatch(r"[0-9a-f]{64}", p.get("checksum", "")) for p in registry)
    by = {p["name"]: p for p in lock["package"]}
    assert by["infinity-agent-core"]["version"] == "0.2.0"

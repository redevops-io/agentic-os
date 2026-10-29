"""The optional voice integration (integrations/voice-pipecat) must not change the base install.

* The base package imports no voice framework: a clean interpreter that imports the kernel and the Agent Gateway has none of
  pipecat, the voice package or the heavy libraries pipecat pulls in (numpy, numba, onnxruntime, openai) in sys.modules.
* The base dependency set is unchanged: the root pyproject.toml neither depends on nor offers an extra that installs the voice
  packages, the wheel still contains only agentic_os, and the root lock has no pipecat.
* Nothing under agentic_os/ refers to the optional package.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VOICE = ROOT / "integrations" / "voice-pipecat"
FORBIDDEN = ("pipecat", "agentic_os_voice", "numba", "onnxruntime", "openai", "llvmlite")


def test_the_kernel_and_the_gateway_import_without_any_voice_framework():
    code = (
        "import sys, json\n"
        "import agentic_os\n"
        "import agentic_os.agent_gateway\n"
        "import agentic_os.agent_gateway.write_capabilities\n"
        "import agentic_os.mission.authority\n"
        "import agentic_os.mission.store\n"
        "print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))\n")
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert out.returncode == 0, out.stderr[-2000:]
    loaded = set(json.loads(out.stdout.strip().splitlines()[-1]))
    assert not (loaded & set(FORBIDDEN)), sorted(loaded & set(FORBIDDEN))


def test_the_root_dependency_set_does_not_mention_the_voice_packages():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    reqs = list(meta["project"]["dependencies"])
    for extra, items in meta["project"].get("optional-dependencies", {}).items():
        assert not re.search(r"voice|pipecat", extra), extra
        reqs += list(items)
    assert not [r for r in reqs if re.search(r"pipecat|agentic-os-voice", r, re.I)]
    assert meta["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["agentic_os"]


def test_the_root_lock_has_no_pipecat():
    assert "pipecat" not in (ROOT / "uv.lock").read_text(encoding="utf-8").lower()


def test_no_kernel_source_refers_to_the_optional_package():
    hits = []
    for p in (ROOT / "agentic_os").rglob("*.py"):
        text = p.read_text(encoding="utf-8", errors="replace")
        if re.search(r"agentic_os_voice|import pipecat|from pipecat", text):
            hits.append(str(p.relative_to(ROOT)))
    assert hits == []


def test_the_optional_package_pins_pipecat_exactly_and_its_lock_agrees():
    meta = tomllib.loads((VOICE / "pyproject.toml").read_text(encoding="utf-8"))
    pin = next(d for d in meta["project"]["dependencies"] if d.startswith("pipecat-ai"))
    assert re.fullmatch(r"pipecat-ai==\d+\.\d+\.\d+", pin), pin
    lock = tomllib.loads((VOICE / "uv.lock").read_text(encoding="utf-8"))
    locked = {p["name"]: p for p in lock["package"]}
    assert f"pipecat-ai=={locked['pipecat-ai']['version']}" == pin
    assert locked["pipecat-ai"]["sdist"]["hash"].startswith("sha256:") and locked["pipecat-ai"]["wheels"][0]["hash"].startswith("sha256:")

"""trusted_import inside the PyInstaller runtime (D-NEW-3): planted packages stay untrusted and never run.

Opt-in: build the probe with packaging/probes/build-trusted-import-probe.sh (the sidecar's own spec, a different entry script) and set
REDEVOPS_FROZEN_TRUSTED_IMPORT_PROBE to it. The same scenarios run unfrozen in tests/test_trusted_import_frozen_safe.py,
tests/test_bb01_forged_dist_info.py and tests/test_dr02_enterprise_import_trust.py.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(not os.environ.get("REDEVOPS_FROZEN_TRUSTED_IMPORT_PROBE"),
                                reason="set REDEVOPS_FROZEN_TRUSTED_IMPORT_PROBE to a built redevops-trusted-import-probe")


def _run(tmp_path):
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "TMPDIR": str(tmp_path)}
    p = subprocess.run([os.environ["REDEVOPS_FROZEN_TRUSTED_IMPORT_PROBE"], str(tmp_path / "w")], capture_output=True, text=True, env=env,
                       timeout=180, cwd=tmp_path)
    return p, json.loads(p.stdout)


def test_planted_packages_are_untrusted_and_never_executed_in_the_frozen_runtime(tmp_path):
    p, r = _run(tmp_path)
    assert p.returncode == 0, p.stderr
    assert r["frozen"] is True
    assert set(r["planted_status"]) == {"context_runtime_enterprise", "context_runtime", "agent_harness"}
    assert all(s in ("untrusted", "absent") for s in r["planted_status"].values()), r["planted_status"]
    assert all(r["planted_import_refused"].values())
    assert r["planted_code_ran"] == []


def test_the_frozen_runtime_has_no_site_module_and_the_probe_still_decides(tmp_path):
    p, r = _run(tmp_path)
    assert r["site_loaded"] is False                 # excluded by the spec: the guarded import fell back to sysconfig
    assert r["ok"] is True
    assert "Traceback" not in p.stderr

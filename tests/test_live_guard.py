"""Live tests are opt-in: an ambient credential alone never triggers a live call (conftest.py).

Runs a throwaway pytest session in a temp directory with a copy of tests/conftest.py, a fake credential in the
environment and a test marked ``live``; nothing here touches a network.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests._proc_support import child_env

ROOT = Path(__file__).resolve().parent.parent

SAMPLE = '''
import os
import pytest

def test_the_ambient_credential_is_hidden():
    assert os.environ.get("FAKE_PROVIDER_API_KEY") is None
    assert os.environ.get("FAKE_BOT_TOKEN") is None
    assert os.environ.get("SSH_AUTH_SOCK") is None
    assert os.environ.get("HOME") is not None          # only credential-looking names are hidden

@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("FAKE_PROVIDER_API_KEY"), reason="needs a key")
def test_live_call():
    raise AssertionError("a live call would happen here")
'''

SAMPLE_ALLOWED = '''
import os
import pytest

@pytest.mark.live
def test_live_call_runs_when_allowed():
    assert os.environ.get("FAKE_PROVIDER_API_KEY") == "k-not-a-real-key"
'''


def run(tmp: Path, sample: str, allow: bool):
    (tmp / "conftest.py").write_text((ROOT / "tests" / "conftest.py").read_text())
    (tmp / "test_sample.py").write_text(sample)
    # the child's conftest copy imports agentic_os (a fixture): give it THIS repository explicitly (the session guard in tests/conftest.py separately checks that the ambient install is this repository's)
    env = {k: v for k, v in child_env().items() if not k.startswith(("REDEVOPS_ALLOW", "PYTEST"))}
    env.update(FAKE_PROVIDER_API_KEY="k-not-a-real-key", FAKE_BOT_TOKEN="t-not-real", SSH_AUTH_SOCK="/tmp/not-a-socket")
    if allow:
        env["REDEVOPS_ALLOW_LIVE_TESTS"] = "1"
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rs", str(tmp)],
                          capture_output=True, text=True, env=env, cwd=tmp)


def test_without_the_opt_in_the_credential_is_hidden_and_the_live_test_is_skipped(tmp_path):
    r = run(tmp_path, SAMPLE, allow=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 passed, 1 skipped" in r.stdout, r.stdout
    assert "live tests: disabled" in r.stdout and "k-not-a-real-key" not in r.stdout + r.stderr


def test_with_the_opt_in_the_live_test_runs_and_sees_the_credential(tmp_path):
    r = run(tmp_path, SAMPLE_ALLOWED, allow=True)
    assert r.returncode == 0 and "1 passed" in r.stdout, r.stdout + r.stderr
    assert "live tests: ENABLED" in r.stdout


def test_the_known_live_tests_carry_the_marker():
    for rel, needle in [("tests/test_providers_grok.py", "XAI_API_KEY"), ("tests/test_channels_telegram.py", "REDEVOPS_BOT_TOKEN"),
                        ("tests/test_event_store_postgres.py", "RDO_PROXMOX_IT"), ("tests/test_installer_proxmox.py", "RDO_PROXMOX_IT")]:
        text = (ROOT / rel).read_text()
        assert "pytest.mark.live" in text and needle in text, rel

"""The demo deploy scripts take the target host from configuration and stop clearly when it is missing.

Where a deployment goes is not source (docs/DEPLOYMENT_ADDRESSES.md): each `deploy/*/deploy.sh` reads `<PREFIX>_HOST`
from the environment or from the gitignored `deploy/deploy.local.env`, and refuses to run while it is empty or still
`REPLACE_ME`. The scripts are exercised in a scratch copy of the tree (so a developer's own `deploy.local.env` cannot
change the result), with a minimal environment, and only up to the first check: nothing connects anywhere.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")

# directory -> (host variable, needs RUNTIME_CONTRACTS)
SCRIPTS = {
    "admin-console": ("AD_HOST", True),
    "attention-console": ("AT_HOST", True),
    "evidence-explorer-demo": ("EV_HOST", True),
    "permissions-showcase-demo": ("PERM_HOST", False),
    "refcore-billing": ("RC_HOST", False),
    "security-showcase-demo": ("SEC_HOST", True),
    "unsubscribe-service": ("U_HOST", True),
    "world-canvas-demo": ("WC_HOST", True),
}


@pytest.fixture()
def tree(tmp_path):
    (tmp_path / "deploy" / "lib").mkdir(parents=True)
    shutil.copy(ROOT / "deploy" / "lib" / "deploy-env.sh", tmp_path / "deploy" / "lib" / "deploy-env.sh")
    for name in SCRIPTS:
        (tmp_path / "deploy" / name).mkdir()
        shutil.copy(ROOT / "deploy" / name / "deploy.sh", tmp_path / "deploy" / name / "deploy.sh")
    return tmp_path


def _run(tree, name, env=None):
    clean = {"PATH": "/usr/bin:/bin", "HOME": str(tree)}
    clean.update(env or {})
    return subprocess.run([BASH, str(tree / "deploy" / name / "deploy.sh")], capture_output=True, text=True, timeout=30, env=clean,
                          cwd=str(tree))


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_script_has_valid_shell_syntax(name):
    r = subprocess.run([BASH, "-n", str(ROOT / "deploy" / name / "deploy.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_unset_host_stops_with_a_clear_message(tree, name):
    var, _ = SCRIPTS[name]
    r = _run(tree, name)
    assert r.returncode == 2
    assert f"{var} is not set" in r.stderr and "deploy.local.env" in r.stderr


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_placeholder_host_is_refused(tree, name):
    var, _ = SCRIPTS[name]
    r = _run(tree, name, {var: "REPLACE_ME"})
    assert r.returncode == 2 and f"{var} is not set" in r.stderr


@pytest.mark.parametrize("name", sorted(n for n, (_, rc) in SCRIPTS.items() if rc))
def test_runtime_contracts_checkout_is_required_and_there_is_no_default(tree, name):
    var, _ = SCRIPTS[name]
    r = _run(tree, name, {var: "REPLACE_ME"})
    assert "RUNTIME_CONTRACTS" not in r.stderr          # the host check comes first
    r = _run(tree, name, {var: "target.example.invalid", "RUNTIME_CONTRACTS": "REPLACE_ME"})
    assert r.returncode == 2 and "RUNTIME_CONTRACTS is not set" in r.stderr


def test_local_env_file_supplies_the_values(tree):
    (tree / "deploy" / "deploy.local.env").write_text("AD_HOST=REPLACE_ME\n", encoding="utf-8")
    r = _run(tree, "admin-console")
    assert r.returncode == 2 and "AD_HOST is not set" in r.stderr           # a placeholder in the file is still refused
    (tree / "deploy" / "deploy.local.env").write_text("AD_HOST=target.example.invalid\nRUNTIME_CONTRACTS=REPLACE_ME\n", encoding="utf-8")
    r = _run(tree, "admin-console")
    assert r.returncode == 2 and "RUNTIME_CONTRACTS is not set" in r.stderr  # the host was accepted from the file


def test_example_file_is_all_placeholders_and_the_real_one_is_ignored_by_git():
    example = (ROOT / "deploy" / "deploy.local.env.example").read_text(encoding="utf-8")
    for line in example.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            assert line.split("=", 1)[1].split("#", 1)[0].strip() == "REPLACE_ME", line
    assert "deploy/deploy.local.env" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

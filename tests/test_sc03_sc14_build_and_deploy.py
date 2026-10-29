"""SC-03: key material never enters a Docker build context or an image layer. SC-14: the showcase deploy script has an explicit key step and
does not rsync --delete away key/env files it did not create. (The script is only read and syntax-checked here; it is never run.)"""
from __future__ import annotations

import fnmatch
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "permissions-showcase-demo" / "deploy.sh"


def _patterns(path):
    return [l.strip() for l in path.read_text().splitlines() if l.strip() and not l.strip().startswith("#")]


def _ignored(rel: str, patterns) -> bool:
    """A small .dockerignore matcher: ``**/x`` matches at any depth, a bare name matches the path or any parent directory."""
    rel = rel.strip("/")
    parts = rel.split("/")
    for pat in patterns:
        pat = pat.rstrip("/")
        cands = [rel] + ["/".join(parts[:i]) for i in range(1, len(parts))]
        if pat.startswith("**/"):
            tail = pat[3:]
            if any(fnmatch.fnmatch("/".join(parts[i:]), tail) or fnmatch.fnmatch(parts[i], tail) for i in range(len(parts))):
                return True
        elif any(fnmatch.fnmatch(c, pat) for c in cands):
            return True
    return False


KEY_BEARING = ["permissions.env", "sub/dir/permissions.env", "prod.permissions.env", ".permissions.key", "x/y/.permissions.key",
               ".local/permissions/.permissions.key", ".local/permissions/grants.enc", ".local", "deploy/secret.key", ".env", "a/.env"]


class TestSC03TheDockerContextExcludesKeyMaterial:
    @pytest.mark.parametrize("rel", KEY_BEARING)
    def test_dockerignore_excludes_every_key_bearing_path(self, rel):
        assert _ignored(rel, _patterns(ROOT / ".dockerignore")), rel

    def test_ordinary_source_is_still_in_the_context(self):
        pats = _patterns(ROOT / ".dockerignore")
        for rel in ("agentic_os/permissions.py", "deploy/permissions-showcase-demo/app.py", "pyproject.toml", "Makefile"):
            assert not _ignored(rel, pats), rel

    def test_the_gitignore_and_dockerignore_agree_on_the_key_files(self):
        git = _patterns(ROOT / ".gitignore")
        for rel in ("permissions.env", ".permissions.key", ".local/permissions/.permissions.key"):
            assert _ignored(rel, git) and _ignored(rel, _patterns(ROOT / ".dockerignore")), rel

    def _make_target_path(self, **env):
        e = {k: v for k, v in os.environ.items() if k not in ("XDG_CONFIG_HOME", "PERMISSIONS_ENV")}
        e.update(env)
        out = subprocess.run(["make", "-n", "-C", str(ROOT), "permissions-key"], env=e, capture_output=True, text=True, timeout=60).stdout
        return re.search(r"--env-file (\S+)", out).group(1)

    def test_make_permissions_key_writes_outside_the_repo_by_default(self, tmp_path):
        p = self._make_target_path(HOME=str(tmp_path))
        assert p == f"{tmp_path}/.config/agentic-os/permissions.env" and not pathlib.Path(p).is_relative_to(ROOT)
        q = self._make_target_path(HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "xdg"))
        assert q == f"{tmp_path}/xdg/agentic-os/permissions.env"

    def test_the_default_location_is_not_in_the_build_context_even_for_a_repo_local_override(self):
        assert _ignored("permissions.env", _patterns(ROOT / ".dockerignore"))      # an override into the repo root is still excluded

    def test_the_key_command_creates_the_parent_directory_private(self, tmp_path):
        target = tmp_path / "a" / "b" / "permissions.env"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PERMISSIONS_", "APPROVAL_", "AGENTIC_OS_"))}
        env["PYTHONPATH"] = str(ROOT)
        r = subprocess.run([sys.executable, "-m", "agentic_os.cli", "permissions", "key", "--env-file", str(target)], env=env, cwd=str(ROOT),
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        assert stat.S_IMODE(os.stat(target.parent).st_mode) == 0o700 and stat.S_IMODE(os.stat(target.parent.parent).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o600

    def test_the_makefile_default_and_check_use_the_same_outside_path(self):
        mk = (ROOT / "Makefile").read_text()
        assert "PERMISSIONS_ENV ?=" in mk and ".config" in mk and "agentic-os/permissions.env" in mk


class TestSC14TheDeployScriptHasAKeyStep:
    text = SCRIPT.read_text()

    def test_it_is_valid_bash_and_is_not_run_here(self):
        assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0

    def test_rsync_delete_excludes_key_and_env_files_it_did_not_create(self):
        sync = [l for l in self.text.splitlines() if "rsync" in l and "--delete" in l]
        assert sync, "the sync line moved"
        for pat in ("permissions.env", ".permissions.key", ".env", "*.key"):
            assert f"--exclude '{pat}'" in sync[0], pat

    def test_a_missing_key_on_the_host_stops_the_deploy_with_the_fix(self):
        assert "permissions.env" in self.text and "PERMISSIONS_KEY=" in self.text
        assert re.search(r"no key|key is not provisioned|not provisioned", self.text, re.I) and "--init-key" in self.text
        assert "exit 1" in self.text

    def test_the_init_key_step_generates_on_the_host_and_never_prints_or_transfers_the_key(self):
        block = self.text[self.text.index("--init-key"):]
        assert "umask 077" in block and "os.urandom(32)" in block and "[ -e permissions.env ]" in block
        assert "echo $PERMISSIONS_KEY" not in self.text and "cat permissions.env" not in self.text and "scp" not in self.text

    def test_compose_is_started_with_the_env_file_and_the_key_location_is_documented(self):
        assert "--env-file permissions.env" in self.text
        assert "0600" in self.text and "$HOST_DIR/permissions.env" in self.text

    def test_the_post_deploy_checks_use_the_public_endpoints_not_the_admin_api(self):
        assert "/api/permissions/status" not in self.text.replace("ingress", "") or "api/permissions" in self.text.split("ingress:")[-1]
        assert "/api/demo/scenarios" in self.text and "api/demo" in self.text.split("ingress:")[-1]


class TestSC20TheCiHarnessIsPinned:
    def test_agent_harness_is_installed_from_a_commit_sha_not_a_moving_branch(self):
        text = (ROOT / ".github" / "workflows" / "permissions-demo-compat.yml").read_text()
        spec = re.search(r'agent-harness @ git\+https://github.com/redevops-io/agent-harness\.git(@\S+?)"', text)
        assert spec and re.fullmatch(r"@[0-9a-f]{40}", spec.group(1))

    def test_no_workflow_in_the_repo_installs_it_unpinned(self):
        for f in (ROOT / ".github" / "workflows").glob("*.yml"):
            for m in re.finditer(r"agent-harness\.git(\S*)", f.read_text()):
                assert re.match(r"@[0-9a-f]{40}", m.group(1)), (f.name, m.group(0))

"""The sibling licence-tag verifier passes on the prepared tags (when the workspace clones are present) and fails closed on anything else."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "release"))
import verify_sibling_licences as v  # noqa: E402

WORK = Path("/Users/arybach/qualification/0.6.0-rc1/xrepo/work-t1")


@pytest.mark.skipif(not (WORK / "connectors" / ".git").exists(), reason="the workspace clones of the sibling tags are not here")
def test_the_prepared_tags_pass_every_check():
    rows = v.check("redevops-connectors", WORK / "connectors") + v.check("knowledge-frontier", WORK / "kf")
    assert [t for s, t in rows if s == "FAIL"] == []


def test_a_repository_without_the_tags_fails_closed(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null"})
    rows = v.check("redevops-connectors", tmp_path)
    assert rows and all(s == "FAIL" for s, _ in rows)


def test_a_licence_commit_that_also_changes_code_is_refused(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    g = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True, env=env)
    g("init", "-q")
    (tmp_path / "pyproject.toml").write_text('version = "0.1.0"\nlicense = "AGPL-3.0-or-later"\n')
    (tmp_path / "code.py").write_text("x = 1\n")
    g("add", "-A"); g("commit", "-q", "-m", "base"); g("tag", "-a", "v0.1.0", "-m", "base")
    (tmp_path / "LICENSE").write_bytes((ROOT / "LICENSE").read_bytes())
    (tmp_path / "code.py").write_text("x = 2\n")
    (tmp_path / "pyproject.toml").write_text('version = "0.1.1"\nlicense = { text = "AGPL-3.0-or-later WITH Commons-Clause" }\n')
    g("add", "-A"); g("commit", "-q", "-m", "licence and code"); g("tag", "-a", "v0.1.1", "-m", "new")
    rows = v.check("redevops-connectors", tmp_path)
    assert any(s == "FAIL" and "no source, test or other file changed" in t for s, t in rows)
    assert any(s == "FAIL" and "LICENSE-COMMONS-CLAUSE" in t for s, t in rows)

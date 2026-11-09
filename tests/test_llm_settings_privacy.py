"""How the LLM settings file is judged private enough to steer model traffic (Windows has no mode bits).

Windows `os.stat().st_mode` reports 0o666 for an ordinary writable file, so the POSIX mode rule that used to
guard this file refused every settings file there and `load_llm_settings` fell back to the environment with no
signal at all. These tests pin the replacement: POSIX keeps its rule verbatim, Windows asks the ACL question
the secret store asks, and a refusal is never silent. The Windows branch is exercised here on any platform by
substituting the platform helpers, so the behaviour is pinned even where CI has no Windows host.
"""
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentic_os._platform import files as _files
from agentic_os.app import llm_settings as ls

POSIX = pytest.mark.skipif(not _files.HAS_POSIX_MODES, reason="POSIX mode bits do not exist on Windows")


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    for k in ("REDEVOPS_LLM_BASE_URL", "REDEVOPS_LLM_MODEL", "REDEVOPS_LLM_IN_BOUNDARY_HOSTS",
              ls.SETTINGS_FILE_ENV):
        monkeypatch.delenv(k, raising=False)
    path = tmp_path / "llm.json"
    ls.save_llm_settings(str(path), base_url="http://127.0.0.1:8000/v1", model="m1")
    monkeypatch.setenv(ls.SETTINGS_FILE_ENV, str(path))
    return path


def test_a_written_settings_file_is_accepted_on_every_platform(settings_file):
    """The regression this fixes: on Windows this returned the environment, not the file."""
    assert ls.settings_file_problem(str(settings_file)) is None
    assert ls.load_llm_settings().model == "m1"


def test_privacy_is_decided_by_the_platform_helper_not_by_the_caller():
    """The Windows decision is the same one the secret store makes, so the two cannot drift."""
    assert ls.settings_file_problem.__doc__ is not None
    assert hasattr(_files, "private_file_problem") and hasattr(_files, "HAS_POSIX_MODES")


def test_windows_asks_the_acl_question_even_when_no_windows_host_is_available(monkeypatch, settings_file):
    """Substitute the platform seam: `HAS_POSIX_MODES` is read at call time for exactly this reason."""
    calls = []

    def fake_problem(path):
        calls.append(str(path))
        return None

    monkeypatch.setattr(_files, "HAS_POSIX_MODES", False, raising=False)
    monkeypatch.setattr(_files, "private_file_problem", fake_problem)
    assert ls.settings_file_problem(str(settings_file)) is None
    assert calls == [str(settings_file)], "the Windows path must consult the ACL helper, not the mode bits"
    assert ls.load_llm_settings().model == "m1"


def test_windows_refusal_still_falls_back_to_the_environment_but_says_so(monkeypatch, settings_file, tmp_path):
    monkeypatch.setattr(_files, "HAS_POSIX_MODES", False, raising=False)
    monkeypatch.setattr(_files, "private_file_problem", lambda p: "is not under the user's profile directory")
    monkeypatch.setenv("REDEVOPS_LLM_MODEL", "from-environment")
    with pytest.warns(RuntimeWarning, match="is not under the user's profile directory"):
        got = ls.load_llm_settings()
    assert got.model == "from-environment"


@POSIX
def test_posix_rule_is_unchanged_group_or_world_writable_is_refused(settings_file):
    os.chmod(settings_file, 0o660)
    assert "lets others write it" in ls.settings_file_problem(str(settings_file))
    monkey_model = os.environ.get("REDEVOPS_LLM_MODEL")
    os.environ["REDEVOPS_LLM_MODEL"] = "from-environment"
    try:
        with pytest.warns(RuntimeWarning, match="which lets others write it"):
            got = ls.load_llm_settings()
    finally:
        os.environ.pop("REDEVOPS_LLM_MODEL", None)
        if monkey_model is not None:
            os.environ["REDEVOPS_LLM_MODEL"] = monkey_model
    assert got.model == "from-environment"


@POSIX
def test_posix_still_accepts_a_file_that_is_readable_but_not_writable(settings_file):
    """A 0644 file provisioned by a configuration manager worked before and must work after."""
    os.chmod(settings_file, 0o644)
    assert ls.settings_file_problem(str(settings_file)) is None
    assert ls.load_llm_settings().model == "m1"


@POSIX
def test_the_written_file_carries_the_owner_only_mode_on_posix(settings_file):
    assert stat.S_IMODE(os.stat(settings_file).st_mode) == 0o600

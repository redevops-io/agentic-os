"""Windows %USERPROFILE% -> %LOCALAPPDATA% state migration — additive, verified, idempotent, fail-closed.

Runs on any OS: the migration functions take plain paths and never branch on the real platform, so
these exercise the same logic a real Windows run uses, deterministically, without a Windows host.
"""
import os

from agentic_os.mission.windows_state_migration import (
    migrate_file_if_needed, migrate_if_needed, windows_app_data_root,
)


def test_windows_app_data_root_uses_localappdata_env():
    root = windows_app_data_root({"LOCALAPPDATA": "/fake/local"})
    assert str(root) == os.path.join("/fake/local", "ReDevOps")


def test_windows_app_data_root_falls_back_to_home_appdata_local(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    root = windows_app_data_root({})
    assert str(root).endswith(os.path.join("AppData", "Local", "ReDevOps"))


def test_migrate_dir_fresh_install_no_legacy_data_is_a_noop(tmp_path):
    legacy = tmp_path / "legacy"           # does not exist
    new = tmp_path / "new"
    assert migrate_if_needed(legacy, new) is False
    assert not new.exists()


def test_migrate_dir_with_legacy_data_copies_and_verifies(tmp_path):
    legacy = tmp_path / "legacy"
    (legacy / "default").mkdir(parents=True)
    (legacy / "default" / "api_key").write_bytes(b"super-secret-value")
    new = tmp_path / "new"

    assert migrate_if_needed(legacy, new) is True
    assert (new / "default" / "api_key").read_bytes() == b"super-secret-value"
    assert (new / ".migration-complete").exists()
    # legacy is preserved, never deleted
    assert (legacy / "default" / "api_key").read_bytes() == b"super-secret-value"


def test_migrate_dir_is_idempotent_second_call_is_noop(tmp_path):
    legacy = tmp_path / "legacy"
    (legacy / "default").mkdir(parents=True)
    (legacy / "default" / "api_key").write_bytes(b"value-1")
    new = tmp_path / "new"
    assert migrate_if_needed(legacy, new) is True

    # change legacy after migration — a stale second call must NOT re-copy and clobber the new copy
    (legacy / "default" / "api_key").write_bytes(b"value-2-should-not-propagate")
    assert migrate_if_needed(legacy, new) is False
    assert (new / "default" / "api_key").read_bytes() == b"value-1"


def test_migrate_dir_skip_env_opts_out(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "f").write_text("x")
    new = tmp_path / "new"
    assert migrate_if_needed(legacy, new, environ={"REDEVOPS_SKIP_WINDOWS_MIGRATION": "1"}) is False
    assert not new.exists()


def test_migrate_dir_interrupted_copy_leaves_no_half_written_destination(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy"
    (legacy / "default").mkdir(parents=True)
    (legacy / "default" / "api_key").write_bytes(b"value")
    new = tmp_path / "new"

    import shutil as shutil_module

    real_copytree = shutil_module.copytree

    def boom(*a, **k):
        real_copytree(*a, **k)
        raise OSError("simulated disk-full mid-migration")

    monkeypatch.setattr("agentic_os.mission.windows_state_migration.shutil.copytree", boom)
    assert migrate_if_needed(legacy, new) is False
    assert not new.exists()                                    # no half-written destination
    assert (legacy / "default" / "api_key").read_bytes() == b"value"   # legacy untouched

    monkeypatch.undo()
    # a later, un-sabotaged retry succeeds from scratch
    assert migrate_if_needed(legacy, new) is True
    assert (new / "default" / "api_key").read_bytes() == b"value"


def test_migrate_dir_verification_mismatch_rolls_back(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy"
    (legacy / "default").mkdir(parents=True)
    (legacy / "default" / "api_key").write_bytes(b"value")
    new = tmp_path / "new"

    import agentic_os.mission.windows_state_migration as mod
    monkeypatch.setattr(mod, "_file_hashes",
                         lambda root: {"deliberately": "source" if root == legacy else "staged-differs"})
    assert migrate_if_needed(legacy, new) is False
    assert not new.exists()
    assert (legacy / "default" / "api_key").read_bytes() == b"value"


def test_migrate_file_fresh_install_is_a_noop(tmp_path):
    legacy = tmp_path / "legacy-file"
    new = tmp_path / "new-dir" / "file"
    assert migrate_file_if_needed(legacy, new) is False
    assert not new.exists()


def test_migrate_file_with_legacy_data_copies_and_verifies(tmp_path):
    legacy = tmp_path / "trusted-sources"
    legacy.write_text("example.com\nanother.example\n", encoding="utf-8")
    new = tmp_path / "new-dir" / "trusted-sources"

    assert migrate_file_if_needed(legacy, new) is True
    assert new.read_text(encoding="utf-8") == "example.com\nanother.example\n"
    assert new.with_name(new.name + ".migration-complete").exists()
    assert legacy.read_text(encoding="utf-8") == "example.com\nanother.example\n"  # preserved


def test_migrate_file_is_idempotent(tmp_path):
    legacy = tmp_path / "trusted-sources"
    legacy.write_text("a\n", encoding="utf-8")
    new = tmp_path / "new-dir" / "trusted-sources"
    assert migrate_file_if_needed(legacy, new) is True

    legacy.write_text("b-should-not-propagate\n", encoding="utf-8")
    assert migrate_file_if_needed(legacy, new) is False
    assert new.read_text(encoding="utf-8") == "a\n"


def test_onboarding_default_secret_dir_migrates_on_simulated_windows(tmp_path, monkeypatch):
    from agentic_os.mission import onboarding

    legacy_home = tmp_path / "home"
    (legacy_home / ".redevops" / "secrets" / "default").mkdir(parents=True)
    (legacy_home / ".redevops" / "secrets" / "default" / "twenty_api_key").write_bytes(b"tok-123")

    monkeypatch.setattr(os.path, "expanduser", lambda p: str(legacy_home) if p == "~" else p)
    monkeypatch.delenv("REDEVOPS_SECRET_DIR", raising=False)
    monkeypatch.setattr("agentic_os.mission.windows_state_migration.is_windows", lambda platform=None: True)
    monkeypatch.setenv("REDEVOPS_WINDOWS_APPDATA_DIR", str(tmp_path / "localappdata" / "ReDevOps"))

    result = onboarding.default_secret_dir()
    assert result == str(tmp_path / "localappdata" / "ReDevOps" / "secrets")
    assert (tmp_path / "localappdata" / "ReDevOps" / "secrets" / "default" / "twenty_api_key").read_bytes() == b"tok-123"
    # legacy preserved
    assert (legacy_home / ".redevops" / "secrets" / "default" / "twenty_api_key").read_bytes() == b"tok-123"


def test_trust_store_default_path_migrates_on_simulated_windows(tmp_path, monkeypatch):
    from agentic_os.mission import trust

    legacy_home = tmp_path / "home"
    (legacy_home / ".agentic-os").mkdir(parents=True)
    (legacy_home / ".agentic-os" / "trusted-sources").write_text("trusted.example\n", encoding="utf-8")

    monkeypatch.setattr(os.path, "expanduser", lambda p: str(legacy_home) if p == "~" else p)
    monkeypatch.delenv("AGENTIC_OS_TRUST_FILE", raising=False)
    monkeypatch.delenv("AGENTIC_OS_HOME", raising=False)
    monkeypatch.setattr("agentic_os.mission.windows_state_migration.is_windows", lambda platform=None: True)
    monkeypatch.setenv("REDEVOPS_WINDOWS_APPDATA_DIR", str(tmp_path / "localappdata" / "ReDevOps"))

    path = trust._default_path()
    assert path == tmp_path / "localappdata" / "ReDevOps" / "trusted-sources"
    assert path.read_text(encoding="utf-8") == "trusted.example\n"
    assert (legacy_home / ".agentic-os" / "trusted-sources").read_text(encoding="utf-8") == "trusted.example\n"

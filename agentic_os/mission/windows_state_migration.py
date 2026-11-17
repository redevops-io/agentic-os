"""Relocate per-user state from ``%USERPROFILE%`` to ``%LOCALAPPDATA%`` on Windows.

Microsoft Store policy 10.2.7 requires a clean uninstall with no remnants outside the locations an
uninstaller can actually clean: ``%LOCALAPPDATA%\\<Publisher>\\<App>``, not the profile root. Before
this module, :mod:`agentic_os.mission.onboarding` and :mod:`agentic_os.mission.trust` wrote directly
under ``os.path.expanduser("~")`` (``%USERPROFILE%`` on Windows) via ``~/.redevops/secrets`` and
``~/.agentic-os/trusted-sources`` — fine on macOS/Linux (an ordinary dotfile convention, unchanged
here), wrong on Windows.

:func:`migrate_if_needed` is copy-only and additive, never destructive: it copies the legacy directory
into the new location, verifies every file by content hash, and only then writes a completion marker.
If a prior run copied but never verified (crash, disk full, etc.), the next run detects the missing
marker and restarts the copy from scratch; a partially-written destination is removed first so a retry
never merges with debris from an interrupted one. The legacy directory is never deleted — it stays on
disk as a passive backup/rollback path. Set :data:`REDEVOPS_SKIP_WINDOWS_MIGRATION` in the environment
to opt out (tests, or a deliberate rollback to the legacy location via the existing env-var overrides
each store already honours: ``REDEVOPS_SECRET_DIR``, ``AGENTIC_OS_TRUST_FILE``).
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from agentic_os._platform import files as _platform_files

_MARKER = ".migration-complete"
_SKIP_ENV = "REDEVOPS_SKIP_WINDOWS_MIGRATION"


def is_windows(platform: str | None = None) -> bool:
    return (platform or os.name) in ("nt", "windows")


def windows_app_data_root(environ: "dict[str, str] | None" = None) -> Path:
    """``%LOCALAPPDATA%\\ReDevOps`` (or ``REDEVOPS_WINDOWS_APPDATA_DIR`` override, for tests)."""
    env = os.environ if environ is None else environ
    override = env.get("REDEVOPS_WINDOWS_APPDATA_DIR")
    if override:
        return Path(override)
    base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "ReDevOps"


def _file_hashes(root: Path) -> "dict[str, str]":
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def migrate_if_needed(legacy_dir: Path, new_dir: Path, *, environ: "dict[str, str] | None" = None) -> bool:
    """Copy ``legacy_dir`` into ``new_dir`` once, verified, idempotent. Returns True if it migrated now.

    Fails closed: any copy/verification error removes the half-written ``new_dir`` and leaves
    ``legacy_dir`` exactly as it was, so the caller's own default-path logic keeps using the legacy
    location until a later run succeeds. Never raises for an expected absence (fresh install) or an
    already-migrated state — both are silent no-ops.
    """
    env = os.environ if environ is None else environ
    if env.get(_SKIP_ENV):
        return False
    if (new_dir / _MARKER).exists():
        return False                                        # already migrated
    if not legacy_dir.exists() or not any(legacy_dir.iterdir()):
        return False                                         # fresh install, nothing to migrate

    staging = new_dir.parent / f"{new_dir.name}.migrating"
    try:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)        # debris from an interrupted prior attempt
        staging.mkdir(parents=True, exist_ok=True)
        shutil.copytree(legacy_dir, staging, dirs_exist_ok=True)

        source_hashes = _file_hashes(legacy_dir)
        staged_hashes = _file_hashes(staging)
        if source_hashes != staged_hashes:
            raise OSError(f"migration verification mismatch: {len(source_hashes)} source files, "
                          f"{len(staged_hashes)} staged, content differs")

        if new_dir.exists():
            shutil.rmtree(new_dir, ignore_errors=True)
        _platform_files.ensure_private_dir(str(new_dir.parent))
        os.rename(str(staging), str(new_dir))                 # atomic on the same volume
        (new_dir / _MARKER).write_text(
            f"migrated from {legacy_dir} — legacy copy preserved, not deleted\n", encoding="utf-8")
        return True
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        if new_dir.exists() and not (new_dir / _MARKER).exists():
            shutil.rmtree(new_dir, ignore_errors=True)        # never leave a half-written destination
        return False


def migrate_file_if_needed(legacy_file: Path, new_file: Path, *,
                            environ: "dict[str, str] | None" = None) -> bool:
    """Single-file counterpart to :func:`migrate_if_needed` (e.g. one flat state file, not a directory).

    Same contract: additive, verified by hash, idempotent via a sibling ``<name>.migration-complete``
    marker, fails closed (removes a half-written destination, never touches ``legacy_file``).
    """
    env = os.environ if environ is None else environ
    if env.get(_SKIP_ENV):
        return False
    marker = new_file.with_name(new_file.name + _MARKER)
    if marker.exists():
        return False
    if not legacy_file.exists() or not legacy_file.is_file():
        return False

    staging = new_file.with_name(new_file.name + ".migrating")
    try:
        _platform_files.ensure_private_dir(str(new_file.parent))
        shutil.copyfile(legacy_file, staging)
        if hashlib.sha256(staging.read_bytes()).hexdigest() != hashlib.sha256(legacy_file.read_bytes()).hexdigest():
            raise OSError("migration verification mismatch")
        os.replace(str(staging), str(new_file))               # atomic on the same volume
        marker.write_text(f"migrated from {legacy_file} — legacy copy preserved, not deleted\n",
                           encoding="utf-8")
        return True
    except Exception:
        if staging.exists():
            staging.unlink(missing_ok=True)
        return False

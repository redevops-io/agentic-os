"""The execution profile of the packaged desktop sidecar: chosen explicitly, before anything else runs.

The frozen sidecar used to start with no ``AGENTIC_OS_PROFILE`` at all, so it ran as ``development`` by default. A packaged build must not get
its security posture from a missing setting (``mission/profile.py`` refuses that). The desktop app has no deployer who could set one, so the
sidecar entry point calls :func:`apply` first:

* it marks the process as a packaged build (``AGENTIC_OS_PACKAGED=1``);
* when no profile was chosen it selects ``production`` and points ``AGENTIC_OS_AUTHORITY_DB`` and ``AGENTIC_OS_REPLAY_DB`` at private files in
  the per-user state directory (next to the registration audit), creating that directory 0700;
* a profile somebody did set is kept as it is. ``production`` without stores gets the same per-user stores; ``development`` and ``test`` are
  left alone, as a stated choice.

Pure with respect to its ``env`` argument except for creating the state directory.
"""
from __future__ import annotations

import os
import sys
from typing import MutableMapping, Optional

from .desktop_sources import default_audit_path
from .mission.profile import ENV_PACKAGED, ENV_PROFILE, PRODUCTION

ENV_AUTHORITY_DB = "AGENTIC_OS_AUTHORITY_DB"
ENV_REPLAY_DB = "AGENTIC_OS_REPLAY_DB"
ENV_DASHBOARDS_DIR = "AGENTIC_OS_DASHBOARDS_DIR"
ENV_DASHBOARDS_FILE = "AGENTIC_OS_DASHBOARDS"
ENV_LOCAL_AGENTS_DIR = "AGENTIC_OS_LOCAL_AGENTS_DIR"
ENV_DASHBOARDS_DEFAULT_FILE = "AGENTIC_OS_DASHBOARDS_DEFAULT_FILE"
ENV_CAPTURE_DB = "AGENTIC_OS_CAPTURE_DB"
STATE_SUBDIR = "authority"
_TRUE = ("1", "true", "yes", "on")


def state_dir(env=None, home: Optional[str] = None, platform: Optional[str] = None) -> str:
    """``<per-user application state>/<app id>/authority``: the sibling of the directory that holds the registration audit."""
    platform = sys.platform if platform is None else platform
    audit = default_audit_path({k: v for k, v in (env or os.environ).items() if k != "AGENTIC_OS_SOURCES_AUDIT_PATH"}, home, platform)
    import ntpath
    import posixpath
    path = ntpath if platform.startswith("win") else posixpath
    return path.join(path.dirname(path.dirname(audit)), STATE_SUBDIR)


def apply(env: Optional[MutableMapping[str, str]] = None, home: Optional[str] = None, platform: Optional[str] = None) -> dict:
    """Make the profile explicit in ``env``. Returns what was decided: ``{"profile", "chosen_here", "authority_db", "replay_db"}``."""
    env = os.environ if env is None else env
    env[ENV_PACKAGED] = "1"
    raw = (env.get(ENV_PROFILE) or "").strip().lower()
    alias = (env.get("AGENTIC_OS_REQUIRE_AUTHORITY") or "").strip().lower() in _TRUE
    chosen_here = not raw and not alias
    if chosen_here:
        env[ENV_PROFILE] = PRODUCTION
    effective = (env.get(ENV_PROFILE) or "").strip().lower() or (PRODUCTION if alias else "")
    if effective == PRODUCTION:
        d = state_dir(env, home, platform)
        missing = [k for k in (ENV_AUTHORITY_DB, ENV_REPLAY_DB) if not (env.get(k) or "").strip()]
        if missing:
            os.makedirs(d, mode=0o700, exist_ok=True)
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass        # Windows: the per-user profile's ACL is the protection there
            sep = "\\" if (sys.platform if platform is None else platform).startswith("win") else "/"
            if ENV_AUTHORITY_DB in missing:
                env[ENV_AUTHORITY_DB] = d + sep + "authority.db"
            if ENV_REPLAY_DB in missing:
                env[ENV_REPLAY_DB] = d + sep + "replay.db"
        # The dashboards' saved views and history, and the durable intake behind "Track in Today": the same per-user directory. Without these
        # the dashboards keep both in memory and say so, and the intake answers 501; a desktop user would lose both at every restart.
        dsep = "\\" if (sys.platform if platform is None else platform).startswith("win") else "/"
        if not (env.get(ENV_DASHBOARDS_DIR) or "").strip() or not (env.get(ENV_CAPTURE_DB) or "").strip():
            os.makedirs(d, mode=0o700, exist_ok=True)
        if not (env.get(ENV_DASHBOARDS_DIR) or "").strip():
            env[ENV_DASHBOARDS_DIR] = d + dsep + "dashboards"
            os.makedirs(env[ENV_DASHBOARDS_DIR], mode=0o700, exist_ok=True)
        if not (env.get(ENV_CAPTURE_DB) or "").strip():
            env[ENV_CAPTURE_DB] = d + dsep + "capture.db"
        # The dashboards file of the desktop app lives next to its data. It is read only when a person has created it (an unreadable
        # file grants nothing, so a path to a missing file must never be set); its place is always published, so a dashboard that is
        # not offered can name the exact file that enables it.
        # Where the credentials of connected external agents live, and where the running gateway is published for them. Setting the
        # directory admits nobody: an agent exists only after the user connects one (agentic_os/local_agents.py).
        if not (env.get(ENV_LOCAL_AGENTS_DIR) or "").strip():
            env[ENV_LOCAL_AGENTS_DIR] = d + dsep + "agents"
        # Governed writes and portable applications, beside the other per-user state. Nothing is changed by this alone: an agent
        # proposes only after the person at the app has connected it with that permission, and each change waits for them there.
        if not (env.get("AGENTIC_OS_WRITES_DIR") or "").strip():
            env["AGENTIC_OS_WRITES_DIR"] = d + dsep + "writes"
        if not (env.get("AGENTIC_OS_APPS") or "").strip():
            env["AGENTIC_OS_APPS"] = "1"
        if not (env.get(ENV_DASHBOARDS_FILE) or "").strip():
            default_file = env[ENV_DASHBOARDS_DIR] + dsep + "dashboards.yaml"
            env[ENV_DASHBOARDS_DEFAULT_FILE] = default_file
            if os.path.isfile(default_file):
                env[ENV_DASHBOARDS_FILE] = default_file
    return {"profile": effective, "chosen_here": chosen_here,
            "authority_db": (env.get(ENV_AUTHORITY_DB) or ""), "replay_db": (env.get(ENV_REPLAY_DB) or "")}


def posture_line(env=None) -> str:
    """One line for a packaged build's stderr, so the posture of an installed build can be read from its own output."""
    env = os.environ if env is None else env
    stores = bool((env.get(ENV_AUTHORITY_DB) or "").strip() and (env.get(ENV_REPLAY_DB) or "").strip())
    return "redevops-sidecar: profile=%s; authority and replay stores are %s" % (
        (env.get(ENV_PROFILE) or "").strip().lower() or "unset", "durable, per user" if stores else "not configured")

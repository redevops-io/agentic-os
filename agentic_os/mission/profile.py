"""Execution profile: whether authority enforcement is an invariant or an explicit local convenience.

* ``production``  - durable authority store, guard and ledger are REQUIRED; startup fails if any is missing, and
                    every node is checked against the owner's envelope (no envelope = no autonomous action).
* ``development`` - the permissive local mode is allowed, but it is an explicit choice and is logged.
* ``test``        - injected guards and deterministic clocks; durability is not required.

Selected by ``AGENTIC_OS_PROFILE``. ``AGENTIC_OS_REQUIRE_AUTHORITY=1`` is kept as an alias for ``production``.
When neither is set the process runs as ``development`` and says so once on the log: an unset profile is never a
silent grant of authority, but it does not break an existing install that has no envelope database yet. Any other
value is an error rather than a guess.
"""
from __future__ import annotations

import logging
import os

PRODUCTION, DEVELOPMENT, TEST = "production", "development", "test"
PROFILES = (PRODUCTION, DEVELOPMENT, TEST)
ENV_PROFILE = "AGENTIC_OS_PROFILE"
_log = logging.getLogger("agentic_os.profile")
_warned = False


class ProfileError(RuntimeError):
    pass


def profile() -> str:
    global _warned
    raw = os.environ.get(ENV_PROFILE, "").strip().lower()
    if raw:
        if raw not in PROFILES:
            raise ProfileError(f"{ENV_PROFILE}={raw!r} is not one of {PROFILES}")
        return raw
    if os.environ.get("AGENTIC_OS_REQUIRE_AUTHORITY", "").strip().lower() in ("1", "true", "yes", "on"):
        return PRODUCTION
    if not _warned:
        _warned = True
        _log.warning("%s is not set: running as 'development' - authority is NOT enforced. "
                     "Set %s=production (and AGENTIC_OS_AUTHORITY_DB) for any deployment that acts on real systems.",
                     ENV_PROFILE, ENV_PROFILE)
    return DEVELOPMENT


def is_production() -> bool:
    return profile() == PRODUCTION


def assert_authority(guard, *, where: str) -> None:
    """Fail startup (or the call) in production unless a durable guard is wired. Never raises otherwise."""
    if profile() == PRODUCTION and (guard is None or not getattr(guard, "durable", False)):
        raise ProfileError(f"{where}: {ENV_PROFILE}=production requires a durable authority store, guard and ledger "
                           f"(set AGENTIC_OS_AUTHORITY_DB); refusing to run without enforceable authority")

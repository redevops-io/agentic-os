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


_announced: set = set()


def announce(where: str, guard=None) -> None:
    """One startup line per component with the effective posture, so a deployment that is accidentally in
    development is visible in the logs of every service that makes the decision."""
    if where in _announced:
        return
    _announced.add(where)
    st = status(guard)
    _log.log(logging.INFO if st["ok"] else logging.WARNING, "security profile for %s: %s%s", where, st["profile"],
             "" if st["ok"] else " (" + "; ".join(st["warnings"]) + ")")


def assert_authority(guard, *, where: str) -> None:
    """Fail startup (or the call) in production unless a durable guard is wired. Never raises otherwise."""
    announce(where, guard)
    if profile() == PRODUCTION and (guard is None or not getattr(guard, "durable", False)):
        raise ProfileError(f"{where}: {ENV_PROFILE}=production requires a durable authority store, guard and ledger "
                           f"(set AGENTIC_OS_AUTHORITY_DB); refusing to run without enforceable authority")


def status(guard=None, *, replay_durable: "bool | None" = None) -> dict:
    """The effective security posture, for startup logs, readiness checks and Edge Sentinel: a deployment that is
    accidentally in development mode must be visible, not inferred."""
    prof = profile()
    explicit = bool(os.environ.get(ENV_PROFILE, "").strip()) or os.environ.get(
        "AGENTIC_OS_REQUIRE_AUTHORITY", "").strip().lower() in ("1", "true", "yes", "on")
    warnings = []
    if prof != PRODUCTION:
        warnings.append(f"profile is '{prof}': authority is not enforced")
    if not explicit:
        warnings.append(f"{ENV_PROFILE} is not set (defaulted to development)")
    if guard is not None and not getattr(guard, "durable", False):
        warnings.append("authority guard is not durable")
    if replay_durable is False:
        warnings.append("approval/authorization replay protection is per-process")
    return {"profile": prof, "explicit": explicit, "authority_configured": guard is not None,
            "authority_durable": bool(guard is not None and getattr(guard, "durable", False)),
            "replay_durable": replay_durable, "ok": prof == PRODUCTION and not warnings, "warnings": warnings}


def check_environment(env=None) -> list:
    """Problems that would make the PRODUCTION profile unsafe or unable to start. Pure (reads only `env`), so a deployment
    pipeline can run it before rollout. Returns [] when everything production needs is present."""
    e = os.environ if env is None else env
    problems = []

    def dir_writable(path: str) -> bool:
        d = os.path.dirname(os.path.abspath(path)) or "."
        return os.path.isdir(d) and os.access(d, os.W_OK)

    if (e.get(ENV_PROFILE, "") or "").strip().lower() != PRODUCTION:
        problems.append(f"{ENV_PROFILE} is not set to 'production'")
    for var, what in (("AGENTIC_OS_AUTHORITY_DB", "durable authority store and usage ledger"),
                      ("AGENTIC_OS_REPLAY_DB", "durable approval/authorization replay protection")):
        v = (e.get(var, "") or "").strip()
        if not v:
            problems.append(f"{var} is not set ({what})")
        elif not dir_writable(v):
            problems.append(f"{var}={v!r}: its directory does not exist or is not writable")
    secret = (e.get("AGENTIC_OS_APPROVAL_SECRET", "") or "")
    if len(secret) < 32:
        problems.append("AGENTIC_OS_APPROVAL_SECRET is missing or shorter than 32 characters (signs approvals and authorizations)")
    if not ((e.get("AGENTIC_OS_CONTROL_TOKEN", "") or "").strip() or (e.get("AGENTIC_OS_TENANT_TOKENS", "") or "").strip()):
        problems.append("no AGENTIC_OS_CONTROL_TOKEN or AGENTIC_OS_TENANT_TOKENS: operator /invoke would be disabled")
    if (e.get("AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS", "") or "").strip() == "1":
        problems.append("AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS=1 must not be set in production")
    return problems


def main(argv=None) -> int:
    """`python -m agentic_os.mission.profile [--require-production]` prints the effective posture; with
    --require-production it exits 1 (and lists why) unless every production requirement is met. Run it in the
    deployment pipeline and as a container health/preflight step."""
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="agentic_os.mission.profile")
    ap.add_argument("--require-production", action="store_true")
    a = ap.parse_args(argv)
    out = {"posture": status()}
    if a.require_production:
        out["problems"] = check_environment()
    print(json.dumps(out, indent=2))
    return 1 if a.require_production and out["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

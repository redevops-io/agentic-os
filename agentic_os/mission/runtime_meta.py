"""Runtime interpretation metadata — the versions a Mission was reasoned under.

A content hash tells you a plan/evidence object *had* identity X. It does not tell you *how that
identity was computed* — which contract schema, which canonicalization rule, which runtime-contracts
release the interpreter linked against. For replay years later (and for the cross-language conformance
claim), that "interpreted under" context is as important as the hash itself.

This module surfaces it in one place so EXPLAIN output and a ``/buildinfo`` endpoint can stamp every
answer with: the installed ``runtime-contracts`` version, the canonicalization version (``rcv1``), the
contract schema version, and the seal contract version. Keep it dependency-light and fail-soft — a
missing package metadata entry must never break a mission.
"""
from __future__ import annotations

from functools import lru_cache

try:  # the canonicalization + schema version constants are the source of truth
    from runtime_contracts import (
        CANONICALIZATION_VERSION,
        CONTRACT_VERSION,
        SEAL_CONTRACT_VERSION,
    )
except Exception:  # noqa: BLE001 — contracts must be importable in practice; stay fail-soft
    CANONICALIZATION_VERSION = CONTRACT_VERSION = SEAL_CONTRACT_VERSION = "unknown"


def _installed_version(dist: str) -> str:
    """The version of an installed distribution (the actual pinned artifact), or 'unknown'."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version(dist)
        except PackageNotFoundError:
            return "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


@lru_cache(maxsize=1)
def runtime_build_metadata() -> dict:
    """The interpretation context stamped onto EXPLAIN / build info. ``runtime_contracts_version`` is
    the *installed* pin (so it catches drift between what a repo declares and what it actually links);
    the rest are the contract's own version constants."""
    rc_pkg = _installed_version("runtime-contracts")
    if rc_pkg == "unknown":  # editable/source checkout without dist metadata — fall back to the module
        try:
            import runtime_contracts
            rc_pkg = getattr(runtime_contracts, "__version__", "unknown")
        except Exception:  # noqa: BLE001
            rc_pkg = "unknown"
    return {
        "runtime_contracts_version": rc_pkg,
        "canonicalization_version": CANONICALIZATION_VERSION,   # "rcv1"
        "contract_schema_version": CONTRACT_VERSION,
        "seal_contract_version": SEAL_CONTRACT_VERSION,
    }

"""SCAN_ALLOWLIST — the actual enforcement, not a policy claim.

A scan never starts against a target this process wasn't explicitly told about. The allowlist is
per-profile (a target approved for `trivy-image`-style web scanning is not automatically approved for
`k8s-rbac`, which reaches a different kind of resource), configured once by the operator who runs this
process, never learned or widened at runtime.

Format (env var SCAN_ALLOWLIST): one `profile=target` pair per line (or comma-separated on one line).
A target may end in `*` to allow a literal-prefix family (e.g. `k8s-rbac=kind-deploy-scan-test/*` allows
every namespace in that one named kubeconfig context) — there is no other wildcard or pattern matching,
deliberately: no regex engine to get subtly wrong on a security boundary.
"""
from __future__ import annotations

import os

ALLOWLIST_ENV = "SCAN_ALLOWLIST"


def _parse(raw: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for chunk in raw.replace(",", "\n").splitlines():
        chunk = chunk.strip()
        if not chunk or chunk.startswith("#"):
            continue
        if "=" not in chunk:
            raise ValueError(f"SCAN_ALLOWLIST entry {chunk!r} is not profile=target")
        profile, target = chunk.split("=", 1)
        profile, target = profile.strip(), target.strip()
        if not profile or not target:
            raise ValueError(f"SCAN_ALLOWLIST entry {chunk!r} has an empty profile or target")
        pairs.append((profile, target))
    return pairs


def load(raw: str | None = None) -> list[tuple[str, str]]:
    """The parsed (profile, target) pairs from SCAN_ALLOWLIST (or an explicit string, for tests)."""
    return _parse(os.getenv(ALLOWLIST_ENV, "") if raw is None else raw)


def is_allowed(profile: str, target: str, entries: list[tuple[str, str]]) -> bool:
    """Fail closed: true only on an exact (profile, target) match or a `*`-suffixed prefix match."""
    for p, t in entries:
        if p != profile:
            continue
        if t == target:
            return True
        if t.endswith("*") and target.startswith(t[:-1]):
            return True
    return False

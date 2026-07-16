"""Capability trust boundary — discover metadata-only, gate execution.

A capability whose ``source`` is a third party (a plugin, a cloned repo, an external registry) is an
execution surface: binding it lets it run. So the Context Runtime treats untrusted sources the way a
plugin host does (cf. xai-grok-build's plugin trust): capabilities from an untrusted source are still
**discovered and listed** (the registry returns them, metadata-only), but they are **not bindable**
until the source is trusted — the bind door fails closed. Built-in capabilities (``source == ""``)
are trusted by default, so nothing existing changes.

Trust is per-source (a canonical string), persisted one-per-line — the same shape as a plugin
trust-store — under ``AGENTIC_OS_TRUST_FILE`` or ``~/.agentic-os/trusted-sources``.
"""
from __future__ import annotations

import os
from pathlib import Path


def _default_path() -> Path:
    env = os.environ.get("AGENTIC_OS_TRUST_FILE")
    if env:
        return Path(env)
    home = os.environ.get("AGENTIC_OS_HOME") or os.path.expanduser("~")
    return Path(home) / ".agentic-os" / "trusted-sources"


class TrustStore:
    """The set of trusted capability sources. Built-in (empty source) is always trusted."""

    def __init__(self, path: str | os.PathLike | None = None, trusted: set[str] | None = None):
        self.path = Path(path) if path is not None else _default_path()
        self._trusted: set[str] = set(trusted) if trusted is not None else self._load()

    def _load(self) -> set[str]:
        try:
            return {ln.strip() for ln in self.path.read_text(encoding="utf-8").splitlines() if ln.strip()}
        except OSError:
            return set()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("\n".join(sorted(self._trusted)) + "\n", encoding="utf-8")

    def is_trusted(self, source: str | None) -> bool:
        """Built-in/unsourced (``""``/None) is trusted; otherwise the source must be trusted."""
        return not source or source in self._trusted

    def trust(self, source: str, *, persist: bool = True) -> None:
        if source:
            self._trusted.add(source)
            if persist:
                self.save()

    def revoke(self, source: str, *, persist: bool = True) -> None:
        self._trusted.discard(source)
        if persist:
            self.save()

    def trusted(self) -> set[str]:
        return set(self._trusted)

"""``DocumentBackend``: the one interface every external editor adapter implements.

Capability model (loaded from config, never assumed)::

    document_backend: {provider: onlyoffice|collabora|elixiroffice, deployment: external,
                       capabilities: {open, convert, inspect, tracked_changes, automated_edit, export}}

*Declared* capabilities are what the operator asks for; the *effective* set is ``declared AND adapter-proven``.
``automated_edit`` and ``tracked_changes`` are only ever effective when the adapter lists them in ``PROVEN`` -- a
claim backed by a named integration test against a real stock Document Server (see ``EDIT_API.md``). Every
operation calls ``require(capability)`` first and FAILS CLOSED with ``UnsupportedCapability`` otherwise.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .errors import ConfigError, UnsupportedCapability

CAPABILITIES = ("open", "convert", "inspect", "tracked_changes", "automated_edit", "export")
PROVIDERS = ("onlyoffice", "collabora", "elixiroffice")


@dataclass(frozen=True)
class BackendDecl:
    """The parsed ``document_backend`` block."""
    provider: str
    deployment: str = "external"
    capabilities: frozenset = frozenset()
    experimental_enabled: bool = False

    @staticmethod
    def parse(raw: Optional[Mapping[str, Any]]) -> "BackendDecl":
        if not isinstance(raw, Mapping):
            raise ConfigError("document_backend block is missing")
        extra = set(raw) - {"provider", "deployment", "capabilities", "experimental_enabled"}
        if extra:
            raise ConfigError(f"document_backend: unknown key(s) {sorted(extra)}")
        provider = str(raw.get("provider", "")).strip().lower()
        if provider not in PROVIDERS:
            raise ConfigError(f"document_backend.provider must be one of {PROVIDERS}, got {provider!r}")
        deployment = str(raw.get("deployment", "external")).strip().lower()
        if deployment != "external":
            raise ConfigError("document_backend.deployment must be 'external': the editor is never bundled or managed")
        caps = raw.get("capabilities", {})
        if not isinstance(caps, Mapping):
            raise ConfigError("document_backend.capabilities must be a mapping of capability -> bool")
        unknown = set(caps) - set(CAPABILITIES)
        if unknown:
            raise ConfigError(f"document_backend.capabilities: unknown capability(ies) {sorted(unknown)}")
        for k, v in caps.items():
            if not isinstance(v, bool):
                raise ConfigError(f"document_backend.capabilities.{k} must be true/false")
        enabled = frozenset(k for k, v in caps.items() if v)
        exp = raw.get("experimental_enabled", False)
        if not isinstance(exp, bool):
            raise ConfigError("document_backend.experimental_enabled must be true/false")
        return BackendDecl(provider, deployment, enabled, exp)


@dataclass(frozen=True)
class Capabilities:
    declared: frozenset
    proven: frozenset
    enabled: bool = True
    reason: str = ""

    @property
    def effective(self) -> frozenset:
        return (self.declared & self.proven) if self.enabled else frozenset()

    def has(self, cap: str) -> bool:
        return cap in self.effective

    def report(self) -> dict:
        """The truthful capability report: a capability is True only if declared AND proven (and the backend on)."""
        eff = self.effective
        return {"capabilities": {c: (c in eff) for c in CAPABILITIES},
                "declared": sorted(self.declared), "proven": sorted(self.proven),
                "enabled": self.enabled, "reason": self.reason}


@dataclass
class DocRef:
    """What a backend receives. ``content_url`` is a short-lived, connector-hosted URL the editor fetches the bytes
    from (the transmit step; it only exists after the classification gate allowed it)."""
    doc_id: str
    tenant: str
    version: int
    sha256: str
    filename: str
    content_url: str = ""


@dataclass
class BackendFile:
    content: bytes
    filename: str
    sha256: str
    media_type: str = ""
    meta: dict = field(default_factory=dict)


class DocumentBackend(ABC):
    """An externally managed editor. Subclasses implement ``_do_*``; the public methods enforce capabilities."""

    provider = "abstract"
    #: capabilities PROVEN by an integration test against the real product (see tests/integration). Anything not in
    #: this set can never become effective, whatever the config says.
    PROVEN: frozenset = frozenset()

    def __init__(self, decl: BackendDecl):
        self.decl = decl

    # -- capability model ------------------------------------------------------------------------------------
    def describe(self) -> Capabilities:
        return Capabilities(declared=self.decl.capabilities, proven=self.PROVEN, enabled=self._enabled(),
                            reason=self._disabled_reason())

    def _enabled(self) -> bool:
        return True

    def _disabled_reason(self) -> str:
        return ""

    def require(self, cap: str) -> None:
        caps = self.describe()
        if cap not in CAPABILITIES:
            raise UnsupportedCapability(f"unknown capability {cap!r}")
        if not caps.has(cap):
            why = caps.reason or (
                "not declared in document_backend.capabilities" if cap not in caps.declared
                else "declared but not proven for this backend (fail closed)")
            raise UnsupportedCapability(f"{self.provider}: capability '{cap}' is unavailable: {why}",
                                        provider=self.provider, capability=cap)

    # -- operations (public, guarded) --------------------------------------------------------------------------
    def health(self) -> dict:
        """Connectivity probe. Allowed regardless of declared capabilities."""
        return self._do_health()

    def inspect(self, ref: DocRef) -> dict:
        self.require("inspect")
        return self._do_inspect(ref)

    def open_session(self, ref: DocRef, *, user_id: str, ttl_s: int, mode: str = "view") -> dict:
        self.require("open")
        return self._do_open_session(ref, user_id=user_id, ttl_s=ttl_s, mode=mode)

    def convert(self, ref: DocRef, target_format: str) -> BackendFile:
        self.require("convert")
        return self._do_convert(ref, target_format)

    def export(self, ref: DocRef, target_format: str) -> BackendFile:
        self.require("export")
        return self._do_convert(ref, target_format)

    def apply_edit(self, ref: DocRef, ops: "list[dict]", *, track_changes: bool, idem_key: str) -> BackendFile:
        """Run a validated, typed operation list and return the NEW document bytes. Never mutates the source."""
        self.require("automated_edit")
        if track_changes:
            self.require("tracked_changes")
        return self._do_apply_edit(ref, ops, track_changes=track_changes, idem_key=idem_key)

    # -- adapter hooks ------------------------------------------------------------------------------------------
    @abstractmethod
    def _do_health(self) -> dict: ...

    def _do_inspect(self, ref: DocRef) -> dict:
        raise UnsupportedCapability(f"{self.provider}: inspect not implemented")

    def _do_open_session(self, ref: DocRef, *, user_id: str, ttl_s: int, mode: str) -> dict:
        raise UnsupportedCapability(f"{self.provider}: open not implemented")

    def _do_convert(self, ref: DocRef, target_format: str) -> BackendFile:
        raise UnsupportedCapability(f"{self.provider}: convert not implemented")

    def _do_apply_edit(self, ref: DocRef, ops, *, track_changes: bool, idem_key: str) -> BackendFile:
        raise UnsupportedCapability(f"{self.provider}: automated_edit not implemented")


class UnimplementedBackend(DocumentBackend):
    """Collabora (later) and ElixirOffice (experimental, off by default): declared in config but no adapter ships.
    Every operation fails closed. ElixirOffice additionally requires ``experimental_enabled`` and still has no
    proven capability."""

    def __init__(self, decl: BackendDecl):
        super().__init__(decl)
        self.provider = decl.provider

    def _enabled(self) -> bool:
        return False

    def _disabled_reason(self) -> str:
        if self.decl.provider == "elixiroffice":
            return "ElixirOffice is experimental and disabled; no adapter is shipped"
        return "no adapter is shipped for this provider yet"

    def _do_health(self) -> dict:
        raise UnsupportedCapability(f"{self.provider}: not implemented / unavailable: {self._disabled_reason()}",
                                    provider=self.provider)

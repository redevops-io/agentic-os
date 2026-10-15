"""Lazy construction of the DocumentService from configuration + environment.

The app must boot and serve ``/health`` and ``/capabilities`` even when the connector is not configured or the
editor is down: a missing/invalid configuration surfaces as a typed ``not_configured`` result on each call (fail
closed), never as a crashed process and never as an effect on other apps.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from .backend import DocumentBackend, UnimplementedBackend
from .config import ConnectorConfig, load_config
from .content import ContentRegistry
from .errors import ConfigError
from .onlyoffice import OnlyOfficeConfig
from .onlyoffice import OnlyOfficeBackend
from .service import DocumentService
from .store import LocalFSStore


def build_backend(cfg: ConnectorConfig, content: ContentRegistry) -> DocumentBackend:
    if cfg.decl.provider == "onlyoffice":
        return OnlyOfficeBackend(cfg.decl, OnlyOfficeConfig.from_env(zone=cfg.backend_zone), content)
    return UnimplementedBackend(cfg.decl)


def build_service(cfg: Optional[ConnectorConfig] = None) -> DocumentService:
    cfg = cfg or load_config()
    from agentic_os.mission.approval import ReplayGuard  # noqa: PLC0415
    content = ContentRegistry(cfg.content_base_url)
    return DocumentService(cfg, LocalFSStore(cfg.document_root), build_backend(cfg, content), content,
                           replay_guard=ReplayGuard())


class ServiceHolder:
    """Caches the service; a failed build is remembered for a few seconds so a misconfiguration is not retried on
    every request, then retried (so a fixed config / recovered Vault takes effect without a restart)."""

    def __init__(self, factory=build_service, retry_s: float = 5.0):
        self._factory, self._retry_s = factory, retry_s
        self._svc: Optional[DocumentService] = None
        self._err: Optional[ConfigError] = None
        self._err_at = 0.0
        self._lock = threading.Lock()

    def get(self) -> DocumentService:
        if self._svc is not None:
            return self._svc
        with self._lock:
            if self._svc is not None:
                return self._svc
            if self._err is not None and time.monotonic() - self._err_at < self._retry_s:
                raise self._err
            try:
                self._svc = self._factory()
            except ConfigError as e:
                self._err, self._err_at = e, time.monotonic()
                raise
            except Exception as e:  # noqa: BLE001 - e.g. unreadable root: still typed, still fail closed
                self._err, self._err_at = ConfigError(f"connector could not start: {type(e).__name__}: {e}"), time.monotonic()
                raise self._err from None
            return self._svc


_HOLDER = ServiceHolder()


def default_service() -> DocumentService:
    """The process-wide lazily built service (configuration + environment)."""
    return _HOLDER.get()

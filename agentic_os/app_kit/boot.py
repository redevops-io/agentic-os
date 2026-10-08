"""Boot-time app registration (plan §3 / N8).

A process gets one shared ``AppRegistry`` via :func:`default_registry`. An app registers its
runtime-native contract at startup by calling :func:`register_app_manifest` with its ``manifest``
module — fail-closed, so a non-conformant app raises ``ManifestError`` and does not boot. It is
idempotent, so importing the manifest more than once in a process is safe.

Wiring (one line in an app's startup, e.g. app.py):

    from agentic_os.app_kit.boot import register_app_manifest
    from . import manifest as _manifest
    register_app_manifest(_manifest)
"""
from __future__ import annotations

from typing import Any, Optional

from .registry import AppRegistry, RegisteredApp

_DEFAULT: Optional[AppRegistry] = None


def default_registry() -> AppRegistry:
    """The process-wide AppRegistry (created on first use)."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = AppRegistry()
    return _DEFAULT


def reset_default_registry() -> None:
    """Drop the process registry (tests only)."""
    global _DEFAULT
    _DEFAULT = None


def register_app_manifest(manifest_module: Any, *, registry: Optional[AppRegistry] = None) -> RegisteredApp:
    """Register a manifest module's app into the (default) registry, idempotently and fail-closed.

    ``manifest_module`` exposes ``MANIFEST`` and ``default_operator()`` (the per-app manifest.py
    shape). Raises ``ManifestError`` if the app is non-conformant — the app must not boot.
    """
    reg = registry or default_registry()
    manifest = manifest_module.MANIFEST
    operator = manifest_module.default_operator()
    return reg.ensure(manifest, operator)


__all__ = ["default_registry", "reset_default_registry", "register_app_manifest"]

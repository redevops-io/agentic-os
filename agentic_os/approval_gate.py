"""Shared approval-gate wrapper for side-effecting capability handlers (the #399 pattern).

``gated(action, handler, **preview)`` returns a handler that runs ``handler`` ONLY when the input carries a
runtime-delivered ``_approval`` (a resumed, approved Mission node, or the trusted in-process governance
path). The open HTTP /invoke strips forged ``_approval*`` keys, so otherwise it returns
``pending_approval(...)`` -- a staged preview with ``executed: False`` -- WITHOUT performing the side effect.
``preview`` carries the capability's declared-output marker set falsey plus safe context.
"""
from __future__ import annotations

from typing import Callable

from agentic_os.mission.operator_sdk import pending_approval


def gated(action: str, handler: Callable[[dict], dict], **preview) -> Callable[[dict], dict]:
    def _handler(inp: dict) -> dict:
        if inp.get("_approval"):
            return handler(inp)
        return pending_approval(action, **preview)
    _handler.__name__ = getattr(handler, "__name__", "gated_handler")
    return _handler

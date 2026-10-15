"""Bearer-adding transport for the kernel's ``HTTPOperatorClient``.

The kernel client sends no ``Authorization`` header, but this operator is authenticated (``mission_gate``). Use::

    HTTPOperatorClient({"document-editor": "https://docedit.internal:8216"}, transport=bearer_transport(token))
"""
from __future__ import annotations

from agentic_os.mission.util import _urllib_transport


def bearer_transport(token: str):
    if not token or not isinstance(token, str):
        raise ValueError("a bearer token is required")

    def transport(url: str, body: dict, headers: dict, timeout: float) -> dict:
        return _urllib_transport(url, body, {**headers, "Authorization": f"Bearer {token}"}, timeout)
    return transport

"""Bearer-adding transport for the kernel's ``HTTPOperatorClient``.

The kernel client sends no ``Authorization`` header, but this operator is authenticated (``mission_gate``). Use::

    HTTPOperatorClient({"document-editor": "https://docedit.internal:8216"}, transport=bearer_transport(token))

``https://`` is required for any non-loopback host (the token is refused over plain http).
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from agentic_os.mission.util import _urllib_transport


def bearer_transport(token: str):
    if not token or not isinstance(token, str):
        raise ValueError("a bearer token is required")

    def transport(url: str, body: dict, headers: dict, timeout: float) -> dict:
        sp = urlsplit(url)
        host = (sp.hostname or "").lower()
        loop = host == "localhost"
        try:
            loop = loop or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if sp.scheme != "https" and not loop:
            raise ValueError("refusing to send a bearer token over plain http to a non-loopback host: use https://")
        return _urllib_transport(url, body, {**headers, "Authorization": f"Bearer {token}"}, timeout)
    return transport

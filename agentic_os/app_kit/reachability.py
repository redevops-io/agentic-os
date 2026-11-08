"""Did this connection arrive over loopback? One implementation for every request-time boundary.

Two guards ask this question: the module proxy in `agentic_os.control_plane` (writes through `/m/*`) and
`agentic_os.app_kit.route_authz` (an app's own write routes). Both read the ASGI `server` scope entry, which
uvicorn fills with the **local address of the accepted socket** — chosen by the server, not by the caller, and
verified against a real uvicorn in `tests/test_mproxy_write_containment.py` (a `0.0.0.0` bind dialled from
loopback reports `127.0.0.1`; a `::` bind dialled at `::1` reports `::1`). No Host, X-Forwarded-For or
Cloudflare header can change which local address the kernel delivered the connection on.

What this deliberately does NOT claim, and what every caller must say out loud: a reverse proxy terminating on
the same host connects over loopback, so its traffic looks local even when it carried a request from the
internet. The socket answers "which interface did this TCP peer reach us on", not "who is upstream". A
deployment that fronts this process with a same-host proxy that does not authenticate its own callers has
that proxy inside its trusted surface — see docs/SECURITY_MODEL.md.

Anything that is not a loopback IP — a container interface address such as 172.17.0.4, a LAN address, a bare
hostname like TestClient's `testserver`, a missing or malformed scope — answers False. Unknown is treated as
exposed, because that is the safe reading of an address we cannot place inside the host.
"""
from __future__ import annotations

import ipaddress

__all__ = ["arrived_on_loopback"]


def arrived_on_loopback(scope: dict) -> bool:
    """True only when the socket this request arrived on is loopback."""
    host = ((scope or {}).get("server") or ("", 0))[0] or ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

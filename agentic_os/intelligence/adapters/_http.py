"""A tiny injectable JSON HTTP seam so adapters have a real network path AND run offline in tests.

Adapters call ``fetch(method, url, headers, body)`` -> (status, json). Tests inject a fake returning canned
(status, body) tuples, so no adapter test ever hits the network. ``http_get_json`` is kept for the open adapters
that only GET.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

# fetch(method, url, headers, body_json) -> (status, parsed_json)
Fetch = Callable[[str, str, Optional[dict], Optional[dict]], "tuple[int, Any]"]


def http_json(method: str, url: str, headers: Optional[dict] = None,
              body: Optional[dict] = None, timeout: float = 20.0) -> "tuple[int, Any]":
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = dict(headers or {})
    h.setdefault("Accept", "application/json")
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed provider hosts
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            body_err = json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            body_err = {}
        return e.code, body_err


# Back-compat GET seam used by the open adapters: fetch(url, headers) -> (status, json).
def http_get_json(url: str, headers: Optional[dict] = None, timeout: float = 20.0) -> "tuple[int, Any]":
    return http_json("GET", url, headers, None, timeout)

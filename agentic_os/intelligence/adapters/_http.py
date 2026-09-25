"""A tiny injectable JSON-GET seam so adapters have a real network path AND run offline in tests.

Adapters take ``fetch=http_get_json`` and call ``fetch(url, headers)`` -> (status, json). Tests inject a fake
that returns canned (status, body) tuples, so no adapter test ever hits the network.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

Fetch = Callable[[str, Optional[dict]], "tuple[int, Any]"]


def http_get_json(url: str, headers: Optional[dict] = None, timeout: float = 20.0) -> "tuple[int, Any]":
    req = urllib.request.Request(url, headers=headers or {"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed provider hosts
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            body = {}
        return e.code, body

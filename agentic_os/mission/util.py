"""Dependency-free HTTP with an injectable transport.

The kernel stays stdlib-only: `post_json` uses urllib by default, but every client accepts a
`transport` callable `(url, body, headers, timeout) -> dict` so tests inject a fake and no
network (or live GPU / Dagster) is needed to prove the wiring.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

Transport = Callable[[str, dict, dict, float], dict]


class HTTPError(RuntimeError):
    pass


def _urllib_transport(url: str, body: dict, headers: dict, timeout: float) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:  # noqa: PERF203
        raise HTTPError(f"http_{e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from e
    except urllib.error.URLError as e:
        raise HTTPError(f"unreachable: {e}") from e
    return json.loads(raw) if raw.strip() else {}


def post_json(url: str, body: dict, headers: dict | None = None, timeout: float = 60.0,
              transport: Transport | None = None) -> dict:
    return (transport or _urllib_transport)(url, body, headers or {}, timeout)


Fetch = Callable[[str, float], dict]  # GET a URL -> parsed JSON (injectable for tests)


def _urllib_get(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise HTTPError(f"http_{e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from e
    except urllib.error.URLError as e:
        raise HTTPError(f"unreachable: {e}") from e
    return json.loads(raw) if raw.strip() else {}


def get_json(url: str, timeout: float = 30.0, fetch: Fetch | None = None) -> dict:
    return (fetch or _urllib_get)(url, timeout)

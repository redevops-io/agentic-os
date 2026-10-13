"""Per-client-IP token-bucket rate limit for the UNAUTHENTICATED POST routes (`/api/agent`, `/agent/run`,
`/api/permissions/query`, `/api/cases/{id}/respond`).

Those routes drive an LLM, run scanners and mutate case state with no credential, so a single client must not
be able to hammer them. Pure stdlib + a FastAPI dependency; the clock is injectable for tests.

Env:
  SENTINEL_RATE_PER_MIN  sustained refill rate per client IP, default 20 (invalid / <=0 -> default)
  SENTINEL_RATE_BURST    bucket capacity (max burst), default 10
  SENTINEL_TRUST_PROXY   default false. When false the peer address is the client key and ``X-Forwarded-For`` is
                         IGNORED (it is attacker-controlled). When true, the RIGHT-MOST ``X-Forwarded-For`` entry
                         — the one appended by our own trusted reverse proxy — is the client key.
"""
from __future__ import annotations

import math
import os
import threading
import time
from typing import Callable

from fastapi import HTTPException, Request

_MAX_BUCKETS = 10_000


def _env_num(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, ""))
        return v if v > 0 and math.isfinite(v) else default
    except ValueError:
        return default


def trust_proxy() -> bool:
    return os.environ.get("SENTINEL_TRUST_PROXY", "").strip().lower() in ("1", "true", "yes", "on")


def client_ip(request: Request) -> str:
    if trust_proxy():
        xff = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"


class TokenBucketLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}   # key -> (tokens, last_ts)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()

    def check(self, key: str) -> float:
        """Take one token for ``key``. Returns 0.0 when allowed, else the seconds until a token is available."""
        rate = _env_num("SENTINEL_RATE_PER_MIN", 20.0) / 60.0        # tokens / second
        cap = _env_num("SENTINEL_RATE_BURST", 10.0)
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (cap, now))
            tokens = min(cap, tokens + max(0.0, now - last) * rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                allowed = 0.0
            else:
                self._buckets[key] = (tokens, now)
                allowed = (1.0 - tokens) / rate
            if len(self._buckets) > _MAX_BUCKETS:      # bound memory: drop buckets that have fully refilled
                for k in [k for k, (t, ts) in self._buckets.items()
                          if min(cap, t + (now - ts) * rate) >= cap and k != key]:
                    del self._buckets[k]
            return allowed


LIMITER = TokenBucketLimiter()


def rate_limit(request: Request) -> None:
    """FastAPI dependency: 429 + Retry-After when the caller's bucket is empty."""
    wait = LIMITER.check(client_ip(request))
    if wait > 0:
        raise HTTPException(status_code=429, detail="rate limit exceeded",
                            headers={"Retry-After": str(max(1, math.ceil(wait)))})

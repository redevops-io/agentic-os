"""Rate limits and resource quotas for the anonymous demo surfaces.

The unified desktop demo answers without a credential by design — that is what makes the guided journey
work. Two things have to be true for that to be safe to expose, and neither is about identity:

* a caller must not be able to make unbounded work per request, and
* the number of missions the process will create has to be bounded, because a demo mission parks at the
  approval gate and stays there until a human resolves it.

Both are enforced here, and both are enforced **before** anything consequential: a rejected request never
reaches `seed_mission`, so it creates no mission, writes no event, and calls no provider.

Keying follows the rule the route-policy layer uses: the socket peer is the only caller address a process can
trust. `X-Forwarded-For` is consulted only when the operator declares the proxy in front of it
(`UNIFIED_TRUSTED_PROXY_IPS`), and only when the socket really came from that proxy. Both consequences are
intended: a spoofed forwarded header buys an attacker no fresh budget, and behind an *undeclared* same-host
proxy every caller shares one bucket, so limits arrive early rather than never. Collapsing toward stricter
is the acceptable direction of error for a rate limit.
"""
from __future__ import annotations

import ipaddress
import os
import time
from typing import Callable, Dict, Optional, Tuple

ENV_RATE_PER_MIN = "UNIFIED_SIDEKICK_REQUESTS_PER_MIN"
ENV_BURST = "UNIFIED_SIDEKICK_BURST"
ENV_MISSION_LIMIT = "UNIFIED_DEMO_MISSION_LIMIT"
ENV_TRUSTED_PROXY_IPS = "UNIFIED_TRUSTED_PROXY_IPS"

# Defaults for a demo: enough for a person clicking through the journey, far short of a flooding rate.
# The burst is deliberately above what one journey costs rather than at it: the desktop posts to this route
# for the mission and may post again for a follow-up, and a limit that fires inside a legitimate visit is a
# defect, not a defence (the first draft at 3 refused the fourth request of an ordinary visit). The ceiling
# is still real: 20 requests per minute per caller bounds anonymous work long before a flood is useful.
DEFAULT_RATE_PER_MIN = 20
DEFAULT_BURST = 8
DEFAULT_MISSION_LIMIT = 25


class RateLimited(Exception):
    """A request refused before it did any work. `retry_after` is seconds, for the Retry-After header.

    `kind` separates the two reasons so a log reader can tell a busy caller from an exhausted demo: `rate`
    (this caller is going too fast) and `quota` (this process will not create another demo mission).
    """

    def __init__(self, message: str, *, retry_after: int, kind: str = "rate"):
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))
        self.kind = kind


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default                      # a typo falls back to the safe default, never to unlimited
    return value if value > 0 else default


def trusted_proxy_addresses() -> Tuple[ipaddress.ip_address, ...]:
    raw = os.environ.get(ENV_TRUSTED_PROXY_IPS, "")
    out = []
    for token in raw.replace(",", " ").split():
        try:
            out.append(ipaddress.ip_address(token.strip()))
        except ValueError:
            continue                        # an unparseable entry is ignored: it cannot widen anything
    return tuple(out)


def caller_key(scope: Optional[dict]) -> str:
    """The bucket a request lands in: the socket peer, or the forwarded client only when a declared proxy relayed it."""
    peer_raw = ((scope or {}).get("client") or ("", 0))[0] or ""
    try:
        peer = ipaddress.ip_address(peer_raw)
    except ValueError:
        return "peer:unknown"               # no addressable peer: one shared bucket, never an open one
    if peer in trusted_proxy_addresses():
        for name, value in (scope or {}).get("headers") or []:
            key = name.decode() if isinstance(name, bytes) else str(name)
            if key.lower() == "x-forwarded-for":
                first = (value.decode() if isinstance(value, bytes) else str(value)).split(",")[0].strip()
                if first:
                    return f"xff:{first}"
    return f"peer:{peer_raw}"


class DemoLimits:
    """A token bucket per caller plus a cap on demo missions. The clock is injectable so tests are exact.

    Nothing here is a security boundary by itself: it bounds work. What a caller may *do* stays with the
    route policies (agentic_os/app_kit/route_authz.py) and the approval gate.
    """

    def __init__(self, *, per_min: Optional[int] = None, burst: Optional[int] = None,
                 mission_limit: Optional[int] = None, clock: Callable[[], float] = time.monotonic):
        self.per_min = per_min if per_min is not None else _env_int(ENV_RATE_PER_MIN, DEFAULT_RATE_PER_MIN)
        self.burst = burst if burst is not None else _env_int(ENV_BURST, DEFAULT_BURST)
        self.mission_limit = (mission_limit if mission_limit is not None
                              else _env_int(ENV_MISSION_LIMIT, DEFAULT_MISSION_LIMIT))
        self.clock = clock
        self._buckets: Dict[str, Tuple[float, float]] = {}
        self.missions_created = 0

    @property
    def _refill_per_second(self) -> float:
        return self.per_min / 60.0

    def take(self, key: str) -> None:
        """Spend one token for `key`, or raise RateLimited saying how long to wait."""
        now = self.clock()
        tokens, last = self._buckets.get(key, (float(self.burst), now))
        tokens = min(float(self.burst), tokens + (now - last) * self._refill_per_second)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            wait = (1.0 - tokens) / self._refill_per_second
            raise RateLimited("this demo surface is rate limited; send the next request a little later",
                              retry_after=int(wait) + 1)
        self._buckets[key] = (tokens - 1.0, now)

    def check_mission_quota(self, live_missions: int) -> None:
        """Refuse before a mission is created. `live_missions` is what this process is already holding open."""
        if self.missions_created >= self.mission_limit:
            raise RateLimited(
                f"this demo process has created its allowance of {self.mission_limit} missions; it is a demo, "
                "not a multi-tenant backend", retry_after=3600, kind="quota")
        if live_missions >= self.mission_limit:
            raise RateLimited(
                f"{live_missions} demo missions are already awaiting approval; resolve one before starting "
                "another", retry_after=300, kind="quota")

    def mission_created(self) -> None:
        self.missions_created += 1


"""What every provider probe shares: a read-only transport, the mapping of HTTP failures to outcomes, timestamps, and the unique-match rule.

The unique-match rule is the same for every provider that gives no way to tie a record to our request (no idempotency key, no echoed correlation id): a single record that looks
like our effect is a CANDIDATE, not proof, because an unrelated action with the same fields (a person, another agent, a retry from elsewhere) looks identical. By default that is
INCONCLUSIVE with the candidate named for a person to confirm; an operator opts in per adapter where nothing else can produce such a record."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional

from ..probes import ProbeOutcome, ProbeResult, from_http_status

SKEW_S = 120.0                                   # a timestamp slightly before our dispatch time (clock skew) still counts as "since the call"
Http = Callable[[str, dict], "tuple[int, Any, dict]"]   # (path, params) -> (status, parsed JSON or None, response headers); GET only


def epoch(ts: Any) -> Optional[float]:
    if isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return float(ts)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
        except ValueError:
            return None
    return None


def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def httpx_get(base_url: str, headers: dict, allowed: "tuple[str, ...]", timeout: float = 10.0, *, one_segment: bool = False) -> Http:
    """A GET-only transport over httpx (imported lazily). It refuses any path that is not exactly one of `allowed`, or (with `one_segment`) one of them plus a single id segment:
    a probe cannot reach another endpoint through it. GET-only is the guarantee that it cannot write; the path check is defence in depth."""
    import re as _re   # noqa: PLC0415

    def permitted(path: str) -> bool:
        for a in allowed:
            if path == a:
                return True
            if one_segment and path.startswith(a + "/") and _re.fullmatch(r"[A-Za-z0-9_.@-]+", path[len(a) + 1:]):
                return True
        return False

    def http(path: str, params: dict) -> tuple:
        if not permitted(path):
            raise PermissionError(f"probe transport refuses {path}: not a read path")
        import httpx   # noqa: PLC0415
        r = httpx.get(base_url.rstrip("/") + path, params=params, headers=headers, timeout=timeout)
        try:
            body = r.json()
        except ValueError:
            body = None
        return r.status_code, body, dict(r.headers)
    return http


class ProbeBase:
    def __init__(self, http: Http, *, accept_unique_match: bool = False) -> None:
        self.http = http
        self.accept_unique_match = accept_unique_match

    def _get(self, path: str, params: Optional[dict] = None):
        """(status, body, headers) or a TRANSIENT_ERROR ProbeResult when the lookup itself failed."""
        try:
            return self.http(path, params or {})
        except Exception as e:  # noqa: BLE001 - "I could not look" is never "it did not happen"
            return ProbeResult(ProbeOutcome.TRANSIENT_ERROR, detail=f"the lookup failed: {type(e).__name__}: {str(e)[:120]}")

    @staticmethod
    def _bad_status(status: int, headers: dict) -> Optional[ProbeResult]:
        if 200 <= status < 300:
            return None
        if status == 404:
            return ProbeResult(ProbeOutcome.INCONCLUSIVE, detail="not found: a missing record is not proof the action never happened")
        h = {k.lower(): v for k, v in (headers or {}).items()}
        return from_http_status(status, retry_after=h.get("retry-after"))

    @staticmethod
    def _evidence(reference: str, now: float) -> dict:
        return {"kind": "provider_record", "reference": reference[:480], "observed_at": str(now)}

    def _unique_candidate(self, ctx_now: float, ref_text: str, provider_ref: str, what: str, result: dict) -> ProbeResult:
        """Exactly one record looks like our effect and nothing ties it to our request."""
        if not self.accept_unique_match:
            return ProbeResult(ProbeOutcome.INCONCLUSIVE, provider_ref=provider_ref,
                               detail=f"one {what} matches since the call ({ref_text}), but the provider does not tie it to this request: it may be an unrelated one. A person must confirm it before the reservation is settled.")
        return ProbeResult(ProbeOutcome.CONFIRMED_EXECUTED, evidence=self._evidence(f"{what}: {ref_text}", ctx_now), provider_ref=provider_ref, observed_at=ctx_now,
                           result={**result, "reconciled": True},
                           detail=f"exactly one {what} since the call (consistent with the request, not identified by it)")

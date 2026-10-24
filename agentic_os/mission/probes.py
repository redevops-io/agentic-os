"""Reconciliation probes: what an adapter may claim, and what the supervisor will accept.

An unknown outcome is resolved ONLY by positive execution evidence or by an authoritative negative. "Not found" is not a negative: it
means "not observed", and a provider's index may simply not have caught up. The distinction is enforced here, by the supervisor's
registry, not left to each adapter, so a careless adapter cannot turn uncertainty into permission to resend.

An adapter returns a structured `ProbeResult`, never a boolean:

  CONFIRMED_EXECUTED      the effect exists (evidence + provenance; the provider reference; ideally the real result)
  CONFIRMED_NOT_EXECUTED  the effect provably does not exist. Accepted ONLY when the adapter was registered as qualified to make an
                          authoritative negative determination, the result says so, evidence is attached, and the provider's
                          visibility window has elapsed. Otherwise it is downgraded to INCONCLUSIVE.
  INCONCLUSIVE            not observed / cannot tell. Retried (read-only) with backoff.
  TRANSIENT_ERROR         the lookup itself failed (timeout, 429, 5xx). Retried with backoff, honouring Retry-After.
  UNSUPPORTED             this provider cannot support reliable reconciliation. Escalated immediately.

A generic HTTP 404 is INCONCLUSIVE, always (`from_http_status`).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatchcase
from typing import Any, Callable, Optional

from .reconcile import ReconciliationError, validate_evidence


class ProbeOutcome(str, Enum):
    CONFIRMED_EXECUTED = "confirmed_executed"
    CONFIRMED_NOT_EXECUTED = "confirmed_not_executed"
    INCONCLUSIVE = "inconclusive"
    TRANSIENT_ERROR = "transient_error"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ProbeResult:
    outcome: ProbeOutcome
    evidence: Optional[dict] = None            # {kind, reference, ...}: provenance of the observation
    provider_ref: str = ""
    observed_at: float = 0.0
    result: Optional[dict] = None              # the provider's real result (restored into the mission on CONFIRMED_EXECUTED)
    retry_after_s: Optional[float] = None      # a provider's Retry-After, honoured (bounded) by the scheduler
    detail: str = ""
    authoritative_negative: bool = False       # the adapter asserts THIS negative is authoritative (necessary, never sufficient)


@dataclass(frozen=True)
class ProbeContext:
    lease: dict                                 # the durable reservation record (meta: idempotency key, capability, mission, ...)
    attempt: int                                # which probe this is (1-based)
    now: float
    dispatched_at: float = 0.0
    effect_ref: "dict | None" = None            # the few input fields that identify the effect at the provider (recorded at dispatch; never secrets)


Probe = Callable[[ProbeContext], ProbeResult]


@dataclass(frozen=True)
class Qualification:
    """What the adapter's owner declares about the provider's lookup. The defaults are the conservative ones."""
    name: str
    eventually_consistent: bool = True          # a fresh effect may not be visible yet
    visibility_window_s: Optional[float] = None  # documented maximum delay before a fresh effect is visible (None = unknown)
    authoritative_negative: bool = False        # a negative from this adapter can prove the effect did not happen
    supports_lookup: bool = True


@dataclass(frozen=True)
class Verdict:
    """The supervisor's reading of an adapter's result after enforcement. `outcome` may differ from what the adapter returned."""
    outcome: ProbeOutcome
    reason: str
    downgraded: bool = False
    result: Optional[ProbeResult] = None
    adapter: str = ""


class ProbeRegistry:
    """Adapters by capability pattern, with their declared qualification, and the rules that judge their answers."""

    def __init__(self) -> None:
        self._adapters: "list[tuple[str, Qualification, Probe]]" = []
        self._refs: "list[tuple[str, tuple]]" = []

    def register(self, capability_pattern: str, probe: Probe, qualification: Qualification, ref_keys: tuple = ()) -> None:
        """`ref_keys` names the input fields that identify the effect at the provider (a customer id, an invoice number, a message id). They are copied into the
        reservation when the call is dispatched, so a probe still knows WHAT to look for after a restart, without the mission's inputs."""
        self._adapters.append((capability_pattern, qualification, probe))
        self._refs.append((capability_pattern, tuple(ref_keys)))

    def ref_keys(self, capability: str) -> tuple:
        out: list = []
        for pat, keys in self._refs:
            if fnmatchcase(capability, pat):
                out += [k for k in keys if k not in out]
        return tuple(out)

    def effect_ref(self, capability: str, inputs: "dict | None") -> dict:
        """The identifying subset of `inputs` for this capability: scalar values only, bounded in size, never a key that looks like a secret."""
        ref: dict = {}
        for k in self.ref_keys(capability):
            v = (inputs or {}).get(k)
            if isinstance(v, (str, int, float, bool)) and not any(w in k.lower() for w in ("secret", "token", "password", "key")):
                ref[k] = v[:200] if isinstance(v, str) else v
        return ref

    def adapters_for(self, capability: str) -> "list[tuple[Qualification, Probe]]":
        return [(q, p) for pat, q, p in self._adapters if fnmatchcase(capability, pat)]

    def supported(self, capability: str) -> bool:
        return any(q.supports_lookup for q, _ in self.adapters_for(capability))

    def horizon_floor(self, capability: str, dispatched_at: float) -> float:
        """No probe before this time can say anything useful: the latest documented visibility window of any adapter."""
        floors = [dispatched_at + q.visibility_window_s for q, _ in self.adapters_for(capability)
                  if q.eventually_consistent and q.visibility_window_s]
        return max(floors) if floors else 0.0

    def judge(self, q: Qualification, res: Any, *, dispatched_at: float, now: float) -> Verdict:
        """Enforce the evidence contract on one adapter's answer. Anything the adapter was not entitled to claim is downgraded."""
        if not isinstance(res, ProbeResult):
            return Verdict(ProbeOutcome.INCONCLUSIVE, "the adapter returned no structured result", True, None, q.name)
        out = res.outcome
        if out is ProbeOutcome.CONFIRMED_EXECUTED:
            try:
                validate_evidence(res.evidence)
            except ReconciliationError as e:
                return Verdict(ProbeOutcome.INCONCLUSIVE, f"positive result without usable evidence provenance: {e}", True, res, q.name)
            return Verdict(out, res.detail or "the provider has the effect", False, res, q.name)
        if out is ProbeOutcome.CONFIRMED_NOT_EXECUTED:
            if not q.authoritative_negative:
                return Verdict(ProbeOutcome.INCONCLUSIVE, f"{q.name} is not qualified to prove an effect absent: 'not found' is 'not observed'",
                               True, res, q.name)
            if not res.authoritative_negative:
                return Verdict(ProbeOutcome.INCONCLUSIVE, "the adapter did not mark this negative as authoritative", True, res, q.name)
            try:
                validate_evidence(res.evidence)
            except ReconciliationError as e:
                return Verdict(ProbeOutcome.INCONCLUSIVE, f"negative result without usable evidence provenance: {e}", True, res, q.name)
            if q.eventually_consistent:
                if not q.visibility_window_s:
                    return Verdict(ProbeOutcome.INCONCLUSIVE, f"{q.name} is eventually consistent with no documented visibility window: "
                                   "an absence cannot yet be trusted", True, res, q.name)
                if now < dispatched_at + q.visibility_window_s:
                    return Verdict(ProbeOutcome.INCONCLUSIVE, "inside the provider's visibility window: the effect may not be visible yet",
                                   True, res, q.name)
            return Verdict(out, res.detail or "authoritatively absent", False, res, q.name)
        if out is ProbeOutcome.UNSUPPORTED:
            return Verdict(out, res.detail or "this provider cannot support reliable reconciliation", False, res, q.name)
        if out is ProbeOutcome.TRANSIENT_ERROR:
            return Verdict(out, res.detail or "the lookup failed", False, res, q.name)
        return Verdict(ProbeOutcome.INCONCLUSIVE, res.detail or "not observed", False, res, q.name)


def from_http_status(status: int, *, retry_after: Optional[str] = None, detail: str = "") -> ProbeResult:
    """The ONLY sanctioned mapping from a bare HTTP status to a probe result. Notably 404 is INCONCLUSIVE: a generic 'not found' is
    never an authoritative negative. A 2xx needs the adapter to parse the body and build its own CONFIRMED_EXECUTED."""
    if status == 429 or status >= 500 or status in (408, 425):
        ra = None
        if retry_after:
            try:
                ra = max(0.0, float(retry_after))
            except ValueError:
                ra = None
        return ProbeResult(ProbeOutcome.TRANSIENT_ERROR, retry_after_s=ra, detail=detail or f"provider answered {status}")
    if status in (401, 403):
        return ProbeResult(ProbeOutcome.UNSUPPORTED, detail=f"the lookup is not authorised ({status}): reliable reconciliation is not possible")
    return ProbeResult(ProbeOutcome.INCONCLUSIVE, detail=detail or f"provider answered {status}: not observed")

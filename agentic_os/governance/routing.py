"""GovernedModelRouter — fail-closed model routing by data classification (Private Data Plane plan §4).

Policy: business/customer reasoning over private data → an approved model INSIDE the trust boundary ONLY; coding/
infra over ENGINEERING/PUBLIC context → an approved external frontier model is permitted; no compliant route →
FAIL CLOSED (never a silent fallback to public inference). Possessing a provider API key does NOT bypass this — the
router only routes to registered, approved endpoints. Pure + deterministic; every decision is explainable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional, Sequence, Tuple

from .classification import DataClassification, EXTERNAL_MAX, externally_shareable, max_classification
from .policy import DEFAULT_MODE, PrivacyMode, privacy_notice


class ExecutionBoundary(str, Enum):
    IN_BOUNDARY = "in_boundary"      # customer cloud/on-prem private inference
    EXTERNAL = "external"            # external frontier provider (engineering assistance plane only)


class TaskClass(str, Enum):
    BUSINESS_REASONING = "business_reasoning"   # over customer/business data — private only
    CODING = "coding"
    INFRA = "infra"
    PUBLIC_QA = "public_qa"
    VERIFICATION = "verification"


class RoutingRefused(Exception):
    """Raised when no compliant model route exists — the fail-closed outcome."""


@dataclass(frozen=True)
class ModelEndpoint:
    """A registered, approved model route. ``accepts`` is the highest classification it may receive."""
    model_id: str
    provider: str
    boundary: ExecutionBoundary
    accepts: DataClassification = DataClassification.SECRET
    deployment: str = ""
    network_route: str = ""
    external_data_processor: bool = False
    capabilities: Tuple[str, ...] = ()
    approved: bool = True


@dataclass(frozen=True)
class ModelRequest:
    """A request to run inference. ``classifications`` are the data classes present in the evidence."""
    task: str
    task_class: TaskClass
    classifications: Tuple[DataClassification, ...] = ()
    required_capabilities: Tuple[str, ...] = ()
    tenant: str = ""
    evidence_refs: Tuple[str, ...] = ()

    @property
    def max_classification(self) -> DataClassification:
        return max_classification(self.classifications)


@dataclass(frozen=True)
class RoutingDecision:
    permitted: bool
    endpoint: Optional[ModelEndpoint]
    max_classification: DataClassification
    reason: str
    privacy_preserved: bool = True     # False iff private data was routed to an external processor (OPEN mode only)
    considered: int = 0


class GovernedModelRouter:
    """Routes a ModelRequest to a compliant endpoint, or refuses (fail-closed). ``mode`` gates whether external
    providers may receive data: STRICT_PRIVATE (never), PRIVATE_WITH_ENGINEERING_ASSIST (only ENGINEERING/PUBLIC),
    OPEN (anything — privacy NOT guaranteed, an explicit deliberate choice)."""

    def __init__(self, endpoints: Iterable[ModelEndpoint] = (), *, mode: PrivacyMode = DEFAULT_MODE) -> None:
        self._endpoints: list[ModelEndpoint] = list(endpoints)
        self.mode = mode

    def register(self, endpoint: ModelEndpoint) -> None:
        self._endpoints.append(endpoint)

    def _eligible(self, req: ModelRequest) -> list[ModelEndpoint]:
        maxc = req.max_classification
        need = set(req.required_capabilities)
        out = []
        for e in self._endpoints:
            if not e.approved:
                continue
            if need and not need.issubset(set(e.capabilities)):
                continue
            if maxc.rank > e.accepts.rank:                       # endpoint may not receive this classification
                continue
            if e.boundary is ExecutionBoundary.EXTERNAL:
                if self.mode is PrivacyMode.STRICT_PRIVATE:
                    continue                                     # no external at all
                if self.mode is PrivacyMode.PRIVATE_WITH_ENGINEERING_ASSIST and not externally_shareable(maxc):
                    continue                                     # private data can never leave the boundary
                # OPEN: external permitted for ANY classification (privacy not guaranteed) — see route() flag
            out.append(e)
        # deterministic preference: in-boundary first (safest), then by model_id
        out.sort(key=lambda e: (0 if e.boundary is ExecutionBoundary.IN_BOUNDARY else 1, e.model_id))
        return out

    def route(self, req: ModelRequest) -> RoutingDecision:
        maxc = req.max_classification
        eligible = self._eligible(req)
        if not eligible:
            return RoutingDecision(permitted=False, endpoint=None, max_classification=maxc,
                                   reason=f"no compliant route for task_class={req.task_class.value} at "
                                          f"classification={maxc.value} in mode={self.mode.value} "
                                          f"(fail-closed; no public fallback)",
                                   considered=len(self._endpoints))
        chosen = eligible[0]
        preserved = chosen.boundary is ExecutionBoundary.IN_BOUNDARY or externally_shareable(maxc)
        reason = f"routed to {chosen.model_id} ({chosen.boundary.value}) for {maxc.value} in mode={self.mode.value}"
        if not preserved:
            reason += " — WARNING: private data sent to an external provider; data privacy is NOT guaranteed (OPEN mode)"
        return RoutingDecision(permitted=True, endpoint=chosen, max_classification=maxc,
                               reason=reason, privacy_preserved=preserved, considered=len(self._endpoints))

    def require_route(self, req: ModelRequest) -> ModelEndpoint:
        """Return the chosen endpoint or raise RoutingRefused — for callers that must fail closed."""
        d = self.route(req)
        if not d.permitted or d.endpoint is None:
            raise RoutingRefused(d.reason)
        return d.endpoint


__all__ = [
    "ExecutionBoundary", "TaskClass", "RoutingRefused", "ModelEndpoint", "ModelRequest",
    "RoutingDecision", "GovernedModelRouter",
]

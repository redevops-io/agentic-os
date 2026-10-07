"""Service Fulfillment Intelligence — plan and verify delivered services (plan §18, P7).

The audit found service planning absent. This adds the ServicePlan object + a deterministic feasibility check
(do we have the skills/capacity by the deadline?) and reuses the generic reliability engine for the service
lifecycle (requested → scheduled → in_progress → completed → verified). Extends Agentic Apps beyond product/order
businesses (implementation, installation, field/professional services, onboarding, training, managed service).
Pure + deterministic; real calendars/capacity and scheduling Missions bind in the enterprise overlay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence, Tuple

from ..reliability import ExpectedTransition

SERVICE_STAGES: Tuple[str, ...] = ("requested", "scheduled", "in_progress", "completed", "verified")


@dataclass(frozen=True)
class ServiceRequirement:
    skill: str
    quantity: int = 1


@dataclass(frozen=True)
class ServiceResource:
    resource_id: str
    skills: Tuple[str, ...] = ()
    capacity: int = 1
    available_from: int = 0
    available_until: int = 0            # 0 = open-ended


@dataclass(frozen=True)
class ServiceFeasibility:
    feasible: bool
    matched: Tuple[Tuple[str, str], ...] = ()      # (skill, resource_id)
    missing_skills: Tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class ServicePlan:
    customer_commitment: str
    requested_service: str = ""
    required_by: int = 0
    requirements: Tuple[ServiceRequirement, ...] = ()
    assigned: Tuple[Tuple[str, str], ...] = ()      # (skill, resource_id)
    location: str = ""
    dependencies: Tuple[str, ...] = ()
    sla: str = ""
    risks: Tuple[str, ...] = ()
    evidence: Tuple[str, ...] = ()
    feasible: bool = False


def assess_service_feasibility(requirements: Sequence[ServiceRequirement], resources: Sequence[ServiceResource], *,
                               required_by: int = 0) -> ServiceFeasibility:
    """Greedily match each required skill-unit to a resource with capacity available by ``required_by``.
    Deterministic (resources sorted by id). Reports missing skills when under-resourced."""
    remaining = {r.resource_id: r.capacity for r in sorted(resources, key=lambda r: r.resource_id)}
    by_id = {r.resource_id: r for r in resources}
    matched, missing = [], []
    for req in requirements:
        for _ in range(req.quantity):
            pick = None
            # prefer the LEAST-versatile capable resource (keep multi-skill resources free for scarcer skills),
            # then most remaining capacity, then id — deterministic.
            for rid in sorted(remaining, key=lambda i: (len(by_id[i].skills), -remaining[i], i)):
                r = by_id[rid]
                in_window = (required_by == 0) or (r.available_until == 0) or (r.available_from <= required_by)
                if remaining[rid] > 0 and req.skill in r.skills and in_window:
                    pick = rid
                    break
            if pick is None:
                missing.append(req.skill)
            else:
                remaining[pick] -= 1
                matched.append((req.skill, pick))
    feasible = not missing
    reason = "all requirements met" if feasible else f"unmet: {', '.join(sorted(set(missing)))}"
    return ServiceFeasibility(feasible=feasible, matched=tuple(matched),
                              missing_skills=tuple(sorted(set(missing))), reason=reason)


def plan_service(customer_commitment: str, requested_service: str, requirements: Sequence[ServiceRequirement],
                 resources: Sequence[ServiceResource], *, required_by: int = 0, location: str = "",
                 sla: str = "") -> ServicePlan:
    feas = assess_service_feasibility(requirements, resources, required_by=required_by)
    risks = () if feas.feasible else (f"under-resourced: {feas.reason}",)
    return ServicePlan(customer_commitment=customer_commitment, requested_service=requested_service,
                       required_by=required_by, requirements=tuple(requirements), assigned=feas.matched,
                       location=location, sla=sla, risks=risks, feasible=feas.feasible)


def service_expected_transitions(plan: ServicePlan, *, stage_deadlines: Mapping[str, int] = None) -> Tuple[ExpectedTransition, ...]:
    deadlines = dict(stage_deadlines or {})
    out = []
    for i in range(1, len(SERVICE_STAGES)):
        frm, to = SERVICE_STAGES[i - 1], SERVICE_STAGES[i]
        out.append(ExpectedTransition(subject=f"service:{plan.customer_commitment}:{to}", expected_to_state=to,
                                      from_state=frm, expected_by=deadlines.get(to, plan.required_by),
                                      failure_states=("cancelled", "failed"), remediation_policy=f"remediate_{to}"))
    return tuple(out)


__all__ = ["SERVICE_STAGES", "ServiceRequirement", "ServiceResource", "ServiceFeasibility", "ServicePlan",
           "assess_service_feasibility", "plan_service", "service_expected_transitions"]

"""Integration plane — the Obligation engine (Phase 0, §8).

Traditional automation says "the API call succeeded." This says "the expected business state was independently
observed." The engine: attempt execution → verify the destination by read-back → classify.

    satisfied   → receipt
    absent      → retry; still absent after the policy → OBLIGATION_UNSATISFIED exception (the silent-failure catch)
    conflicting → OBLIGATION_CONFLICT exception (destination shows a different value)
    overdue     → OBLIGATION_OVERDUE exception

The engine never executes side effects itself — the caller passes an idempotent ``action`` and the destination
provider; the engine owns verification, classification, the receipt, and the exception. This is what turns a
pile of connectors into a plane that PROVES cross-system outcomes.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Optional

from .contracts import (
    ExceptionCategory, IntegrationException, IntegrationReceipt, Obligation, ObligationStatus, _now,
)
from .provider import ActionResult, IntegrationError, IntegrationProvider

# action() performs the (idempotent) write and returns its ActionResult; raising IntegrationError triggers a retry.
Action = Callable[[], ActionResult]


@dataclass
class DischargeResult:
    obligation: Obligation                      # the updated (append-only) revision
    receipt: Optional[IntegrationReceipt] = None
    exception: Optional[IntegrationException] = None

    @property
    def satisfied(self) -> bool:
        return self.obligation.status == ObligationStatus.SATISFIED


def _check(expected: dict[str, dict[str, Any]], destination: IntegrationProvider,
           targets: Mapping[str, str]) -> "tuple[dict, list, list]":
    """Read the destination for each expected object and compare. Returns (observed, missing, conflicts)."""
    observed: dict[str, Any] = {}
    missing: list[str] = []
    conflicts: list[str] = []
    for object_type, expected_fields in expected.items():
        ext = targets.get(object_type, "")
        obs = destination.verify_action(object_type, ext) if ext else None
        if obs is None:
            missing.append(object_type)                      # the whole object never landed (silent failure)
            continue
        observed[object_type] = dict(obs.normalized_fields)
        for field_name, want in expected_fields.items():
            got = obs.normalized_fields.get(field_name, _MISSING)
            if got is _MISSING:
                missing.append(f"{object_type}.{field_name}")
            elif got != want:
                conflicts.append(f"{object_type}.{field_name}={got!r}≠{want!r}")
    return observed, missing, conflicts


_MISSING = object()


class ObligationEngine:
    """Discharges obligations against a destination provider by read-after-write verification."""

    def discharge(self, obligation: Obligation, destination: IntegrationProvider, *, action: Action,
                  targets: Mapping[str, str], authority: str = "owner",
                  now: Optional[str] = None) -> DischargeResult:
        now = now or _now()
        attempts = 0
        last_detail = ""
        observed: dict[str, Any] = {}
        missing: list[str] = []
        conflicts: list[str] = []
        max_attempts = max(1, obligation.retry_policy.max_attempts)

        while attempts < max_attempts:
            attempts += 1
            try:
                action()                                     # idempotent write (deduped by the provider)
            except IntegrationError as e:
                last_detail = str(e)
                continue                                     # provider error → retry
            observed, missing, conflicts = _check(obligation.expected_state, destination, targets)
            if not missing and not conflicts:
                obl = replace(obligation, status=ObligationStatus.SATISFIED, attempts=attempts)
                receipt = IntegrationReceipt(
                    obligation_id=obligation.obligation_id, trigger=obligation.trigger,
                    action=_action_label(obligation), satisfied=True, observed_state=observed,
                    authority=authority, evidence_refs=obligation.entity_refs)
                return DischargeResult(obl, receipt=receipt)
            # not satisfied yet — loop and retry

        # exhausted → classify the failure as a durable exception
        if obligation.deadline and now > obligation.deadline:
            category, status = ExceptionCategory.OBLIGATION_OVERDUE, ObligationStatus.FAILED
            detail = f"deadline {obligation.deadline} exceeded; missing={missing} conflicts={conflicts}"
        elif conflicts:
            category, status = ExceptionCategory.OBLIGATION_CONFLICT, ObligationStatus.AMBIGUOUS
            detail = f"destination conflicts: {conflicts}"
        elif missing:
            category, status = ExceptionCategory.OBLIGATION_UNSATISFIED, ObligationStatus.FAILED
            detail = f"expected state never observed (silent failure): missing={missing}"
        else:
            category, status = ExceptionCategory.ACTION_FAILED, ObligationStatus.FAILED
            detail = last_detail or "action failed before verification"

        obl = replace(obligation, status=status, attempts=attempts)
        exc = IntegrationException(
            category=category, obligation_id=obligation.obligation_id, workflow_id=obligation.workflow_id,
            affected_entities=obligation.entity_refs, detail=detail,
            recommended_action=_recommend(category), retry_count=attempts,
            last_attempt=now)
        return DischargeResult(obl, exception=exc)


def _action_label(obligation: Obligation) -> str:
    return f"{obligation.source_resource or obligation.trigger} → {obligation.destination_resource or 'destination'}"


def _recommend(category: ExceptionCategory) -> str:
    return {
        ExceptionCategory.OBLIGATION_UNSATISFIED: "replay the destination write from the failed node; check the connector/credential",
        ExceptionCategory.OBLIGATION_CONFLICT: "reconcile the destination value against the source evidence (human review)",
        ExceptionCategory.OBLIGATION_OVERDUE: "escalate to the workflow owner; the handoff missed its SLA",
        ExceptionCategory.ACTION_FAILED: "inspect the provider error; retry after fixing auth/validation",
    }.get(category, "review the exception evidence")

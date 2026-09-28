"""Flagship B2B quote-to-order composition (Revenue & Execution Intelligence plan §14).

Ties the pieces already built into the plan's flagship pre-mission step — *construct quote plan*:

    Chatwoot message → detect QUOTE_REQUEST (intent)          [intent.classify_intent]
    → resolve the customer                                    [business.resolve_entity]
    → assess feasibility + approvals over the catalog         [revenue.assess_quote_feasibility]
    → a governed QuotePlan ready to stage for approval

Governance is enforced at composition time: a low-confidence / non-quote intent becomes an *observation
only* (§7), and an entity that isn't RESOLVED (or a policy-approved PROBABLE) can never drive a consequential
draft (§12). Creating a quote + updating CRM is consequential, so the plan always carries `approval_required`
— the Mission Runtime gate applies it. Pure + deterministic; wiring the real Chatwoot/ERPNext/Twenty clients
and opening the actual Mission is the sensor/mission slice that follows.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Mapping, Optional

from agentic_os.integrations.business.entity_resolution import EntityMatch, EntityQuery, resolve_entity
from agentic_os.revenue.intent import IntentClassification, classify_intent
from agentic_os.revenue.quote import CatalogItem, QuoteFeasibility, QuoteLine, assess_quote_feasibility


@dataclass(frozen=True)
class QuotePlan:
    ready: bool                                  # a governed, feasible draft quote is ready to stage
    reason: str
    intent: IntentClassification
    entity: Optional[EntityMatch] = None
    feasibility: Optional[QuoteFeasibility] = None
    approval_required: bool = True               # creating a quote / CRM write is consequential

    def as_dict(self) -> dict:
        out: dict = {
            "ready": self.ready, "reason": self.reason, "approval_required": self.approval_required,
            "intent": {"intent": self.intent.intent.value, "confidence": self.intent.confidence,
                       "entities": self.intent.entities, "source_ref": self.intent.source_ref},
        }
        if self.entity is not None:
            out["entity"] = {"state": self.entity.state.value, "ref": self.entity.entity_ref,
                             "matched_on": self.entity.matched_on, "confidence": self.entity.confidence}
        if self.feasibility is not None:
            out["draft_quote_plan"] = self.feasibility.draft_quote_plan()
        return out


def plan_quote_from_request(text: str, *, entity_query: EntityQuery, candidate_accounts, lines: List[QuoteLine],
                            catalog: Mapping[str, CatalogItem], now_ms: int, source: str = "chatwoot",
                            source_ref: str = "", min_intent_confidence: float = 0.6,
                            allow_probable_entity: bool = False, **quote_kwargs) -> QuotePlan:
    """Compose intent → entity resolution → quote feasibility into a governed QuotePlan (§14).

    `quote_kwargs` pass through to assess_quote_feasibility (min_margin_pct, approval_over_cents,
    customer_discount_pct, currency).
    """
    intent = classify_intent(text, source=source, source_ref=source_ref, observed_at_ms=now_ms)
    if not intent.is_actionable_quote or intent.confidence < min_intent_confidence:
        return QuotePlan(ready=False, intent=intent, approval_required=False,
                         reason=(f"observation only: intent {intent.intent.value} "
                                 f"@ {intent.confidence:.2f} (< {min_intent_confidence:.2f} or not a quote)"))

    entity = resolve_entity(entity_query, candidate_accounts)
    if not entity.may_drive_action(allow_probable=allow_probable_entity):
        return QuotePlan(ready=False, intent=intent, entity=entity, approval_required=False,
                         reason=f"customer identity {entity.state.value} — cannot auto-draft a quote (§12)")

    feas = assess_quote_feasibility(lines, catalog, now_ms=now_ms, **quote_kwargs)
    if not feas.feasible:
        return QuotePlan(ready=False, intent=intent, entity=entity, feasibility=feas, approval_required=True,
                         reason="not feasible: " + "; ".join(feas.blockers[:4]))
    return QuotePlan(ready=True, intent=intent, entity=entity, feasibility=feas, approval_required=True,
                     reason="governed draft quote ready to stage for approval")

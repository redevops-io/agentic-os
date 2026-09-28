"""Commercial intent detection (Revenue & Execution Intelligence plan §7).

Deterministic, explainable classification of an inbound message (Chatwoot conversation, email, …) into the
§7 taxonomy, preserving source / source ref / timestamp / confidence / extracted entities / evidence. Start
keyword-deterministic (§21) — a learned model only re-weights later. Per §7, a low-confidence classification
should become an *observation*, not a Mission; this returns the confidence and lets the caller gate.

`QUOTE_REQUEST` is treated as a composite: a request that names a quantity AND asks about price/supply is a
quote request even if it never says the word "quote" — that's the flagship trigger (§14).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Tuple


class CommercialIntent(str, Enum):
    BUY_INTENT = "BUY_INTENT"
    QUOTE_REQUEST = "QUOTE_REQUEST"
    PRICING_REQUEST = "PRICING_REQUEST"
    AVAILABILITY_REQUEST = "AVAILABILITY_REQUEST"
    DELIVERY_REQUEST = "DELIVERY_REQUEST"
    UPSELL_INTENT = "UPSELL_INTENT"
    CROSS_SELL_INTENT = "CROSS_SELL_INTENT"
    RENEWAL_CONCERN = "RENEWAL_CONCERN"
    CHURN_RISK = "CHURN_RISK"
    SUPPORT_ONLY = "SUPPORT_ONLY"
    IMPLEMENTATION_BLOCKER = "IMPLEMENTATION_BLOCKER"
    PROCUREMENT_BLOCKER = "PROCUREMENT_BLOCKER"
    COMPLIANCE_REQUEST = "COMPLIANCE_REQUEST"
    SECURITY_REQUEST = "SECURITY_REQUEST"
    PRODUCT_GAP = "PRODUCT_GAP"
    COMPETITOR_MENTION = "COMPETITOR_MENTION"
    GENERAL = "GENERAL"


@dataclass(frozen=True)
class IntentClassification:
    intent: CommercialIntent
    confidence: float
    source: str = "chatwoot"
    source_ref: str = ""
    observed_at_ms: int = 0
    matched_terms: Tuple[str, ...] = ()
    entities: Dict[str, object] = field(default_factory=dict)   # {quantity, has_date, price_terms}
    evidence: Tuple[str, ...] = ()

    @property
    def is_actionable_quote(self) -> bool:
        return self.intent in (CommercialIntent.QUOTE_REQUEST, CommercialIntent.BUY_INTENT)


# term lexicons per intent (lowercase substrings); order is priority for ties (earlier = stronger)
_LEX: Tuple[Tuple[CommercialIntent, Tuple[str, ...]], ...] = (
    (CommercialIntent.CHURN_RISK, ("cancel", "cancelling", "canceling", "terminate", "leaving", "switch away",
                                   "unhappy", "disappointed", "refund")),
    (CommercialIntent.RENEWAL_CONCERN, ("renew", "renewal", "contract end", "expire", "expiring")),
    (CommercialIntent.PRICING_REQUEST, ("price", "pricing", "how much", "cost", "quote", "quotation",
                                        "discount", "contract pricing")),
    (CommercialIntent.AVAILABILITY_REQUEST, ("in stock", "availability", "available", "do you have", "supply")),
    (CommercialIntent.DELIVERY_REQUEST, ("deliver", "delivery", "ship", "lead time", "by when", "eta")),
    (CommercialIntent.BUY_INTENT, ("buy", "purchase", "order", "place an order", "procure")),
    (CommercialIntent.UPSELL_INTENT, ("upgrade", "more seats", "add seats", "expand", "higher plan", "tier up")),
    (CommercialIntent.CROSS_SELL_INTENT, ("also need", "other product", "bundle", "add-on")),
    (CommercialIntent.PROCUREMENT_BLOCKER, ("po number", "purchase order", "vendor form", "procurement",
                                            "supplier onboarding")),
    (CommercialIntent.IMPLEMENTATION_BLOCKER, ("can't get", "cannot get", "not working", "blocked", "stuck",
                                               "integration issue", "setup issue")),
    (CommercialIntent.COMPLIANCE_REQUEST, ("gdpr", "compliance", "dpa", "data processing", "soc 2", "soc2", "hipaa")),
    (CommercialIntent.SECURITY_REQUEST, ("security", "pentest", "penetration test", "vulnerability", "vuln",
                                         "iso 27001")),
    (CommercialIntent.COMPETITOR_MENTION, ("competitor", "vs ", "compared to", "alternative to", "instead of")),
    (CommercialIntent.PRODUCT_GAP, ("does it support", "feature request", "wish it could", "missing feature")),
    (CommercialIntent.SUPPORT_ONLY, ("help", "issue", "bug", "error", "how do i", "question")),
)

_QTY = re.compile(r"\b(\d{1,7})\s*(units?|seats?|licen[cs]es?|pcs?|items?)\b", re.I)
_QTY_BARE = re.compile(r"\b(\d{2,7})\b")
_DATE = re.compile(r"\b(by|before|on)\s+\w+\s*\d{0,2}|\b\d{4}-\d{2}-\d{2}\b|"
                   r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}\b", re.I)
_PRICE_TERMS = ("price", "pricing", "quote", "quotation", "cost", "how much", "contract pricing", "discount")
_SUPPLY_TERMS = ("supply", "units", "in stock", "available", "deliver", "order", "buy")


def classify_intent(text: str, *, source: str = "chatwoot", source_ref: str = "",
                    observed_at_ms: int = 0) -> IntentClassification:
    """Classify one message. Deterministic: score each intent by lexicon hits (earlier lexicons win ties),
    with a composite QUOTE_REQUEST override when a quantity co-occurs with a price/supply ask."""
    t = (text or "").lower()

    # entity extraction (quantity / date / price terms) — carried on the classification for downstream steps
    qty_m = _QTY.search(t) or _QTY_BARE.search(t)
    quantity = int(qty_m.group(1)) if qty_m else None
    has_date = bool(_DATE.search(t))
    price_hit = [w for w in _PRICE_TERMS if w in t]
    entities: Dict[str, object] = {"quantity": quantity, "has_date": has_date, "price_terms": price_hit}

    # composite QUOTE_REQUEST: a quantity + a price/supply ask is a quote request (the §14 flagship trigger)
    if quantity is not None and (price_hit or any(w in t for w in _SUPPLY_TERMS)):
        terms = tuple(price_hit) + (f"qty:{quantity}",) + (("date",) if has_date else ())
        conf = round(min(0.95, 0.7 + 0.05 * len(terms)), 3)
        return IntentClassification(CommercialIntent.QUOTE_REQUEST, conf, source, source_ref, observed_at_ms,
                                    matched_terms=terms, entities=entities,
                                    evidence=(f"quantity {quantity} + price/supply ask",))

    best_intent, best_hits, best_terms = CommercialIntent.GENERAL, 0, ()
    for intent, terms in _LEX:
        hits = [w for w in terms if w in t]
        if len(hits) > best_hits:
            best_intent, best_hits, best_terms = intent, len(hits), tuple(hits)
    if best_hits == 0:
        return IntentClassification(CommercialIntent.GENERAL, 0.3, source, source_ref, observed_at_ms,
                                    entities=entities, evidence=("no commercial-intent terms matched",))
    conf = round(min(0.9, 0.55 + 0.15 * best_hits), 3)
    return IntentClassification(best_intent, conf, source, source_ref, observed_at_ms,
                                matched_terms=best_terms, entities=entities,
                                evidence=(f"matched {best_terms}",))

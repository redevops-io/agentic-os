"""Built-in closing methodologies, expressed purely as config (Phase 2, plan §9).

These exist to PROVE the point that a methodology is data, not code: MEDDICC and SPICED are two very different
sales motions, yet both compile to the same :class:`~agentic_os.deal_closing.methodology.ClosingMethodology`
shape over the shared normalized condition vocabulary, and the readiness engine reasons over them identically. A
customer's own motion (a procurement checklist, a founder-led close, a recruiting pipeline) is added the same
way — by registering another config, not by changing the engine.
"""
from __future__ import annotations

from typing import Dict

from .methodology import ClosingMethodology, ConditionSpec, compile_methodology


def meddicc() -> ClosingMethodology:
    """MEDDICC mapped onto the normalized condition vocabulary over a generic B2B stage flow."""
    stages = ("DISCOVER", "QUALIFY", "VALIDATE", "PROPOSE", "NEGOTIATE", "CLOSE")
    conditions = (
        ConditionSpec("BUSINESS_PROBLEM_CONFIRMED", stage="DISCOVER"),            # Identify pain
        ConditionSpec("METRICS_QUANTIFIED", stage="DISCOVER"),                    # Metrics
        ConditionSpec("CHAMPION_IDENTIFIED", stage="QUALIFY"),                    # Champion
        ConditionSpec("ECONOMIC_BUYER_IDENTIFIED", stage="QUALIFY"),              # Economic buyer
        ConditionSpec("DECISION_CRITERIA_KNOWN", stage="QUALIFY"),                # Decision criteria
        ConditionSpec("DECISION_PROCESS_MAPPED", stage="VALIDATE"),               # Decision process
        ConditionSpec("COMPETITION_UNDERSTOOD", stage="VALIDATE"),                # Competition
        ConditionSpec("CHAMPION_ACTIVE", stage="VALIDATE", depends_on=("CHAMPION_IDENTIFIED",)),
        ConditionSpec("ECONOMIC_BUYER_ENGAGED", stage="PROPOSE",
                      depends_on=("ECONOMIC_BUYER_IDENTIFIED",)),
        ConditionSpec("PAPER_PROCESS_KNOWN", stage="PROPOSE"),                    # Paper process
        ConditionSpec("QUOTE_DELIVERED", stage="PROPOSE"),
        ConditionSpec("QUOTE_ACCEPTED", stage="NEGOTIATE", depends_on=("QUOTE_DELIVERED",)),
        ConditionSpec("CONTRACT_SENT", stage="NEGOTIATE"),
        ConditionSpec("CONTRACT_REDLINES_RESOLVED", stage="CLOSE", depends_on=("CONTRACT_SENT",)),
        ConditionSpec("SIGNATURE_PENDING", stage="CLOSE"),
    )
    return compile_methodology("MEDDICC", stages=stages, conditions=conditions)


def spiced() -> ClosingMethodology:
    """SPICED (Situation / Pain / Impact / Critical event / Decision), a leaner motion."""
    stages = ("DISCOVER", "QUALIFY", "PROPOSE", "CLOSE")
    conditions = (
        ConditionSpec("BUSINESS_PROBLEM_CONFIRMED", stage="DISCOVER"),            # Situation
        ConditionSpec("METRICS_QUANTIFIED", stage="DISCOVER"),                    # Pain
        ConditionSpec("BUDGET_CONFIRMED", stage="QUALIFY"),                       # Impact
        ConditionSpec("DECISION_PROCESS_MAPPED", stage="QUALIFY"),                # Decision
        ConditionSpec("ECONOMIC_BUYER_ENGAGED", stage="PROPOSE"),
        ConditionSpec("MUTUAL_PLAN_AGREED", stage="PROPOSE"),                     # Critical event
        ConditionSpec("SIGNATURE_PENDING", stage="CLOSE"),
    )
    return compile_methodology("SPICED", stages=stages, conditions=conditions)


# A registry of the built-ins, resolved lazily so each caller gets a freshly-sealed instance.
_BUILTINS: Dict[str, "callable"] = {"MEDDICC": meddicc, "SPICED": spiced}


def builtin_names() -> tuple:
    return tuple(_BUILTINS)


def get_methodology(name: str) -> ClosingMethodology:
    """Resolve a built-in methodology by name. Raises :class:`KeyError` on an unknown name."""
    try:
        return _BUILTINS[name]()
    except KeyError:
        raise KeyError(f"unknown built-in methodology {name!r}; known: {sorted(_BUILTINS)}") from None


__all__ = ["meddicc", "spiced", "get_methodology", "builtin_names"]

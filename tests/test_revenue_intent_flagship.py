"""Commercial intent detection (§7) + the flagship quote-to-order composition (§14)."""
from __future__ import annotations

from agentic_os.integrations.business.contracts import Account, Provenance
from agentic_os.integrations.business.entity_resolution import EntityQuery, ResolutionState
from agentic_os.revenue.intent import CommercialIntent, classify_intent
from agentic_os.revenue.quote import CatalogItem, QuoteLine
from agentic_os.revenue.flagship import plan_quote_from_request

_NOW = 1_800_000_000_000
_CATALOG = {"WIDGET": CatalogItem("WIDGET", "Widget", list_price_cents=1_000, unit_cost_cents=500, on_hand_qty=1000)}
_ACCOUNTS = [Account(prov=Provenance("twenty", "acct_acme"), name="Acme Corp", domain="acme.com")]


# ---- §7 intent classification ----
def test_flagship_message_is_a_quote_request() -> None:
    c = classify_intent("Can you supply 300 units by October 15 and give me our contract pricing?")
    assert c.intent is CommercialIntent.QUOTE_REQUEST and c.confidence >= 0.7
    assert c.entities["quantity"] == 300 and c.entities["has_date"] is True and c.is_actionable_quote


def test_specific_intents_and_general() -> None:
    assert classify_intent("we want to renew our contract").intent is CommercialIntent.RENEWAL_CONCERN
    assert classify_intent("thinking of cancelling, we're unhappy").intent is CommercialIntent.CHURN_RISK
    assert classify_intent("how much does the pro plan cost?").intent is CommercialIntent.PRICING_REQUEST
    assert classify_intent("do you have this in stock?").intent is CommercialIntent.AVAILABILITY_REQUEST
    g = classify_intent("hello, nice product")
    assert g.intent is CommercialIntent.GENERAL and g.confidence < 0.5


# ---- §14 flagship composition ----
def _plan(text, q, allow_probable=False):
    return plan_quote_from_request(
        text, entity_query=q, candidate_accounts=_ACCOUNTS, lines=[QuoteLine("WIDGET", 300)],
        catalog=_CATALOG, now_ms=_NOW, allow_probable_entity=allow_probable)


def test_flagship_ready_when_intent_entity_and_feasible() -> None:
    p = _plan("supply 300 units and send pricing", EntityQuery(domain="acme.com"))
    assert p.ready and p.approval_required
    assert p.entity.state is ResolutionState.RESOLVED
    assert p.feasibility.feasible and p.as_dict()["draft_quote_plan"]["total_cents"] == 300_000


def test_flagship_observation_only_when_not_a_quote() -> None:
    p = _plan("just saying hi", EntityQuery(domain="acme.com"))
    assert not p.ready and not p.approval_required and "observation only" in p.reason


def test_flagship_refuses_unresolved_customer() -> None:
    p = _plan("supply 300 units and send pricing", EntityQuery(domain="unknown.co"))
    assert not p.ready and "cannot auto-draft" in p.reason and p.entity.state is ResolutionState.UNRESOLVED


def test_flagship_name_only_is_probable_and_gated() -> None:
    # name-only match is PROBABLE → blocked by default, allowed only with the policy opt-in
    assert not _plan("supply 300 units and send pricing", EntityQuery(name="Acme")).ready
    assert _plan("supply 300 units and send pricing", EntityQuery(name="Acme"), allow_probable=True).ready

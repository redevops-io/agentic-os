"""Inbound signal -> validated Contact-upsert proposal — abstains on malformed input or weak/GENERAL intent."""
from __future__ import annotations

from agentic_os.revenue import contact_intake as ci
from agentic_os.revenue.intent import CommercialIntent, IntentClassification


def _ic(intent=CommercialIntent.QUOTE_REQUEST, confidence=0.8) -> IntentClassification:
    return IntentClassification(intent, confidence, "chatwoot", "conv-1", 0)


def test_proposes_contact_for_well_formed_actionable_signal():
    p = ci.propose_contact_from_classification(_ic(), name="Dana Rivera", email="dana@example.com")
    assert p is not None
    assert p.name == "Dana Rivera" and p.email == "dana@example.com"
    assert p.intent == "QUOTE_REQUEST" and p.confidence == 0.8
    assert "conv-1" in p.rationale


def test_abstains_on_malformed_email():
    assert ci.propose_contact_from_classification(_ic(), name="Dana Rivera", email="not-an-email") is None


def test_abstains_on_empty_name():
    assert ci.propose_contact_from_classification(_ic(), name="  ", email="dana@example.com") is None


def test_abstains_below_confidence_floor():
    assert ci.propose_contact_from_classification(_ic(confidence=0.3), name="Dana Rivera",
                                                  email="dana@example.com") is None


def test_abstains_on_general_intent_even_if_confident():
    assert ci.propose_contact_from_classification(_ic(intent=CommercialIntent.GENERAL, confidence=0.9),
                                                  name="Dana Rivera", email="dana@example.com") is None


def test_email_is_lowercased_and_trimmed():
    p = ci.propose_contact_from_classification(_ic(), name=" Dana Rivera ", email=" Dana@Example.com ")
    assert p.name == "Dana Rivera" and p.email == "dana@example.com"


def test_propose_contact_classifies_text_first():
    p = ci.propose_contact("We'd like to buy 50 units, what's the price?", name="Dana Rivera",
                           email="dana@example.com", source_ref="conv-7")
    assert p is not None
    assert p.intent == "QUOTE_REQUEST"
    assert p.source_ref == "conv-7"


def test_propose_contact_abstains_on_generic_text():
    p = ci.propose_contact("just saying hi", name="Dana Rivera", email="dana@example.com")
    assert p is None

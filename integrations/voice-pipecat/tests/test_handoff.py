from __future__ import annotations

import pytest

from agentic_os_voice.handoff import HANDOFF_SCRIPT, classify


@pytest.mark.parametrize("text,category", [
    ("I think I am having a heart attack", "emergency"),
    ("please call an ambulance", "emergency"),
    ("he is going to hurt me", "violence"),
    ("I want to end my life", "self_harm"),
    ("I'm scared, please help me", "distress"),
    ("I can't breathe", "emergency"),
])
def test_distress_phrases_route_to_handoff(text, category):
    assert classify(text) == category


@pytest.mark.parametrize("text", [
    "I would like a quote for onboarding", "Our budget is twenty thousand dollars", "the code is four eight one five",
    "We had an emergency meeting last week about pricing", ""])
def test_ordinary_sales_talk_does_not(text):
    # 'emergency meeting' is a known over-trigger in the other direction; it is listed so nobody assumes the screen is precise
    expected = "emergency" if "emergency meeting" in text else None
    assert classify(text) == expected


def test_non_text_input_is_ignored_and_long_input_is_bounded():
    assert classify(None) is None and classify(123) is None
    assert classify("x" * 100_000 + " heart attack") is None            # only the first 2000 characters are screened


def test_the_script_makes_no_promise():
    s = HANDOFF_SCRIPT.lower()
    for promise in ("will be with you", "on the way", "help is coming", "right away", "immediately", "shortly", "within", "guarantee"):
        assert promise not in s
    assert "cannot promise" in s and "requesting" in s


def test_the_checker_that_guards_the_demo_catches_an_unreceipted_claim(env):
    from agentic_os_voice.demo.run import unreceipted_claims
    assert unreceipted_claims(["I have sent the message to Dana."], env) == ["I have sent the message to Dana."]
    assert unreceipted_claims(["That is done, and the system returned a confirmation."], env) != []        # no completed receipt exists

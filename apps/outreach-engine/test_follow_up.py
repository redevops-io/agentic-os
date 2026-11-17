"""outreach-engine's second touch (`app.follow_up`): a reply-aware thank-you when `check_replies` already
found one, otherwise a polite no-reply nudge. It only stages a draft (the same consequence level as
draft_pitch); the operator calls the existing gated `send_all` to actually dispatch it.
"""
from __future__ import annotations

import importlib

import pytest

core = importlib.import_module("outreach-engine.core")
app_mod = importlib.import_module("outreach-engine.app")


@pytest.fixture(autouse=True)
def _reset_state():
    core._STATE.update(accounts=[], approved=set(), candidates=[])
    yield


def test_follow_up_unknown_account():
    res = app_mod.follow_up({"account": "nope"})
    assert res["ok"] is False and "refresh the pipeline" in res["reason"]


def test_follow_up_no_reply_nudge_falls_back_to_the_template(monkeypatch):
    monkeypatch.setattr(app_mod, "_llm", lambda *a, **k: "")
    core._STATE["accounts"] = [{"account": "Acme"}]
    res = app_mod.follow_up({"account": "Acme"})
    assert res["ok"] is True and res["follow_up_for"] == "no_reply" and res["draft_source"] == "template"
    assert res["subject"] == "Following up — Acme"
    assert res["body"] == core.neutral_followup("Acme")
    assert "<table" in res["html"]
    assert core._STATE["accounts"][0]["draft"]["body"] == res["body"]


def test_follow_up_no_reply_uses_the_llm_when_it_answers(monkeypatch):
    seen = []

    def fake_llm(prompt, max_tokens=400):
        seen.append(prompt)
        return "  A short LLM-written nudge.  "

    monkeypatch.setattr(app_mod, "_llm", fake_llm)
    core._STATE["accounts"] = [{"account": "Acme"}]
    res = app_mod.follow_up({"account": "Acme"})
    assert res["draft_source"] == "llm" and res["body"] == "A short LLM-written nudge."
    assert any("earlier note that got no reply" in p for p in seen)


def test_follow_up_reply_aware_thank_you_when_a_reply_was_found(monkeypatch):
    seen = []

    def fake_llm(prompt, max_tokens=400):
        seen.append(prompt)
        return "  Thanks for writing back!  "

    monkeypatch.setattr(app_mod, "_llm", fake_llm)
    core._STATE["accounts"] = [{"account": "Acme", "replied": True,
                                "reply": {"subject": "Re: pitch", "from": "a@acme.com"}}]
    res = app_mod.follow_up({"account": "Acme"})
    assert res["follow_up_for"] == "reply" and res["draft_source"] == "llm"
    assert res["subject"] == "Re: Acme" and res["body"] == "Thanks for writing back!"
    assert any("thanking Acme for responding" in p and "Re: pitch" in p for p in seen)


def test_follow_up_reply_aware_falls_back_to_a_fixed_thank_you(monkeypatch):
    monkeypatch.setattr(app_mod, "_llm", lambda *a, **k: "")
    core._STATE["accounts"] = [{"account": "Acme", "replied": True, "reply": {}}]
    res = app_mod.follow_up({"account": "Acme"})
    assert res["draft_source"] == "template" and "short call this week" in res["body"]


def test_follow_up_is_registered_in_the_route_dispatch():
    assert app_mod._ACTIONS["follow_up"] is app_mod.follow_up
    assert app_mod._TOOL_OF["follow_up"] == "outreach.follow_up"

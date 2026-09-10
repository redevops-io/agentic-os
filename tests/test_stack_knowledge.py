"""Sidekick as the stack's Q&A expert — authoritative, deterministic answers about creds,
data locality, and governance (so nobody has to read a manual)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from agentic_os.projects_api import SampleProjectionProvider, create_app, sidekick_reply
from agentic_os.stack_knowledge import answer_stack_question, help_questions


def test_credentials_answer_is_authoritative_and_ref_only():
    e = answer_stack_question("how are my credentials handled?")
    assert e is not None and e.id == "credentials"
    # the load-bearing claims: a reference (not the token) reaches the Mission, nothing logged
    assert "reference" in e.answer.lower() and "never logged" in e.answer.lower()


def test_data_locality_distinguishes_cloud_vs_local():
    e = answer_stack_question("is my data processed locally or in the cloud?")
    assert e is not None and e.id == "data-locality"
    a = e.answer.lower()
    assert "cloud" in a and ("local" in a or "your own machine" in a)


def test_model_never_sees_secrets():
    e = answer_stack_question("does the AI model ever see my tokens?")
    assert e is not None and e.id == "secrets-to-model"
    assert e.answer.strip().lower().startswith("no")


def test_connect_scope_says_connecting_is_not_ingest_everything():
    e = answer_stack_question("if I connect google will it read my whole drive?")
    assert e is not None and e.id == "connect-scope"
    assert "not" in e.answer.lower() and "drive.file" in e.answer.lower()


def test_revoke_and_governance_entries_match():
    assert answer_stack_question("how do I revoke access to an app?").id == "revoke"
    assert answer_stack_question("is anything auto-executed without asking?").id == "governance"


def test_ordinary_mission_request_does_not_false_match():
    # a normal task, not a stack question → no knowledge entry hijacks it
    assert answer_stack_question("refund Sarah Chen for the duplicate charge") is None
    assert answer_stack_question("draft a cold outreach email to Tasha") is None


def test_every_entry_names_where_it_is_enforced():
    for e in help_questions():
        assert e["question"] and e["topic"]
    from agentic_os.stack_knowledge import STACK_KNOWLEDGE
    assert all(e.source for e in STACK_KNOWLEDGE)   # every claim points at its enforcement site


def test_sidekick_reply_routes_stack_questions_to_the_expert():
    r = sidekick_reply({"section": "Overview"}, "where is my data processed, locally or in the cloud?")
    assert "cloud" in r["text"].lower() and r.get("topic") == "Data handling"
    # and the existing governed-mission behaviour is untouched (tier-4 approval answer still wins)
    assert "tier-4" in sidekick_reply({"section": "Missions"}, "why does this need approval?")["text"].lower()


def test_sidekick_and_help_endpoints():
    c = TestClient(create_app(SampleProjectionProvider()))
    ans = c.post("/api/sidekick", json={"ctx": {}, "text": "how are my credentials stored?"}).json()
    assert "reference" in ans["text"].lower()
    helped = c.get("/api/sidekick/help").json()
    ids = {h["id"] for h in helped}
    assert {"credentials", "data-locality", "connect-scope", "governance"} <= ids

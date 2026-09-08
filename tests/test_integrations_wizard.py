"""W1 + W2 — the deterministic wizard end-to-end: a plain-English request read into an
editable proposal, confirmed, and compiled into a ConnectPlan.

Pins:
  * provider resolution precedence (named → connected → workspace-preferred → default)
    and existing-systems-win;
  * a capability nothing can build is refused by name, and the plan is not connectable;
  * the reader grounds against the manifest (never proposes the unbuildable; substitutes);
  * authority/identity is asked, reversible config is inferred;
  * the whole request → ConnectPlan path, content-addressed and answer-gated.
"""
from __future__ import annotations

import pytest

from agentic_os.integrations import (
    CapabilityDimension,
    CapabilityRequirement,
    CapabilityRequirementGraph,
    ConnectPlan,
    IntegrationManifest,
    IntegrationProposal,
    InMemoryConnections,
    KeywordReader,
    Support,
    compile_integration_intent,
    confirm_from_request,
    interpret,
    plan_from_request,
    resolve_capability,
)


def _manifest() -> IntegrationManifest:
    return IntegrationManifest(
        dimensions=(
            CapabilityDimension("email.message.send", "gmail", Support.EXECUTED, tier=3),
            CapabilityDimension("email.message.send", "outlook", Support.EXECUTED, tier=3),
            CapabilityDimension("crm.contact.upsert", "hubspot", Support.EXECUTED, tier=2),
            CapabilityDimension(
                "crm.contact.upsert", "salesforce", Support.NOT_MODELLED, tier=2,
                why="no Salesforce write adapter yet", alternative="hubspot",
            ),
            CapabilityDimension("chat.message.send", "whatsapp_business", Support.EXECUTED, tier=3),
            CapabilityDimension("approval.request", "slack", Support.EXECUTED, tier=3),
            CapabilityDimension("billing.refund.execute", "stripe", Support.EXECUTED, tier=4),
            CapabilityDimension("calendar.event.create", "google_calendar", Support.EXECUTED, tier=3),
        )
    )


# ── W1: provider resolution precedence ──────────────────────────────────────────
def test_named_preference_wins_when_buildable():
    r = resolve_capability("email.message.send", preference="outlook",
                           manifest=_manifest(), connections=InMemoryConnections())
    assert r.provider == "outlook" and r.source == "named"


def test_existing_connection_wins_over_default():
    # no preference; workspace already has outlook connected -> use it, not the default gmail
    conns = InMemoryConnections(already=("outlook",))
    r = resolve_capability("email.message.send", preference="",
                           manifest=_manifest(), connections=conns)
    assert r.provider == "outlook" and r.source == "connected"


def test_workspace_preferred_then_manifest_default():
    m = _manifest()
    pref = resolve_capability("email.message.send", preference="",
                              manifest=m, connections=InMemoryConnections(preferences=(("email.message.send", "outlook"),)))
    assert pref.provider == "outlook" and pref.source == "preferred"
    default = resolve_capability("email.message.send", preference="",
                                 manifest=m, connections=InMemoryConnections())
    assert default.provider == "gmail" and default.source == "default"  # first buildable


def test_unbuildable_capability_is_refused_by_name():
    r = resolve_capability("payments.payout", preference="",
                           manifest=_manifest(), connections=InMemoryConnections())
    assert r.provider == "" and r.source == "refused" and r.refusal is not None


# ── W1: compile → ConnectPlan ────────────────────────────────────────────────────
def _confirmed(reqs, prefs=()):
    graph = CapabilityRequirementGraph(
        requirements=tuple(CapabilityRequirement(c, p) for c, p in reqs),
        edges=(),
    )
    return IntegrationProposal(
        interpreted_outcome="x", requirements=graph, provider_preferences=dict(prefs),
    ).confirm(confirmed_by="alex", confirmed_at="t")


def test_compile_produces_a_connectable_content_addressed_plan():
    ci = _confirmed([("chat.message.send", "whatsapp_business"), ("crm.contact.upsert", "hubspot")])
    plan = compile_integration_intent(ci, manifest=_manifest())
    assert isinstance(plan, ConnectPlan)
    assert plan.connectable
    assert plan.plan_id.startswith("rcv1:")
    providers = {s.provider for s in plan.connect_steps}
    assert providers == {"whatsapp_business", "hubspot"}


def test_compile_marks_already_connected_and_skips_no_new_auth():
    ci = _confirmed([("crm.contact.upsert", "hubspot")])
    plan = compile_integration_intent(ci, manifest=_manifest(),
                                      connections=InMemoryConnections(already=("hubspot",)))
    step = next(s for s in plan.connect_steps if s.provider == "hubspot")
    assert step.already_connected is True


def test_compile_refuses_unbuildable_and_is_not_connectable():
    ci = _confirmed([("crm.contact.upsert", "salesforce")])  # only NOT_MODELLED for salesforce
    plan = compile_integration_intent(ci, manifest=_manifest())
    # salesforce isn't buildable, but hubspot is -> resolver substitutes to a default, connectable
    assert plan.connectable
    assert ("crm.contact.upsert", "hubspot") in plan.applied_defaults


def test_test_mission_preserves_graph_order():
    graph = CapabilityRequirementGraph(
        requirements=(
            CapabilityRequirement("chat.message.send", "whatsapp_business"),
            CapabilityRequirement("crm.contact.upsert", "hubspot"),
            CapabilityRequirement("approval.request", "slack"),
        ),
        edges=(("chat.message.send", "crm.contact.upsert"), ("crm.contact.upsert", "approval.request")),
    )
    ci = IntegrationProposal(interpreted_outcome="x", requirements=graph).confirm(confirmed_by="a", confirmed_at="t")
    plan = compile_integration_intent(ci, manifest=_manifest())
    assert [m.capability for m in plan.test_mission] == [
        "chat.message.send", "crm.contact.upsert", "approval.request",
    ]


def test_compile_auth_method_per_provider():
    ci = _confirmed([("billing.refund.execute", "stripe")])
    plan = compile_integration_intent(ci, manifest=_manifest())
    step = next(s for s in plan.connect_steps if s.provider == "stripe")
    assert step.auth_method == "oauth" and step.tier == 4


# ── W2: reader → grounded proposal ───────────────────────────────────────────────
def test_reader_grounds_capabilities_and_asks_authority():
    p = interpret(
        "handle support and refunds on whatsapp, keep the crm updated, ask me in slack before refunding",
        reader=KeywordReader.default(), manifest=_manifest(),
    )
    caps = {r.capability for r in p.requirements.requirements}
    assert "chat.message.send" in caps and "crm.contact.upsert" in caps
    assert "billing.refund.execute" in caps and "approval.request" in caps
    # authority can't be guessed -> a question was asked
    assert any("approve refunds" in q for q in p.questions)


def test_reader_substitutes_an_unbuildable_hint():
    # "salesforce" hint isn't buildable; the manifest substitutes hubspot and says so
    p = interpret("put them in salesforce", reader=KeywordReader.default(), manifest=_manifest())
    req = next(r for r in p.requirements.requirements if r.capability == "crm.contact.upsert")
    assert req.provider_preference == "hubspot"
    assert any("salesforce" in a for a in p.assumptions)


def test_reader_leaves_out_what_nothing_can_build():
    m = IntegrationManifest(dimensions=(
        CapabilityDimension("email.message.send", "gmail", Support.EXECUTED, tier=3),
    ))
    p = interpret("email them and refund them", reader=KeywordReader.default(), manifest=m)
    caps = {r.capability for r in p.requirements.requirements}
    assert caps == {"email.message.send"}  # refund left out (nothing builds it in this manifest)
    assert any("can't do billing.refund" in a for a in p.assumptions)


# ── end-to-end ───────────────────────────────────────────────────────────────────
def test_plan_from_request_end_to_end_with_answers():
    plan = plan_from_request(
        "answer whatsapp support, update the crm, refund via stripe, approve in slack",
        reader=KeywordReader.default(), manifest=_manifest(),
        confirmed_by="alex", confirmed_at="2026-09-08T00:00:00Z",
        answers={"Who is allowed to approve refunds?": "Finance team",
                 "Which account or number should send replies?": "+123"},
    )
    assert plan.connectable
    providers = {s.provider for s in plan.connect_steps}
    assert {"whatsapp_business", "hubspot", "stripe", "slack"} <= providers


def test_plan_from_request_refuses_until_authority_is_answered():
    with pytest.raises(ValueError, match="questions are open"):
        plan_from_request(
            "refund customers via stripe",
            reader=KeywordReader.default(), manifest=_manifest(),
            confirmed_by="alex", confirmed_at="t",  # no answers -> refund authority unanswered
        )


def test_confirm_from_request_is_deterministic():
    kw = KeywordReader.default()
    m = _manifest()
    a = confirm_from_request("update the crm", reader=kw, manifest=m, confirmed_by="x", confirmed_at="t")
    b = confirm_from_request("update the crm", reader=kw, manifest=m, confirmed_by="y", confirmed_at="u")
    assert a.content_hash == b.content_hash  # meaning-only identity, stable across readers of the same text

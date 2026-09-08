"""W0 — the Connect Compiler's deterministic core: the Integration Capability Manifest
(reality filter + refuse-by-name safety net) and the proposal/confirmation contracts.

These pin the invariants the wizard rests on:

  * the manifest lists only what can actually run (``buildable``) and substitutes a
    buildable provider for a preference it can't honour — so a proposal never suggests
    the impossible;
  * refusals name the dimension and the runnable alternative, are returned all at once,
    and never appear for a capability that can run;
  * confirmation is the seal — it refuses while questions are open, produces a frozen
    content-addressed intent, and its identity is the meaning, not who/when confirmed.
"""
from __future__ import annotations

import dataclasses

import pytest
from runtime_contracts import RefusalKind

from agentic_os.integrations import (
    CapabilityDimension,
    CapabilityRequirement,
    CapabilityRequirementGraph,
    ConfirmedIntegrationIntent,
    IntegrationManifest,
    IntegrationProposal,
    Support,
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
            CapabilityDimension(
                "chat.message.send", "whatsapp_web", Support.REFUSED, tier=3,
                why="unofficial automation is not a production default",
                alternative="whatsapp_business",
            ),
        )
    )


# ── reality filter ────────────────────────────────────────────────────────────
def test_buildable_lists_only_executed_providers():
    m = _manifest()
    assert m.buildable("email.message.send") == ("gmail", "outlook")
    assert m.buildable("crm.contact.upsert") == ("hubspot",)  # salesforce not modelled
    assert m.buildable("calendar.event.create") == ()


def test_substitute_prefers_a_buildable_preference_else_falls_back():
    m = _manifest()
    assert m.substitute("email.message.send", preferred="outlook") == "outlook"
    assert m.substitute("email.message.send", preferred="yahoo") == "gmail"
    assert m.substitute("crm.contact.upsert", preferred="salesforce") == "hubspot"
    assert m.substitute("calendar.event.create") is None


# ── refuse by name, never nearest-runnable ──────────────────────────────────────
def test_executed_pair_is_not_refused():
    m = _manifest()
    assert m.decide("email.message.send", "gmail") is None
    assert m.decide("email.message.send") is None  # buildable at all


def test_unknown_capability_refuses_by_dimension():
    r = _manifest().decide("calendar.event.create")
    assert r is not None and r.kind is RefusalKind.UNSUPPORTED_DIMENSION
    assert r.dimension == "calendar.event.create"
    assert r.executable_values == ()


def test_withheld_provider_refuses_by_value_with_alternative():
    r = _manifest().decide("chat.message.send", "whatsapp_web")
    assert r is not None and r.kind is RefusalKind.UNSUPPORTED_VALUE
    assert r.stated_value == "whatsapp_web"
    assert "whatsapp_business" in r.executable_values
    assert "production default" in r.detail


def test_unmodelled_named_provider_refuses_with_buildable_alternative():
    r = _manifest().decide("crm.contact.upsert", "salesforce")
    assert r is not None and r.kind is RefusalKind.UNSUPPORTED_VALUE
    assert r.executable_values == ("hubspot",)


def test_refusals_for_returns_all_at_once_and_omits_runnable_ones():
    m = _manifest()
    needs = [
        ("chat.message.send", "whatsapp_web"),
        ("crm.contact.upsert", "salesforce"),
        ("email.message.send", "gmail"),        # fine — no refusal
        ("calendar.event.create", ""),          # not modelled
    ]
    dims = [r.dimension for r in m.refusals_for(needs)]
    assert "email.message.send" not in dims
    assert set(dims) == {"chat.message.send", "crm.contact.upsert", "calendar.event.create"}


def test_manifest_is_content_addressed_and_order_independent():
    a = _manifest()
    b = IntegrationManifest(dimensions=tuple(reversed(a.dimensions)))
    assert a.manifest_id == b.manifest_id
    assert a.manifest_id.startswith("rcv1:")


def test_tier_out_of_range_is_refused():
    with pytest.raises(ValueError, match="tier must be 0..4"):
        CapabilityDimension("x.y", "p", Support.EXECUTED, tier=9)


# ── proposal → confirm = seal ───────────────────────────────────────────────────
def _proposal() -> IntegrationProposal:
    graph = CapabilityRequirementGraph(
        requirements=(
            CapabilityRequirement("chat.message.send", "whatsapp_business"),
            CapabilityRequirement("crm.contact.upsert", "hubspot", reason="defaulted from pack"),
        ),
        edges=(("chat.message.send", "crm.contact.upsert"),),
    )
    return IntegrationProposal(
        interpreted_outcome="answer customers",
        requirements=graph,
        provider_preferences={"chat.message.send": "whatsapp_business", "crm.contact.upsert": "hubspot"},
        authority_rules=["reply to customers: allowed"],
        assumptions=["crm defaulted to hubspot"],
    )


def test_confirm_seals_into_a_content_addressed_intent():
    ci = _proposal().confirm(confirmed_by="alex", confirmed_at="2026-09-08T00:00:00Z")
    assert isinstance(ci, ConfirmedIntegrationIntent)
    assert ci.content_hash.startswith("rcv1:")
    assert ci.confirmed_by == "alex"
    assert ci.outcome == "answer customers"


def test_confirmation_identity_is_meaning_only():
    p = _proposal()
    a = p.confirm(confirmed_by="alex", confirmed_at="2026-09-08T00:00:00Z")
    b = p.confirm(confirmed_by="sam", confirmed_at="2027-01-01T00:00:00Z")
    assert a.content_hash == b.content_hash  # who/when confirmed is excluded from identity


def test_confirm_refuses_while_questions_are_open():
    p = _proposal()
    p.questions = ["who may approve refunds?"]
    with pytest.raises(ValueError, match="questions are open"):
        p.confirm(confirmed_by="alex", confirmed_at="t")


def test_confirmed_intent_is_frozen():
    ci = _proposal().confirm(confirmed_by="alex", confirmed_at="t")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ci.outcome = "changed"  # type: ignore[misc]


def test_requirement_graph_is_content_addressed():
    g = _proposal().requirements
    assert g.graph_id.startswith("rcv1:")

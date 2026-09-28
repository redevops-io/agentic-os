"""Generic cross-system entity resolution — the discrete-state model that gates consequential actions."""
from __future__ import annotations

from agentic_os.integrations.business import (
    Account, Contact, Customer, Party, Provenance, ResolutionState, resolve_entity)
from agentic_os.integrations.business.entity_resolution import EntityQuery


def _pv(ref: str) -> Provenance:
    return Provenance(provider="twenty", provider_ref=ref)


def test_shared_external_id_resolves() -> None:
    pool = [Party(prov=_pv("p1"), name="Nothing Alike", external_ids={"crm": "acct_9"})]
    m = resolve_entity(EntityQuery(name="whatever", external_ids={"crm": "acct_9"}), pool)
    assert m.state is ResolutionState.RESOLVED
    assert m.entity_ref == "p1" and m.matched_on == "external:crm"
    assert m.may_drive_action()


def test_exact_email_resolves() -> None:
    pool = [Contact(prov=_pv("c1"), email="jane@acme.com", first_name="Jane", last_name="Doe")]
    m = resolve_entity(EntityQuery(email="jane@acme.com"), pool)
    assert m.state is ResolutionState.RESOLVED and m.entity_ref == "c1" and m.matched_on == "email"


def test_domain_resolves() -> None:
    pool = [Account(prov=_pv("a1"), name="Acme", domain="acme.com")]
    m = resolve_entity(EntityQuery(domain="acme.com"), pool)
    assert m.state is ResolutionState.RESOLVED and m.matched_on == "domain"


def test_name_only_is_probable_not_resolved() -> None:
    # a name-only match is plausible but never certain — must not auto-act without a policy opt-in
    pool = [Customer(prov=_pv("cu1"), name="Acme Corporation")]
    m = resolve_entity(EntityQuery(name="Acme"), pool)   # token Jaccard 0.5 -> ~0.675
    assert m.state is ResolutionState.PROBABLE
    assert not m.may_drive_action()                       # default: PROBABLE cannot drive actions
    assert m.may_drive_action(allow_probable=True)        # only with explicit policy opt-in


def test_two_comparable_names_are_ambiguous() -> None:
    pool = [Account(prov=_pv("a1"), name="Acme Inc"), Account(prov=_pv("a2"), name="Acme LLC")]
    m = resolve_entity(EntityQuery(name="Acme"), pool)
    assert m.state is ResolutionState.AMBIGUOUS
    assert set(m.candidate_refs) == {"a1", "a2"} and not m.may_drive_action()


def test_no_signal_is_unresolved() -> None:
    pool = [Account(prov=_pv("a1"), name="Globex", domain="globex.com")]
    m = resolve_entity(EntityQuery(name="Umbrella", domain="umbrella.co"), pool)
    assert m.state is ResolutionState.UNRESOLVED and not m.may_drive_action()


def test_email_domain_falls_through_to_account_domain() -> None:
    pool = [Account(prov=_pv("a1"), name="Acme", domain="acme.com")]
    m = resolve_entity(EntityQuery(email="new.person@acme.com"), pool)
    assert m.state in (ResolutionState.RESOLVED, ResolutionState.PROBABLE)
    assert m.matched_on == "email_domain"

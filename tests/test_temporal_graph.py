"""Temporal entity/relationship graph projection + KYC ownership replay (§2/§11). Offline.

Pins the bi-temporal guarantee: the ownership graph reconstructs as-of a decision time, so a KYC screening
replays exactly as it stood then — a sanction learned later must not leak into an earlier decision.
"""
from __future__ import annotations

from agentic_os.intelligence import (
    GraphEntity, GraphRelationship, LifecycleEvent, TemporalGraph, project_kyc_ownership, screen_ownership,
)

# The KYC demo vendors shape (id → {name, country, kyc, sanctioned_owner, hops_upstream}).
VENDORS = {
    "handlowy": {"name": "HANDLOWY-INWESTYCJE", "country": "PL", "kyc": "NO-GO",
                 "sanctioned_owner": "CITIGROUP INC.", "hops_upstream": 6},
    "banca": {"name": "BANCA CENTRO EMILIA", "country": "IT", "kyc": "GO",
              "sanctioned_owner": None, "hops_upstream": 0},
    "abb": {"name": "ABB AG", "country": "DE", "kyc": "ABSTAIN",
            "sanctioned_owner": None, "hops_upstream": 0},
}
SANCTION_KNOWN = "2026-02-01T00:00:00Z"


# ── core temporal graph ─────────────────────────────────────────────────────────────────────────────────
def test_as_of_excludes_facts_not_yet_known():
    g = TemporalGraph()
    g.add_entity(GraphEntity("a", known_at="2026-01-01T00:00:00Z"))
    g.add_entity(GraphEntity("b", known_at="2026-03-01T00:00:00Z"))     # learned later
    g.add_relationship(GraphRelationship("a", "owned_by", "b", known_at="2026-03-01T00:00:00Z"))
    early = g.as_of("2026-02-01T00:00:00Z")
    assert "a" in early.entities and "b" not in early.entities          # b not yet known
    assert early.edges == ()                                            # edge drops when an endpoint is unknown
    late = g.as_of("2026-04-01T00:00:00Z")
    assert "b" in late.entities and len(late.edges) == 1


def test_ownership_chain_traversal_and_hops():
    g = project_kyc_ownership(VENDORS, sanction_known_at=SANCTION_KNOWN)
    snap = g.as_of("2026-06-01T00:00:00Z")
    chain = snap.chain("handlowy", "owned_by")
    assert chain[0] == "handlowy" and chain[-1] == "CITIGROUP INC."
    assert len(chain) == 7                                              # vendor + 5 intermediates + owner (6 hops)


# ── KYC screening, replayable as-of ─────────────────────────────────────────────────────────────────────
def test_no_go_only_after_the_sanction_is_known():
    g = project_kyc_ownership(VENDORS, sanction_known_at=SANCTION_KNOWN)
    before = screen_ownership(g, "handlowy", valid_time="2026-01-15T00:00:00Z")  # before sanction known
    after = screen_ownership(g, "handlowy", valid_time="2026-03-15T00:00:00Z")   # after
    assert before["decision"] == "GO"                                  # sanction not yet knowable → clean
    assert after["decision"] == "NO-GO" and after["flagged"] == "CITIGROUP INC."
    assert after["chain"][0] == "handlowy" and after["chain"][-1] == "CITIGROUP INC."


def test_clean_vendor_is_go_and_ambiguous_is_abstain():
    g = project_kyc_ownership(VENDORS, sanction_known_at=SANCTION_KNOWN)
    go = screen_ownership(g, "banca", valid_time="2026-06-01T00:00:00Z")
    abstain = screen_ownership(g, "abb", valid_time="2026-06-01T00:00:00Z")
    unknown = screen_ownership(g, "not-a-vendor", valid_time="2026-06-01T00:00:00Z")
    assert go["decision"] == "GO"
    assert abstain["decision"] == "ABSTAIN" and "ambiguous" in abstain["reason"]
    assert unknown["decision"] == "ABSTAIN" and unknown["reason"] == "identity not resolved"


def test_known_at_can_differ_from_valid_time():
    # as-of a late valid_time but an EARLY known_at: the world has moved on, but we screen on what we knew.
    g = project_kyc_ownership(VENDORS, sanction_known_at=SANCTION_KNOWN)
    d = screen_ownership(g, "handlowy", valid_time="2026-06-01T00:00:00Z", known_at="2026-01-15T00:00:00Z")
    assert d["decision"] == "GO"                                       # sanction not known at that known_at

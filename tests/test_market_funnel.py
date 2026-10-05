"""Market & Funnel Intelligence Phase 5 — funnel versioning + path-change detection.

Proves: a competitor funnel is reconstructable at any past time; an unchanged re-observation emits no change;
and path changes are detected and characterized — new lead magnet, shortened path, reordering.
"""
from __future__ import annotations

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.market import FunnelHistory, diff_funnels
from agentic_os.market.contracts import Funnel, FunnelStep


def _funnel(company, stages, t):
    steps = tuple(FunnelStep(prov=Provenance(provider="obs", observed_at=t), stage=s, url=f"/{s}") for s in stages)
    return Funnel(prov=Provenance(provider="obs", observed_at=t), company_ref=company, entry_url="/", steps=steps)


def test_unchanged_funnel_emits_no_change():
    a = _funnel("e1", ["creative", "landing", "offer", "signup"], 1000)
    b = _funnel("e1", ["creative", "landing", "offer", "signup"], 2000)
    assert diff_funnels(a, b) is None


def test_new_lead_magnet_detected():
    before = _funnel("e1", ["creative", "landing", "offer", "signup"], 1000)
    after = _funnel("e1", ["creative", "landing", "quiz", "offer", "signup"], 2000)
    ch = diff_funnels(before, after, entity_ref="e1")
    assert ch is not None and "quiz" in ch.added_stages and ch.new_lead_magnet
    assert ch.lengthened and not ch.shortened and ch.significance == "CONVERSION_PATH"


def test_shortened_path_detected():
    before = _funnel("e1", ["creative", "landing", "quiz", "offer", "signup"], 1000)
    after = _funnel("e1", ["creative", "landing", "offer", "signup"], 2000)
    ch = diff_funnels(before, after)
    assert ch.shortened and "quiz" in ch.removed_stages and ch.path_len_after == 4


def test_reorder_detected():
    before = _funnel("e1", ["creative", "landing", "offer", "signup"], 1000)
    after = _funnel("e1", ["creative", "offer", "landing", "signup"], 2000)
    ch = diff_funnels(before, after)
    assert ch.reordered and not ch.added_stages and not ch.removed_stages


def test_history_reconstructs_and_series():
    h = FunnelHistory()
    h.record("e1", _funnel("e1", ["creative", "landing", "offer", "signup"], 1000))
    h.record("e1", _funnel("e1", ["creative", "landing", "offer", "signup"], 2000))   # unchanged
    h.record("e1", _funnel("e1", ["creative", "landing", "quiz", "offer", "signup"], 3000))  # +quiz
    assert len(h.versions("e1")) == 3
    assert h.at("e1", 2500).stages() == ("creative", "landing", "offer", "signup")
    assert len(h.latest("e1").stages()) == 5
    changes = h.changes("e1")
    assert len(changes) == 1 and changes[0].new_lead_magnet   # only the +quiz version changed the path

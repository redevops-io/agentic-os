"""Funnel observation from real page metrics (plan §7, P3 wiring)."""
from __future__ import annotations

from agentic_os.commercial import (
    ConversionFunnel, ConversionStage, diagnose_funnel, funnel_observation, stage_conversion_from_pageviews,
)


def _funnel():
    return ConversionFunnel(funnel_id="site", stages=(
        ConversionStage("visit", resource_ref="/"),
        ConversionStage("pricing", resource_ref="/pricing"),
        ConversionStage("demo", resource_ref="/demo")))


def test_stage_conversion_from_pageviews_finds_the_leak():
    pv = {"/": 1000, "/pricing": 620, "/demo": 180}   # pricing→demo is the big drop
    conv = stage_conversion_from_pageviews(_funnel(), pv)
    assert conv["visit"] == round(620 / 1000, 4)
    assert conv["pricing"] == round(180 / 620, 4)     # ~0.29 — the leak
    assert conv["demo"] == 1.0                         # terminal
    # feeds diagnose_funnel → pricing is the affected stage
    assert diagnose_funnel(_funnel(), stage_conversion=conv).affected_stage == "pricing"


def test_zero_prior_stage_is_zero_conversion():
    conv = stage_conversion_from_pageviews(_funnel(), {"/": 0, "/pricing": 0, "/demo": 0})
    assert conv["visit"] == 0.0 and conv["pricing"] == 0.0


def test_funnel_observation_none_without_source(monkeypatch):
    # no client + no env → None (caller falls back to synthetic)
    monkeypatch.delenv("UMAMI_URL", raising=False)
    monkeypatch.delenv("UMAMI_BASE_URL", raising=False)
    assert funnel_observation(_funnel(), client=None) is None


def test_funnel_observation_uses_injected_client():
    class _FakeClient:
        pass
    # monkeypatch collect_page_behavior via the provider by passing a client and stubbing pageviews
    from agentic_os.commercial import funnel_providers as fp

    class _P(fp.UmamiFunnelProvider):
        def pageviews(self, *, days=30):
            return {"/": 1000, "/pricing": 600, "/demo": 150}
    conv = _P(_FakeClient()).stage_conversion(_funnel())
    assert conv["pricing"] == round(150 / 600, 4)

"""Google Search Console sensor → content search signals (Content plan §3–§4). Offline.

A stub GSC client (canned searchAnalytics rows) → SearchObservations → the self-contained search detectors
(CTR opportunity, cannibalization) → the content approval queue. Auth/live paths self-skip without google-auth
or a key.
"""
from __future__ import annotations

from agentic_os.content.interventions import content_intervention_queue
from agentic_os.integrations.gsc import (
    GscClient, collect_search_observations, gsc_from_env, search_signals_from_observations, to_observation,
)


class _StubGsc(GscClient):
    def __init__(self):
        super().__init__(key_file="/dev/null")

    def _bearer(self):
        return "t"

    def search_analytics(self, site_url, *, days=28, row_limit=1000, dimensions=("query", "page")):
        return [
            # a good ranking with meaningful impressions but CTR far under baseline → CTR_OPPORTUNITY
            {"keys": ["governed runtime", "https://redevops.io/runtime"], "clicks": 10, "impressions": 800,
             "ctr": 0.0125, "position": 3.0},
            # same intent on two pages → CANNIBALIZATION
            {"keys": ["agentic os", "https://redevops.io/a"], "clicks": 5, "impressions": 300,
             "ctr": 0.017, "position": 6.0},
            {"keys": ["agentic os", "https://redevops.io/b"], "clicks": 4, "impressions": 200,
             "ctr": 0.02, "position": 8.0},
        ]


def test_to_observation_maps_row_and_rejects_short_keys():
    o = to_observation({"keys": ["q", "https://x/p"], "impressions": 100, "clicks": 3, "ctr": 0.03,
                        "position": 4.5}, days=28, site_id="s")
    assert o.query == "q" and o.page_url == "https://x/p" and o.impressions == 100 and o.position == 4.5
    assert to_observation({"keys": ["only-query"]}) is None


def test_gsc_signals_and_queue():
    obs = collect_search_observations(_StubGsc(), "https://redevops.io/", days=28, site_id="redevops.io")
    assert len(obs) == 3
    sigs = search_signals_from_observations(obs)
    kinds = {s.signal_type.value for s in sigs}
    assert "CTR_OPPORTUNITY" in kinds and "CANNIBALIZATION" in kinds
    q = content_intervention_queue(sigs)
    assert q["count"] >= 2 and q["requires_approval"] == q["count"]     # both park on approval
    caps = {cap for d in q["decisions"] for cap in d["required_capabilities"]}
    assert {"content.page.retitle", "content.page.consolidate"} <= caps


def test_gsc_from_env_and_self_skip(monkeypatch):
    monkeypatch.delenv("GSC_SA_KEY_FILE", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    assert gsc_from_env() is None
    # a client with a missing key self-skips (no google-auth call succeeds) — never raises
    c = GscClient(key_file="/nonexistent/key.json")
    assert c.connected() is False and c.sites() == [] and c.search_analytics("https://x/") == []

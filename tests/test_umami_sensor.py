"""Umami analytics sensor — normalizer (unit) + a live smoke that self-skips without a configured core."""
from __future__ import annotations

import pytest

from agentic_os.integrations.umami import (
    AnalyticsObservation, UmamiClient, collect_page_behavior, umami_from_env)


class _StubUmami:
    def top_pages(self, *, days=30, limit=50):
        return [{"x": "/context-runtime/", "y": 420}, {"x": "/agent-memory/", "y": 130},
                {"x": "", "y": 9}]                    # a blank path is dropped


def test_collect_page_behavior_normalizes_and_prefixes_origin() -> None:
    obs = collect_page_behavior(_StubUmami(), origin="https://redevops.io", site_id="redevops")
    assert [o.page_url for o in obs] == ["https://redevops.io/context-runtime/", "https://redevops.io/agent-memory/"]
    assert obs[0].pageviews == 420 and obs[0].source == "umami" and obs[0].site_id == "redevops"
    assert all(isinstance(o, AnalyticsObservation) for o in obs)


def test_umami_from_env_is_none_without_config(monkeypatch) -> None:
    monkeypatch.delenv("UMAMI_URL", raising=False)
    monkeypatch.delenv("WEBSITE_ID", raising=False)
    assert umami_from_env() is None


def test_client_self_skips_without_website_or_token() -> None:
    # no website id + unreachable core → empty, never raises
    c = UmamiClient(base_url="http://127.0.0.1:1", website_id="")
    assert c.top_pages() == [] and c.stats() == {} and c.connected() is False


def test_live_umami_read() -> None:
    # opt-in / network lane; self-skips unless a real Umami is configured + reachable
    client = umami_from_env()
    if client is None or not client.connected() or not client.token():
        pytest.skip("no reachable/authenticated Umami (set UMAMI_URL/WEBSITE_ID/UMAMI_ADMIN_USER/UMAMI_ADMIN_PASS)")
    obs = collect_page_behavior(client, days=30, origin=__import__("os").environ.get("UMAMI_ORIGIN", ""))
    assert isinstance(obs, list)   # a real read returns a (possibly empty) list of observations

"""Metabase signed embedding + surface adapter — making Metabase an embeddable Sidekick surface.

Proves: the embed token is a valid HS256 JWT we can verify with the shared secret (and a wrong secret fails);
the embed URL has Metabase's `/embed/{kind}/{jwt}` shape; signing without a secret raises; and the
MetabaseSurfaceAdapter produces a typed SurfaceContext (carrying the AnalyticsContext selection) + native/embed/
artifact links.
"""
from __future__ import annotations

import pytest

from agentic_os.sidekick import AnalyticsContext
from agentic_os.visualization import (
    MetabaseSurfaceAdapter, decode_embed_token, embed_url, sign_embed_token,
)

SECRET = "0" * 64


def test_embed_token_roundtrips_and_locks_resource():
    tok = sign_embed_token(SECRET, {"dashboard": 7}, params={}, ttl_seconds=300, now=1_000)
    payload = decode_embed_token(SECRET, tok)
    assert payload["resource"] == {"dashboard": 7}
    assert payload["params"] == {} and payload["exp"] == 1_300


def test_embed_token_wrong_secret_fails():
    tok = sign_embed_token(SECRET, {"question": 40})
    with pytest.raises(ValueError):
        decode_embed_token("deadbeef" * 8, tok)


def test_embed_url_shape():
    url = embed_url("http://mb:3001", "dashboard", 7, SECRET, now=0)
    assert url.startswith("http://mb:3001/embed/dashboard/")
    assert "#bordered=true&titled=true" in url


def test_signing_requires_a_secret():
    with pytest.raises(ValueError):
        sign_embed_token("", {"dashboard": 1})


def test_surface_adapter_context_and_links():
    a = AnalyticsContext(dashboard_id="7", dashboard_filters={"region": "EMEA"},
                         selected_entities=("cust:acme",), source_lineage=("erpnext",))
    ad = MetabaseSurfaceAdapter(base_url="http://mb:3001", embedding_secret=SECRET, analytics=a)
    ctx = ad.context()
    assert ctx.app_id == "metabase" and ctx.object_type == "dashboard" and ctx.route == "/dashboard/7"
    assert ctx.selection == ("cust:acme",) and "analytics.query.execute" in ctx.native_capabilities
    assert ad.deep_link("dashboard", "7") == "http://mb:3001/dashboard/7"
    assert ad.embed_link("dashboard", 7).startswith("http://mb:3001/embed/dashboard/")
    link = ad.artifact_link("dashboard", "7", project_id="P", session_id="sk_1")
    assert link.provider == "metabase" and link.native_url.endswith("/dashboard/7") and link.project_id == "P"

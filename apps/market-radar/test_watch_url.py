"""add_watch only forwards http(s) URLs on public hosts."""
from __future__ import annotations

import importlib

import pytest

core = importlib.import_module("market-radar.core")


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/x", "http://169.254.169.254/latest", "http://localhost:5001/",
    "http://127.0.0.1/", "http://10.0.0.5/", "http://vault/", "http://svc.cluster.local/", "http://u:p@example.com/",
    "http://[::1]/", "https:///nohost", "not a url",
])
def test_non_public_or_non_http_urls_are_refused(monkeypatch, url):
    def boom(*a, **k):
        raise AssertionError("must not call the core")
    monkeypatch.setattr(core.httpx, "Client", boom)
    r = core.add_watch({"url": url})
    assert r["status"] == "error" and r["action"] == "add_watch"


@pytest.mark.parametrize("url", ["https://example.com/pricing", "http://8.8.8.8/"])
def test_public_urls_pass_validation(url):
    assert core._watch_url_error(url) == ""

"""add_watch only forwards http(s) URLs on public hosts."""
from __future__ import annotations

import importlib

import pytest

core = importlib.import_module("market-radar.core")


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    import agentic_os.safe_url as su
    monkeypatch.setattr(su, "_default_resolver", lambda h: ["93.184.216.34"])


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "http://169.254.169.254/latest", "http://localhost:5001/", "http://127.0.0.1/",
    "http://0177.0.0.1/", "http://127.1/", "http://[64:ff9b::7f00:1]/", "http://vault/", "http://kubernetes.default/",
    "http://svc.consul/", "http://u:p@example.com/", "https:///nohost", "not a url",
])
def test_non_public_or_non_http_urls_are_refused(monkeypatch, url):
    def boom(*a, **k):
        raise AssertionError("must not call the core")
    monkeypatch.setattr(core.httpx, "Client", boom)
    r = core.add_watch({"url": url})
    assert r["status"] == "error" and r["action"] == "add_watch"


def test_name_resolving_to_loopback_is_refused(monkeypatch):
    import agentic_os.safe_url as su
    monkeypatch.setattr(su, "_default_resolver", lambda h: ["127.0.0.1"])
    assert core._watch_url_error("http://foo.localtest.me/") != ""


def test_public_url_passes_validation():
    assert core._watch_url_error("https://example.com/pricing") == ""

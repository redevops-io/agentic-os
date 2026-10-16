from __future__ import annotations

import pytest

from agentic_os.safe_url import public_url_problem

PUB = lambda h: ["93.184.216.34"]  # noqa: E731


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/", "http://169.254.169.254/", "http://localhost/", "http://127.0.0.1/",
    "http://0177.0.0.1/", "http://127.1/", "http://2130706433/", "http://0x7f000001/", "http://10.0.0.5/",
    "http://[::1]/", "http://[64:ff9b::7f00:1]/", "http://[::ffff:127.0.0.1]/", "http://[2002:7f00:1::]/",
    "http://kubernetes.default/", "http://svc.consul/", "http://a.cluster.local/", "http://vault/",
    "http://u:p@example.com/", "http://exa mple.com/", "https:///x", "not a url", "http://host.corp/", "http://x.test/",
])
def test_refused(url):
    assert public_url_problem(url, PUB) != ""


@pytest.mark.parametrize("name", ["foo.localtest.me", "127.0.0.1.nip.io", "rebind.example.com"])
def test_names_resolving_to_non_public_addresses_are_refused(name):
    assert public_url_problem(f"http://{name}/", lambda h: ["127.0.0.1"]) != ""
    assert public_url_problem(f"http://{name}/", lambda h: ["93.184.216.34", "10.0.0.1"]) != ""
    assert public_url_problem(f"http://{name}/", lambda h: ["64:ff9b::7f00:1"]) != ""


def test_unresolvable_fails_closed():
    def boom(h):
        raise OSError("nx")
    assert public_url_problem("https://example.com/", boom) != ""


@pytest.mark.parametrize("url", ["https://example.com/pricing", "http://8.8.8.8/", "https://[2606:4700::1111]/"])
def test_public_accepted(url):
    assert public_url_problem(url, PUB) == ""


@pytest.mark.parametrize("url", [
    "http://example.com./", "http://example.com:0/", "http://example.com:8080/", "http://example.com:22/",
    "http://１２７.0.0.1/", "http://127.0.0.１/", "http://exаmple.com/", "http://ехample.com/", "http://exämple.com/",
])
def test_static_host_and_port_checks(url):
    assert public_url_problem(url, PUB) != ""


def test_ports_and_punycode():
    assert public_url_problem("https://example.com:443/", PUB) == ""
    assert public_url_problem("http://example.com:8080/", PUB, allowed_ports=(80, 8080)) == ""
    assert public_url_problem("http://xn--bcher-kva.example.com/", PUB) == ""
    assert public_url_problem("http://xn--exmple-cua.com/", PUB) == ""       # punycode form resolves normally

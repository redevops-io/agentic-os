"""The products registry fails closed: bad entries are skipped and listed, URLs are operator config with no SSRF surface."""
from __future__ import annotations

import json

import pytest

from agentic_os.products.registry import RegistryError, load_registry, parse_registry, validate_url


def _doc(**prod):
    p = {"id": "edge-sentinel", "kind": "security", "api_base": "http://127.0.0.1:8203", "ui_url": "http://127.0.0.1:8203/ui"}
    p.update(prod)
    return {"schema": "aos.products/v1", "products": [p]}


def test_a_valid_entry_loads_with_defaults():
    reg = parse_registry(_doc())
    assert not reg.errors and reg.get("edge-sentinel").api_prefix == "/api/v1"
    s = reg.get("edge-sentinel")
    assert (s.health_path, s.info_path, s.approval_mode) == ("/readyz", "/api/v1/info", "human_click")


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://x/", "http://user:pw@127.0.0.1:1/", "http://127.0.0.1:1/?a=1", "http://127.0.0.1:1/#f",
    "http://169.254.169.254/", "http://0.0.0.0:1/", "https://x/%2e%2e/", "http://evil.example.com/", "http://[fe80::1]/",
    "javascript:alert(1)", "http://127.0.0.1:1/a/../b", "https://", "gopher://127.0.0.1/"])
def test_urls_that_are_not_a_product_are_refused(url):
    with pytest.raises(RegistryError):
        validate_url(url, what="api_base", allow_private_http=())


def test_https_and_loopback_http_and_listed_private_names_are_accepted():
    assert validate_url("https://sentinel.example.com", what="x", allow_private_http=()) == "https://sentinel.example.com"
    assert validate_url("http://localhost:8203", what="x", allow_private_http=()).endswith("8203")
    assert validate_url("http://edge-sentinel:8203", what="x", allow_private_http=("edge-sentinel",))
    with pytest.raises(RegistryError):
        validate_url("http://edge-sentinel:8203", what="x", allow_private_http=())


def test_bad_entries_are_skipped_and_listed_not_half_configured():
    doc = _doc()
    doc["products"].append({"id": "Bad Id", "kind": "security", "api_base": "http://127.0.0.1:1", "ui_url": "http://127.0.0.1:1"})
    doc["products"].append({"id": "other", "kind": "security", "api_base": "http://127.0.0.1:1", "ui_url": "http://127.0.0.1:1", "surprise": 1})
    reg = parse_registry(doc)
    assert list(reg.products) == ["edge-sentinel"] and len(reg.errors) == 2


@pytest.mark.parametrize("bad", [
    {"schema": "other"}, {"schema": "aos.products/v1", "products": "x"}, [], {"schema": "aos.products/v1", "products": [], "extra": 1},
    {"schema": "aos.products/v1", "products": [], "allow_private_http": ["has.dot"]}])
def test_a_bad_file_yields_no_products(bad):
    reg = parse_registry(bad)
    assert not reg.products and reg.errors


def test_secrets_are_file_references_never_inline_and_capabilities_stay_in_their_domain():
    assert parse_registry(_doc(auth={"service_token_file": "/run/secrets/a\nb"})).errors
    assert parse_registry(_doc(capabilities=["quality.qa_info"])).errors          # wrong domain for a security product
    assert not parse_registry(_doc(capabilities=["security.sentinel_posture"])).errors
    assert parse_registry(_doc(approvals={"mode": "auto", "attribute_to": "product_user"})).errors   # no auto-approval, ever
    assert parse_registry(_doc(auth={"service_token": "inline-secret"})).errors


def test_load_registry_handles_yaml_json_missing_and_garbage(tmp_path):
    y = tmp_path / "p.yaml"
    y.write_text("schema: aos.products/v1\nproducts:\n  - {id: a, kind: security, api_base: 'http://127.0.0.1:1', ui_url: 'http://127.0.0.1:1'}\n")
    assert load_registry(y).get("a")
    j = tmp_path / "p.json"
    j.write_text(json.dumps(_doc()))
    assert load_registry(j).get("edge-sentinel")
    assert load_registry(tmp_path / "missing.yaml").errors
    g = tmp_path / "g.yaml"
    g.write_text(":\n  - [")
    assert load_registry(g).errors and not load_registry(g).products

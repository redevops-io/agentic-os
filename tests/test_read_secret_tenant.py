"""Where ``read_secret``'s tenant comes from, and what the runtime-contracts credential gate (>= v0.3.15) does with it.

The gate refuses a credential request unless the request tenant equals the authority principal's tenant and the secret
reference's namespace equals that tenant. ``read_secret`` issues its own one-call authority, so the tenant it puts in
that authority is what the gate can protect. The rule (agentic_os/secrets/resolve.py) is:

    explicit ``tenant`` argument  >  ``REDEVOPS_LOCAL_TENANT``  >  single-user rule (the tenant is the reference's namespace)

These tests pin each source, the refusals (a tenant that differs from the namespace, an empty or malformed tenant, a
configured tenant that the reference does not match) and that no refusal returns or logs the secret. They need
runtime-contracts >= 0.3.15: on v0.3.14 the contract has no gate and the refusal tests fail, which is the point.
"""
from __future__ import annotations

import logging

import pytest

from agentic_os.app.llm_settings import LLMSettings, llm_api_key
from agentic_os.secrets import read_secret, store_secret
from agentic_os.secrets.resolve import LOCAL_TENANT_ENV, local_tenant

SECRET = "sk-TENANT-TEST-must-never-appear-in-a-log-or-a-refusal"
KEY = bytes(range(32))


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """A plain-file store in namespace ``llm`` and an encrypted store in namespace ``creds``; no ambient local tenant."""
    monkeypatch.setenv("REDEVOPS_SECRET_DIR", str(tmp_path / "plain"))
    monkeypatch.delenv(LOCAL_TENANT_ENV, raising=False)
    enc = {"root": str(tmp_path / "enc"), "key": KEY}
    store_secret("file:llm/provider", SECRET)
    store_secret("encrypted-file:creds/api", SECRET, **enc)
    return enc


def _refusals(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "agentic_os.secrets.resolve" and "refused" in r.getMessage()]


# ── (i) the default single-user path works ──────────────────────────────────────────────────────────────────────

def test_default_single_user_path_reads_a_namespaced_reference(stores):
    assert read_secret("file:llm/provider") == SECRET
    assert read_secret("encrypted-file:creds/api", **stores) == SECRET      # the production-grade broker, a different namespace


def test_default_single_user_path_reads_an_env_reference(monkeypatch):
    monkeypatch.delenv(LOCAL_TENANT_ENV, raising=False)
    monkeypatch.setenv("TENANT_TEST_KEY", SECRET)
    assert read_secret("env:TENANT_TEST_KEY") == SECRET


def test_the_llm_key_path_uses_the_same_rule(stores):
    assert llm_api_key(LLMSettings(key_ref="file:llm/provider")) == SECRET


def test_the_default_rule_names_the_namespace_as_the_tenant(monkeypatch):
    monkeypatch.delenv(LOCAL_TENANT_ENV, raising=False)
    assert local_tenant("llm") == "llm"
    assert local_tenant("") == "default"                                    # an env: reference has no namespace


# ── (ii) an explicit tenant that is not the namespace is refused ────────────────────────────────────────────────

def test_an_explicit_matching_tenant_reads(stores):
    assert read_secret("file:llm/provider", tenant="llm") == SECRET


@pytest.mark.parametrize("tenant", ["other", "default", "LLM", "llm "])
def test_an_explicit_tenant_that_differs_from_the_namespace_is_refused(stores, caplog, tenant):
    caplog.set_level(logging.INFO)
    got = read_secret("file:llm/provider", tenant=tenant)
    assert got is None
    if tenant.strip() == tenant:
        assert any("TENANT_MISMATCH" in m for m in _refusals(caplog)), caplog.text
    assert SECRET not in caplog.text


def test_an_explicit_tenant_is_never_repaired_to_the_namespace(stores):
    """The namespace is the default for the omitted tenant only; naming another tenant must not fall back to it."""
    assert read_secret("file:llm/provider", tenant="llm") == SECRET
    assert read_secret("file:llm/provider", tenant="someone-else") is None
    assert read_secret("encrypted-file:creds/api", tenant="llm", **stores) is None      # a store key does not widen the tenant


def test_an_explicit_tenant_given_among_the_store_keywords_is_the_explicit_tenant(stores):
    """``llm_admin.set_provider(**store_cfg)`` forwards its keywords to read_secret, so a tenant there is the explicit one."""
    assert read_secret("file:llm/provider", **{"tenant": "llm"}) == SECRET
    assert read_secret("file:llm/provider", **{"tenant": "globex"}) is None


# ── (iii) a missing or malformed tenant is refused ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tenant", ["", " ", "  acme", "acme  ", "ac\nme", "\t", 7, b"llm"])
def test_an_empty_or_malformed_tenant_is_refused_for_a_namespaced_reference(stores, caplog, tenant):
    caplog.set_level(logging.INFO)
    assert read_secret("file:llm/provider", tenant=tenant) is None
    assert SECRET not in caplog.text


@pytest.mark.parametrize("tenant", ["", " ", "  ", "\t", " default", "default "])
def test_an_empty_or_blank_tenant_is_refused_for_an_env_reference_too(monkeypatch, caplog, tenant):
    """An env: reference has no namespace, so the contract alone would accept a blank tenant that is equal on both sides."""
    monkeypatch.setenv("TENANT_TEST_KEY", SECRET)
    caplog.set_level(logging.INFO)
    assert read_secret("env:TENANT_TEST_KEY", tenant=tenant) is None
    assert any("TENANT_INVALID" in m for m in _refusals(caplog)), caplog.text
    assert SECRET not in caplog.text


def test_the_tenant_is_not_taken_from_the_reference_when_it_is_named_or_configured(stores, monkeypatch):
    """A reference is configuration or model-proposed input; it supplies the tenant only under the single-user rule."""
    assert read_secret("file:llm/provider", tenant="acme") is None
    monkeypatch.setenv(LOCAL_TENANT_ENV, "acme")
    assert read_secret("file:llm/provider") is None
    assert read_secret("encrypted-file:creds/api", **stores) is None


# ── the configured local tenant ─────────────────────────────────────────────────────────────────────────────────

def test_a_configured_local_tenant_must_equal_the_namespace(stores, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv(LOCAL_TENANT_ENV, "llm")
    assert read_secret("file:llm/provider") == SECRET                      # same namespace: allowed
    assert read_secret("encrypted-file:creds/api", **stores) is None       # another namespace of the same store: refused
    assert any("TENANT_MISMATCH" in m for m in _refusals(caplog)), caplog.text
    assert SECRET not in caplog.text


def test_a_configured_local_tenant_also_names_the_tenant_of_env_references(monkeypatch):
    monkeypatch.setenv("TENANT_TEST_KEY", SECRET)
    monkeypatch.setenv(LOCAL_TENANT_ENV, "acme")
    assert read_secret("env:TENANT_TEST_KEY") == SECRET
    assert read_secret("env:TENANT_TEST_KEY", tenant="acme") == SECRET


def test_the_argument_overrides_the_configured_tenant_and_is_still_checked(stores, monkeypatch):
    monkeypatch.setenv(LOCAL_TENANT_ENV, "acme")
    assert read_secret("file:llm/provider", tenant="llm") == SECRET        # explicit tenant = namespace
    assert read_secret("file:llm/provider", tenant="acme") is None         # the configured tenant is not an exemption


@pytest.mark.parametrize("configured", [" ", "\t", " acme", "acme ", "a\nb"])
def test_a_malformed_configured_tenant_is_refused_and_not_ignored(stores, monkeypatch, configured):
    monkeypatch.setenv(LOCAL_TENANT_ENV, configured)
    assert read_secret("file:llm/provider") is None
    assert read_secret("encrypted-file:creds/api", **stores) is None


def test_an_empty_configured_tenant_means_not_configured(stores, monkeypatch):
    """compose files export ``REDEVOPS_LOCAL_TENANT=${REDEVOPS_LOCAL_TENANT:-}``: empty is unset, the single-user rule applies."""
    monkeypatch.setenv(LOCAL_TENANT_ENV, "")
    assert read_secret("file:llm/provider") == SECRET


def test_the_llm_key_degrades_to_the_sentinel_when_the_tenant_does_not_match(stores, monkeypatch):
    monkeypatch.setenv(LOCAL_TENANT_ENV, "acme")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key-must-not-be-used")
    assert llm_api_key(LLMSettings(key_ref="file:llm/provider")) == "EMPTY"


# ── (vi) nothing logs or returns the secret ─────────────────────────────────────────────────────────────────────

def test_no_log_line_carries_the_secret_on_success_or_refusal(stores, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    assert read_secret("file:llm/provider") == SECRET
    read_secret("file:llm/provider", tenant="other")
    read_secret("file:llm/provider", tenant="")
    monkeypatch.setenv(LOCAL_TENANT_ENV, "acme")
    read_secret("file:llm/provider")
    assert SECRET not in caplog.text
    for r in caplog.records:
        assert SECRET not in r.getMessage() and SECRET not in repr(r.args) and SECRET not in str(r.exc_text)


def test_an_absent_or_unreadable_secret_logs_the_error_type_only(stores, caplog):
    caplog.set_level(logging.DEBUG)
    assert read_secret("file:llm/absent") is None
    records = [r.getMessage() for r in caplog.records if r.name == "agentic_os.secrets.resolve"]
    assert records and all(SECRET not in m for m in records)
    assert any(m.endswith("SecretAccessError") or m.endswith("Error") for m in records), records


def test_a_refusal_returns_exactly_none(stores):
    for tenant in ("other", "", " ", "llm "):
        assert read_secret("file:llm/provider", tenant=tenant) is None

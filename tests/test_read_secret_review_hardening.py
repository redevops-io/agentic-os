"""Hardening pinned by the independent review of read_secret (xrepo/review-read-secret.md): RS-03 (writes follow the tenant), RS-06 (backslash and
non-printable characters in a reference), RS-11 (the backend token is not echoed) and the three mutants the first tests did not kill (RS-08):
a non-printable tenant, the authority's expiry and the authority's scope."""
import logging

import pytest

import agentic_os.secrets.resolve as resolve
from agentic_os.secrets import build_secret_store, read_secret, store_secret
from agentic_os.secrets.resolve import LOCAL_TENANT_ENV, SecretReferenceError, parse_ref

SECRET = "sk-REVIEW-must-never-appear"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("REDEVOPS_SECRET_DIR", str(tmp_path))
    monkeypatch.delenv(LOCAL_TENANT_ENV, raising=False)
    store_secret("file:llm/provider", "own")
    store_secret("file:victim/token", "victim-original")


# ── RS-03: a write follows the same tenant as a read ────────────────────────────────────────────────────────────

def test_a_configured_local_tenant_also_limits_writes(store, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv(LOCAL_TENANT_ENV, "llm")
    store_secret("file:llm/provider", "rotated")                       # inside the pin: allowed
    with pytest.raises(SecretReferenceError):
        store_secret("file:victim/token", "overwritten")               # outside the pin: refused, nothing written
    assert "not written" in caplog.text and "overwritten" not in caplog.text
    monkeypatch.delenv(LOCAL_TENANT_ENV)
    assert read_secret("file:victim/token") == "victim-original"
    assert read_secret("file:llm/provider") == "rotated"


def test_an_explicit_tenant_limits_a_write_the_same_way(store):
    with pytest.raises(SecretReferenceError):
        store_secret("file:victim/token", "overwritten", tenant="llm")
    store_secret("file:victim/token", "by-its-own-tenant", tenant="victim")      # the namespace itself is allowed
    assert read_secret("file:victim/token") == "by-its-own-tenant"


@pytest.mark.parametrize("tenant", ["", " ", "a\x01b", 7])
def test_a_malformed_tenant_cannot_write(store, tenant):
    with pytest.raises(SecretReferenceError):
        store_secret("file:llm/provider", "overwritten", tenant=tenant)
    assert read_secret("file:llm/provider") == "own"


def test_the_default_write_path_is_unchanged(store):
    store_secret("file:llm/provider", "rotated")                       # single-user rule: the namespace is the tenant
    assert read_secret("file:llm/provider") == "rotated"
    with pytest.raises(SecretReferenceError):
        store_secret("env:SOME_NAME", "x")                              # env stays read-only


# ── RS-06: backslash and non-printable characters in a reference ────────────────────────────────────────────────

@pytest.mark.parametrize("ref", ["file:llm/..\\victim\\token", "file:llm\\x/y", "file:llm/x FORGED", "file:llm/x\x1b[31m",
                                 "file:llm/a\tb", "file:llm/a\x7fb", "env:NA​ME", "file:llm/a‮b"])
def test_parse_ref_refuses_backslash_and_non_printable_characters(ref):
    with pytest.raises(SecretReferenceError):
        parse_ref(ref)
    with pytest.raises(SecretReferenceError):                          # and so does every caller
        read_secret(ref)
    with pytest.raises(SecretReferenceError):
        store_secret(ref, "v")


def test_parse_ref_still_accepts_ordinary_references():
    assert parse_ref("file:llm/provider") == ("file", "llm", "provider")
    assert parse_ref("keepassxc:Redevops/LLM") == ("keepassxc", "Redevops", "LLM")
    assert parse_ref("file:my ns/p q") == ("file", "my ns", "p q")        # spaces and non-ASCII letters are printable
    assert parse_ref("file:équipe/clé") == ("file", "équipe", "clé")
    assert parse_ref("env:OPENAI_API_KEY") == ("env", "", "OPENAI_API_KEY")


# ── RS-11: the backend token is not echoed ──────────────────────────────────────────────────────────────────────

def test_an_unknown_backend_error_does_not_echo_the_token():
    pasted = "sk-live-ABC123"
    with pytest.raises(ValueError) as e:
        read_secret(f"{pasted}:abc/def")
    assert pasted.lower() not in str(e.value).lower()
    with pytest.raises(ValueError) as e2:
        build_secret_store(pasted)
    assert pasted.lower() not in str(e2.value).lower() and "one of" in str(e2.value)


# ── RS-08: the three mutants the first tests did not kill ───────────────────────────────────────────────────────

@pytest.mark.parametrize("tenant", ["a\x01b", "a​b", "a b", "a\x7fb", "‮ab"])
def test_a_tenant_with_a_control_or_invisible_character_is_refused_even_for_an_env_reference(monkeypatch, caplog, tenant):
    """The contract only compares the two tenants, so an equal pair containing an invisible character would pass for ``env:``."""
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("REVIEW_TEST_KEY", SECRET)
    assert read_secret("env:REVIEW_TEST_KEY", tenant=tenant) is None
    assert "TENANT_INVALID" in caplog.text and SECRET not in caplog.text


def test_the_authority_read_secret_issues_is_one_call_and_narrow(store, monkeypatch):
    """It sets no expiry (the grant TTL, 60 s, bounds it), names its own capability as the only scope, is a service principal and
    asks for no more than 60 seconds: widening any of these must be a visible change."""
    seen = {}
    real = resolve.build_credential_broker

    def spy(backend, **cfg):
        store_, broker = real(backend, **cfg)
        orig = broker.grant

        def grant(request, *, authority_context):
            seen["authority"], seen["request"] = authority_context, request
            return orig(request, authority_context=authority_context)
        broker.grant = grant
        return store_, broker
    monkeypatch.setattr(resolve, "build_credential_broker", spy)
    assert read_secret("file:llm/provider", capability="llm.invoke", mission="m1") == "own"
    a, r = seen["authority"], seen["request"]
    assert a.expires_at == "" and a.parent_digest == "" and a.depth == 0
    assert a.scope == ("llm.invoke",) and r.requirement.required_scopes == ("llm.invoke",)
    assert "*" not in a.scope and a.principal.kind == "service" and a.principal.tenant == "llm" and a.authority_id == "m1"
    assert r.requested_ttl_seconds == 60 and r.requirement.max_ttl_seconds == 60 and r.tenant_id == "llm"
    assert r.authority_context_id == a.chain_ref

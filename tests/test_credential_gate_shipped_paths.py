"""The runtime-contracts credential gate (>= v0.3.15), driven through every path of this repository that issues a credential.

Paths: ``read_secret`` (the LLM key, KeePassXC and the Sidekick provider settings), ``Executor._provision`` (the mission
runtime: ConnectionManager and ServiceIdentityBroker references, env references), and the brokers themselves
(``LocalCredentialBroker`` for the dev stores, ``ProductionCredentialBroker`` for the encrypted/keyring/vault stores).

For each: the matching tenant reads; a tenant that is not the secret's namespace, an authority with no tenant, an expired
authority and a request that does not name the authority are refused, with the contract's reason code; and no refusal
carries the secret. On runtime-contracts v0.3.14 (no gate) the refusal tests below fail, because the read succeeds.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_os.connections import ConnectionManager
from agentic_os.mission.executor import Executor, InMemoryOperatorClient, OperatorError
from agentic_os.mission.types import Node
from agentic_os.provisioning import ServiceIdentityBroker
from agentic_os.secrets import EncryptedFileSecretStore, ProductionCredentialBroker, build_credential_broker, read_secret, store_secret
from runtime_contracts import AuthorityContext, CredentialRequirement, LocalCredentialBroker, PrincipalRef, SecretRef
from runtime_contracts.protocol.secrets import CredentialRequest
from runtime_contracts.secrets_local.broker import CredentialDenied

KEY = bytes(range(32))
SECRET = "sk-GATE-TEST-must-never-appear-in-a-refusal"


def _authority(tenant, *, scope=("cred:use",), expires_at="", authority_id="gate-test"):
    return AuthorityContext(authority_id=authority_id, principal=PrincipalRef(id="p", kind="service", tenant=tenant),
                            purpose="gate-test", scope=scope, expires_at=expires_at)


def _run(broker, ref, authority):
    """Run a credentialed node through Executor._provision (the mission path); returns the bytes the handler received."""
    seen = {}

    def creds_for(node):
        return (CredentialRequirement(name="cred", required_scopes=("cred:use",), secret_ref=ref, max_ttl_seconds=60),)
    ex = Executor(InMemoryOperatorClient({"do": lambda i, s: seen.update(v=s["cred"].bytes()) or {}}),
                  authority=authority, broker=broker, credentials_for=creds_for)
    ex.run(Node(capability="do", operator="op"), {})
    return seen.get("v")


@pytest.fixture
def prod(tmp_path):
    store = EncryptedFileSecretStore(str(tmp_path / "enc"), key=KEY)
    ref = store.put(namespace="acme", path="api", value=SECRET.encode())
    return ProductionCredentialBroker(store), ref


# ── Executor._provision: the mission path ───────────────────────────────────────────────────────────────────────

def test_mission_path_reads_when_the_authority_tenant_is_the_namespace(prod):
    broker, ref = prod
    assert _run(broker, ref, _authority("acme")) == SECRET.encode()


@pytest.mark.parametrize("tenant", ["globex", "ACME", "acme2", ""])
def test_mission_path_refuses_a_tenant_that_is_not_the_namespace(prod, tenant):
    """The cross-tenant read: it succeeds on v0.3.14 (no gate) and is refused from v0.3.15."""
    broker, ref = prod
    with pytest.raises(OperatorError) as e:
        _run(broker, ref, _authority(tenant))
    assert "TENANT_MISMATCH" in str(e.value) and SECRET not in str(e.value)


def test_mission_path_refuses_an_authority_with_no_tenant_for_an_env_reference(monkeypatch):
    monkeypatch.setenv("GATE_TEST_KEY", SECRET)
    broker = LocalCredentialBroker(build_credential_broker("env")[0])
    ref = SecretRef(provider="env", key="GATE_TEST_KEY")
    assert _run(broker, ref, _authority("acme")) == SECRET.encode()                 # a tenant named: allowed
    with pytest.raises(OperatorError) as e:
        _run(broker, ref, _authority(""))                                            # none named: refused
    assert "TENANT_MISMATCH" in str(e.value) and SECRET not in str(e.value)


def test_mission_path_refuses_an_expired_authority_and_allows_an_unexpired_one(prod):
    broker, ref = prod
    now = datetime.now(timezone.utc)
    assert _run(broker, ref, _authority("acme", expires_at=(now + timedelta(hours=1)).isoformat())) == SECRET.encode()
    for expired in ((now - timedelta(seconds=5)).isoformat(), "not-a-date", (now - timedelta(days=1)).replace(tzinfo=None).isoformat()):
        with pytest.raises(OperatorError) as e:
            _run(broker, ref, _authority("acme", expires_at=expired))
        assert "AUTHORITY_EXPIRED" in str(e.value) and SECRET not in str(e.value)


def test_mission_path_refuses_when_the_scope_is_missing_before_it_reads(prod):
    broker, ref = prod
    with pytest.raises(OperatorError) as e:
        _run(broker, ref, _authority("acme", scope=("something:else",)))
    assert "INSUFFICIENT_AUTHORITY" in str(e.value) and SECRET not in str(e.value)


# ── the broker itself: authority id, tenant, namespace ──────────────────────────────────────────────────────────

def _request(ref, *, tenant, authority_context_id, name="cred"):
    return CredentialRequest(request_id="r1", mission_id="m1", node_id="n1", capability_id="cap", tenant_id=tenant,
                             authority_context_id=authority_context_id,
                             requirement=CredentialRequirement(name=name, required_scopes=("cred:use",), secret_ref=ref, max_ttl_seconds=60),
                             requested_ttl_seconds=60)


def test_the_broker_grants_only_when_authority_tenant_and_namespace_agree(prod):
    broker, ref = prod
    auth = _authority("acme")
    grant = broker.grant(_request(ref, tenant="acme", authority_context_id=auth.chain_ref), authority_context=auth)
    material = broker.redeem(grant, capability_id="cap", mission_id="m1")
    assert material.bytes() == SECRET.encode()
    material.destroy()
    broker.revoke_grant(grant.grant_id, reason="test")


@pytest.mark.parametrize("request_tenant,authority_tenant,reason", [
    ("globex", "acme", "TENANT_MISMATCH"),     # request tenant differs from the authority's
    ("acme", "globex", "TENANT_MISMATCH"),     # authority tenant differs from the request's (and the namespace)
    ("globex", "globex", "TENANT_MISMATCH"),   # both agree with each other, not with the secret's namespace
    ("acme", "", "TENANT_MISMATCH"),           # an authority with no tenant
    ("", "", "TENANT_MISMATCH"),               # no tenant anywhere
])
def test_the_broker_refuses_a_tenant_that_is_not_the_namespace(prod, request_tenant, authority_tenant, reason):
    broker, ref = prod
    auth = _authority(authority_tenant)
    with pytest.raises(CredentialDenied) as e:
        broker.grant(_request(ref, tenant=request_tenant, authority_context_id=auth.chain_ref), authority_context=auth)
    assert reason in e.value.reason and SECRET not in e.value.reason and SECRET not in str(e.value)


def test_the_broker_refuses_a_request_that_does_not_name_the_authority(prod):
    broker, ref = prod
    auth = _authority("acme")
    for wrong in ("another-authority", "", auth.chain_ref + "x"):
        with pytest.raises(CredentialDenied) as e:
            broker.grant(_request(ref, tenant="acme", authority_context_id=wrong), authority_context=auth)
        assert "AUTHORITY_MISMATCH" in e.value.reason and SECRET not in e.value.reason
    # the authority id itself is accepted as the name, as is the chain ref
    ok = broker.grant(_request(ref, tenant="acme", authority_context_id=auth.authority_id), authority_context=auth)
    broker.revoke_grant(ok.grant_id, reason="test")


def test_the_broker_refuses_an_expired_authority(prod):
    broker, ref = prod
    auth = _authority("acme", expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    with pytest.raises(CredentialDenied) as e:
        broker.grant(_request(ref, tenant="acme", authority_context_id=auth.chain_ref), authority_context=auth)
    assert "AUTHORITY_EXPIRED" in e.value.reason and SECRET not in e.value.reason


def test_a_grant_cannot_be_redeemed_for_another_mission_or_capability(prod):
    broker, ref = prod
    auth = _authority("acme")
    grant = broker.grant(_request(ref, tenant="acme", authority_context_id=auth.chain_ref), authority_context=auth)
    for kw in ({"capability_id": "other", "mission_id": "m1"}, {"capability_id": "cap", "mission_id": "m2"}):
        with pytest.raises(CredentialDenied) as e:
            broker.redeem(grant, **kw)
        assert SECRET not in str(e.value)
    broker.revoke_grant(grant.grant_id, reason="test")


def test_a_development_broker_gates_the_same_way(tmp_path, monkeypatch):
    monkeypatch.setenv("REDEVOPS_SECRET_DIR", str(tmp_path))
    store, broker = build_credential_broker("file")
    assert broker.assurance_level == "development"
    ref = store.put(namespace="llm", path="provider", value=SECRET.encode())
    assert _run(broker, ref, _authority("llm")) == SECRET.encode()
    with pytest.raises(OperatorError) as e:
        _run(broker, ref, _authority("default"))
    assert "TENANT_MISMATCH" in str(e.value) and SECRET not in str(e.value)


# ── feature references that travel through the mission path ─────────────────────────────────────────────────────

def test_a_connection_reference_is_read_only_for_its_own_namespace(tmp_path):
    store = EncryptedFileSecretStore(str(tmp_path), key=KEY)
    cm = ConnectionManager(store=store)
    cm.connect("pdl", api_key=SECRET)
    ref = cm.secret_ref("pdl")
    broker = ProductionCredentialBroker(store)
    assert _run(broker, ref, _authority(ref.namespace)) == SECRET.encode()
    with pytest.raises(OperatorError) as e:
        _run(broker, ref, _authority("acme"))                     # a real tenant is not the connection's feature namespace
    assert "TENANT_MISMATCH" in str(e.value) and SECRET not in str(e.value)


def test_a_service_identity_reference_is_read_only_for_its_own_namespace(tmp_path):
    store = EncryptedFileSecretStore(str(tmp_path), key=KEY)
    ident = ServiceIdentityBroker(store=store).create_identity("revenue-agent", tenant="acme", roles=("cred:use",))
    broker = ProductionCredentialBroker(store)
    token = store._read(ident.secret_ref)
    assert _run(broker, ident.secret_ref, _authority(ident.secret_ref.namespace)) == token
    with pytest.raises(OperatorError) as e:
        _run(broker, ident.secret_ref, _authority("acme"))
    assert "TENANT_MISMATCH" in str(e.value) and token.decode() not in str(e.value)


# ── read_secret: the LLM key, KeePassXC and the provider settings ───────────────────────────────────────────────

def test_the_llm_key_path_refuses_a_cross_tenant_read(tmp_path, monkeypatch):
    from agentic_os.app.llm_settings import LLMKeyUnavailable, LLMSettings, llm_api_key
    monkeypatch.setenv("REDEVOPS_SECRET_DIR", str(tmp_path))
    store_secret("file:llm/provider", SECRET)
    assert llm_api_key(LLMSettings(key_ref="file:llm/provider")) == SECRET
    monkeypatch.setenv("REDEVOPS_LOCAL_TENANT", "globex")                 # the deployment's tenant is not the secret's namespace
    with pytest.raises(LLMKeyUnavailable):
        llm_api_key(LLMSettings(key_ref="file:llm/provider"))


def test_keepassxc_refuses_a_cross_tenant_read():
    from tests.test_keepassxc_store import FakeCli, MASTER   # noqa: PLC0415 - the fake keepassxc-cli of that suite
    import os
    os.environ["KP_MASTER"] = MASTER
    try:
        from agentic_os.secrets import KeePassXCSecretStore
        fake = FakeCli()
        KeePassXCSecretStore("/db/vault.kdbx", password_env="KP_MASTER", runner=fake).put(namespace="Redevops", path="LLM", value=SECRET.encode())
        cfg = {"database": "/db/vault.kdbx", "password_env": "KP_MASTER", "runner": fake}
        assert read_secret("keepassxc:Redevops/LLM", **cfg) == SECRET
        assert read_secret("keepassxc:Redevops/LLM", tenant="Redevops", **cfg) == SECRET
        assert read_secret("keepassxc:Redevops/LLM", tenant="globex", **cfg) is None
    finally:
        os.environ.pop("KP_MASTER", None)


def test_the_provider_settings_skill_cannot_read_or_overwrite_another_tenants_key(tmp_path, monkeypatch):
    """``set_provider`` reads the previous key (for the rollback) with ``read_secret(key_ref, **store_cfg)`` and writes the new one with
    ``store_secret(key_ref, ..., **store_cfg)``: a tenant that is not the reference's namespace reads nothing and writes nothing."""
    from agentic_os.app import llm_admin
    import agentic_os.secrets.resolve as resolve
    monkeypatch.setenv("REDEVOPS_SECRET_DIR", str(tmp_path / "s"))
    store_secret("file:llm/provider", "previous-key")
    seen = []
    real = resolve.read_secret

    def spy(ref, **kw):
        out = real(ref, **kw)
        seen.append(out)
        return out
    monkeypatch.setattr(resolve, "read_secret", spy)
    settings = str(tmp_path / "llm.json")
    kw = dict(base_url="http://127.0.0.1:9/v1", model="m", key_ref="file:llm/provider", key_value="new-key", test=False)
    llm_admin.set_provider(settings, **kw)
    assert seen == ["previous-key"]
    seen.clear()
    kw2 = dict(kw, key_value="overwritten")
    with pytest.raises(resolve.SecretReferenceError):                 # a cross-tenant request is refused before anything is written
        llm_admin.set_provider(settings, tenant="globex", **kw2)
    assert seen == [None]
    assert real("file:llm/provider") == "new-key"                     # the key the first (own-namespace) call stored is untouched

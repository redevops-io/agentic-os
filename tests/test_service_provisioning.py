"""P0 contract — ServiceIdentityBroker + ServiceProvisioner: machine-identity lifecycle, the
identity-preference ladder, credential invisibility, and uninstall→revoke as a contract obligation.
"""
from __future__ import annotations

import json

import pytest

from agentic_os.provisioning import (
    IdentityRung, RegistryRunner, ServiceIdentityBroker, ServiceProvisioner, ServiceSpec,
    ServiceState, bootstrap, pick_rung)
from agentic_os.secrets import EncryptedFileSecretStore, ProductionCredentialBroker
from runtime_contracts.secrets_local.store import SecretAccessError

KEY = bytes(range(32))


def _store(tmp_path):
    return EncryptedFileSecretStore(str(tmp_path), key=KEY)


# ── ServiceIdentityBroker ───────────────────────────────────────────────────────────────────────────────

def test_create_identity_mints_credential_into_the_store_not_the_record(tmp_path):
    store = _store(tmp_path)
    idb = ServiceIdentityBroker(store=store)
    ident = idb.create_identity("revenue-agent", tenant="acme", roles=("crm:read",))
    assert ident.rung == IdentityRung.SERVICE_TOKEN and ident.status == "active"
    assert ident.principal().kind == "service" and ident.principal().id == "svc:revenue-agent"

    token = store._read(ident.secret_ref)                 # the value lives ONLY in the store
    assert token and len(token) >= 30                     # high-entropy
    # credential-invisible: the token never appears in the identity's own view
    assert token.decode() not in json.dumps(ident.as_dict())
    assert "credential_ref" in ident.as_dict() and "fingerprint" in ident.as_dict()["credential_ref"]


def test_identity_resolves_through_the_credential_broker(tmp_path):
    from agentic_os.mission.executor import Executor, InMemoryOperatorClient
    from agentic_os.mission.types import Node
    from runtime_contracts import AuthorityContext, CredentialRequirement, PrincipalRef

    store = _store(tmp_path)
    idb = ServiceIdentityBroker(store=store)
    ident = idb.create_identity("revenue-agent")
    token = store._read(ident.secret_ref)

    seen = {}
    ex = Executor(InMemoryOperatorClient({"svc.call": lambda i, s: seen.update(v=s["svc"].bytes()) or {}}),
                  authority=AuthorityContext(authority_id="a", principal=PrincipalRef(id="p"),
                                             scope=("svc:use",)),
                  broker=ProductionCredentialBroker(store),
                  credentials_for=lambda n: (CredentialRequirement(
                      name="svc", required_scopes=("svc:use",), secret_ref=ident.secret_ref,
                      max_ttl_seconds=60),))
    ex.run(Node(capability="svc.call", operator="op"), {})
    assert seen["v"] == token                             # identity → broker → capability


def test_identity_preference_ladder():
    assert pick_rung(()) == IdentityRung.SERVICE_TOKEN                                  # internal default
    assert pick_rung(("integration-user", "machine-account")) == IdentityRung.MACHINE_ACCOUNT
    assert pick_rung(("human-admin",)) == IdentityRung.HUMAN_ADMIN


def test_human_admin_last_resort_carries_a_trust_warning(tmp_path):
    idb = ServiceIdentityBroker(store=_store(tmp_path))
    ident = idb.create_identity("legacy-app", supports=("human-admin",))
    assert ident.rung == IdentityRung.HUMAN_ADMIN and ident.trust_warning
    assert "blast radius" in ident.trust_warning
    # and a normal service token carries no warning
    assert idb.create_identity("modern-app").trust_warning == ""


def test_rotate_mints_a_fresh_credential(tmp_path):
    store = _store(tmp_path)
    idb = ServiceIdentityBroker(store=store)
    ident = idb.create_identity("svc")
    v1 = store._read(ident.secret_ref)
    ident2 = idb.rotate("svc")
    v2 = store._read(ident2.secret_ref)
    assert v1 != v2 and ident2.status == "active" and ident2.principal_id == ident.principal_id


def test_revoke_removes_the_credential(tmp_path):
    store = _store(tmp_path)
    idb = ServiceIdentityBroker(store=store)
    ident = idb.create_identity("svc")
    idb.revoke("svc")
    assert idb.get("svc").status == "revoked"
    with pytest.raises(SecretAccessError):
        store._read(ident.secret_ref)


def test_read_only_store_refuses_provisioning():
    from agentic_os.secrets import build_secret_store
    from agentic_os.provisioning import IdentityError
    idb = ServiceIdentityBroker(store=build_secret_store("env"))
    with pytest.raises(IdentityError):
        idb.create_identity("svc")


# ── ServiceProvisioner ──────────────────────────────────────────────────────────────────────────────────

def _prov(tmp_path, **runner_kw):
    idb = ServiceIdentityBroker(store=_store(tmp_path))
    return ServiceProvisioner(identity_broker=idb, runner=RegistryRunner(**runner_kw)), idb


def test_install_creates_identity_and_registers(tmp_path):
    prov, _ = _prov(tmp_path)
    rec = prov.install(ServiceSpec("revenue-agent", domain="crm",
                                   capabilities=("crm.company.read", "crm.opportunity.create")))
    assert rec.state == ServiceState.INSTALLED and rec.endpoint.endswith("/revenue-agent")
    assert rec.identity.rung == IdentityRung.SERVICE_TOKEN
    assert "crm.opportunity.create" in rec.capabilities


def test_full_lifecycle_and_uninstall_revokes_identity(tmp_path):
    """The contract obligation: uninstall MUST revoke the identity/credential (no orphaned service accounts)."""
    prov, idb = _prov(tmp_path)
    store = idb._store
    rec = prov.install(ServiceSpec("support-agent", domain="support"))
    ref = rec.identity.secret_ref
    assert store._read(ref)                                  # credential exists after install

    prov.initialize("support-agent")
    assert prov.health("support-agent") is True and prov._require("support-agent").state == ServiceState.HEALTHY

    prov.uninstall("support-agent")
    assert prov._require("support-agent").state == ServiceState.UNINSTALLED
    assert idb.get("support-agent").status == "revoked"
    with pytest.raises(SecretAccessError):                   # <-- credential revoked by uninstall
        store._read(ref)


def test_health_reflects_the_runner(tmp_path):
    prov, _ = _prov(tmp_path, health_fn=lambda name, ep: False)  # runner reports unhealthy
    prov.install(ServiceSpec("flaky"))
    prov.initialize("flaky")
    assert prov.health("flaky") is False and prov._require("flaky").state == ServiceState.FAILED


def test_registry_is_credential_invisible(tmp_path):
    prov, idb = _prov(tmp_path)
    prov.install(ServiceSpec("revenue-agent", capabilities=("crm.read",)))
    token = idb._store._read(prov.secret_ref("revenue-agent")).decode()
    reg = prov.registry()
    assert reg[0]["identity"]["rung"] == IdentityRung.SERVICE_TOKEN
    assert token not in json.dumps(reg)                      # the registry never carries the value


def test_bootstrap_provisions_the_install_flow_band(tmp_path):
    idb = ServiceIdentityBroker(store=_store(tmp_path))
    prov = ServiceProvisioner(identity_broker=idb, runner=RegistryRunner())
    specs = [ServiceSpec("revenue-agent", domain="crm", capabilities=("crm.company.read",)),
             ServiceSpec("support-agent", domain="support", capabilities=("support.contact.upsert",))]
    bootstrap(specs, provisioner=prov)
    reg = {r["name"]: r for r in prov.registry()}
    assert reg["revenue-agent"]["healthy"] and reg["support-agent"]["healthy"]
    assert reg["revenue-agent"]["identity"]["status"] == "active"
    assert reg["support-agent"]["capabilities"] == ["support.contact.upsert"]

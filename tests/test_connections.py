"""P0 contract — ConnectionManager (connect/disconnect/status/scopes), class-aware + credential-invisible,
and the PDL first-class connector.
"""
from __future__ import annotations

import base64
import json

import pytest

from agentic_os.connections import (
    ConnectionManager, ConnectionRefused, IntegrationClass, default_registry)
from agentic_os.secrets import EncryptedFileSecretStore, ProductionCredentialBroker

KEY = bytes(range(32))
SECRET = "PDL-SECRET-do-not-log"


def _cm(tmp_path):
    store = EncryptedFileSecretStore(str(tmp_path), key=KEY)
    return ConnectionManager(store=store), store


# ── the four contract operations, agent-native (key-first) ──────────────────────────────────────────────

def test_connect_agent_native_stores_via_broker_seam_not_in_state(tmp_path):
    cm, store = _cm(tmp_path)
    st = cm.connect("pdl", api_key=SECRET)
    assert st.connected and st.integration_class == IntegrationClass.AGENT_NATIVE
    assert "data:person.search" in st.scopes

    # the secret is retrievable ONLY through the store at the recorded ref — never in the UI state
    ref = cm.secret_ref("pdl")
    assert store._read(ref) == SECRET.encode()
    blob = json.dumps([st.as_dict(), cm.catalog(), [s.as_dict() for s in cm.list()]], default=str)
    assert SECRET not in blob
    # at rest it is ciphertext, too
    assert SECRET.encode() not in (tmp_path / "data" / "pdl.enc").read_bytes()


def test_connect_then_capability_reads_credential_through_the_broker(tmp_path):
    """connect → the credential lands where a capability's CredentialBroker reads it — the provider becomes
    infrastructure the capability resolves by authority, not a key the user hands around."""
    from agentic_os.mission.executor import Executor, InMemoryOperatorClient
    from agentic_os.mission.types import Node
    from runtime_contracts import AuthorityContext, CredentialRequirement, PrincipalRef

    cm, store = _cm(tmp_path)
    cm.connect("pdl", api_key=SECRET)
    ref = cm.secret_ref("pdl")

    seen = {}
    broker = ProductionCredentialBroker(store)
    ex = Executor(InMemoryOperatorClient({"data.enrich": lambda i, s: seen.update(v=s["pdl"].bytes()) or {}}),
                  authority=AuthorityContext(authority_id="a", principal=PrincipalRef(id="p"),
                                             scope=("data:person.search",)),
                  broker=broker,
                  credentials_for=lambda n: (CredentialRequirement(
                      name="pdl", required_scopes=("data:person.search",), secret_ref=ref,
                      max_ttl_seconds=60),))
    ex.run(Node(capability="data.enrich", operator="op"), {})
    assert seen["v"] == SECRET.encode()


def test_disconnect_revokes_and_clears(tmp_path):
    cm, store = _cm(tmp_path)
    cm.connect("pdl", api_key=SECRET)
    ref = cm.secret_ref("pdl")
    cm.disconnect("pdl")
    assert cm.status("pdl").connected is False and cm.secret_ref("pdl") is None
    from runtime_contracts.secrets_local.store import SecretAccessError
    with pytest.raises(SecretAccessError):
        store._read(ref)


def test_status_and_scopes(tmp_path):
    cm, _ = _cm(tmp_path)
    assert cm.status("pdl").connected is False and cm.status("pdl").needs == "api_key"
    cm.connect("pdl", api_key=SECRET)
    assert cm.scopes("pdl") == ("data:person.search", "data:person.enrich")


# ── class-aware dispatch ────────────────────────────────────────────────────────────────────────────────

def test_ui_bound_provider_is_refused_as_a_default(tmp_path):
    cm, _ = _cm(tmp_path)
    with pytest.raises(ConnectionRefused):
        cm.connect("salesforce", api_key="x")      # dinosaur — never one-click


def test_oauth_native_reports_authorize_not_a_key(tmp_path):
    cm, _ = _cm(tmp_path)
    st = cm.connect("gmail")                        # no token yet
    assert st.connected is False and st.needs == "authorize"
    assert "authorize" in st.detail.lower()


def test_env_backed_read_only_store_refuses_connect(tmp_path):
    from agentic_os.secrets import build_secret_store
    cm = ConnectionManager(store=build_secret_store("env"))
    with pytest.raises(ConnectionRefused):
        cm.connect("pdl", api_key=SECRET)           # env store is read-only


# ── catalog reflects the taxonomy ──────────────────────────────────────────────────────────────────────

def test_catalog_classifies_providers(tmp_path):
    cm, _ = _cm(tmp_path)
    cat = {c["provider"]: c for c in cm.catalog()}
    assert cat["pdl"]["integration_class"] == IntegrationClass.AGENT_NATIVE and cat["pdl"]["defaultable"]
    assert cat["pdl"]["connect_hint"] == "paste-key"
    assert cat["twenty"]["bundled_oss"] is True
    assert cat["gmail"]["connect_hint"] == "authorize"
    assert cat["salesforce"]["defaultable"] is False and cat["salesforce"]["connect_hint"] == "not-offered"
    # every non-UI-bound provider is offered as a default
    assert all(c["defaultable"] for c in cm.catalog() if c["integration_class"] != IntegrationClass.UI_BOUND)


# ── PDL first-class enrichment connector ───────────────────────────────────────────────────────────────

def test_pdl_is_a_registered_enrichment_provider():
    from agentic_os.world import enrichment
    assert "pdl" in enrichment._PROVIDERS
    prov = enrichment._PROVIDERS["pdl"](key="k")
    assert prov.name == "pdl" and prov.configured() is True


def test_pdl_search_and_email_guard(monkeypatch):
    from agentic_os.world import enrichment
    calls = {}

    def fake_post(url, payload, headers, timeout=10.0):
        calls["url"] = url
        # one record with a real email, one with a free-tier bool flag
        return {"data": [
            {"id": "1", "full_name": "Ada L", "first_name": "Ada", "job_title": "VP Data",
             "work_email": "ada@acme.com"},
            {"id": "2", "full_name": "Boolean B", "first_name": "Boolean", "job_title": "Head of Data",
             "work_email": True},                       # free-tier presence flag → must be dropped to None
        ]}
    monkeypatch.setattr(enrichment, "_post", fake_post)
    rows = enrichment._PROVIDERS["pdl"](key="k").search_people(domain="acme.com", titles=["data"], limit=5)
    assert "peopledatalabs.com/v5/person/search" in calls["url"]
    assert rows[0]["email"] == "ada@acme.com"
    assert rows[1]["email"] is None                     # bool flag guarded, not leaked as "True"

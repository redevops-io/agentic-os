"""World Adapter layer — Canonical Business Objects projected into a real OSS core or the in-memory demo
store, one identity across apps, realism never faked (LIVE only when the core actually answered)."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime_contracts import AuthorityContext, PrincipalRef  # noqa: E402
from runtime_contracts.world import RealismClass  # noqa: E402
from agentic_os.world import (  # noqa: E402
    ALL_WORLDS,
    AdapterRegistry,
    CanonicalObject,
    CoreUnavailable,
    InMemoryAdapter,
    LagoBillingAdapter,
    ScenarioOrchestrator,
)
import agentic_os.world.adapters as A  # noqa: E402

SEEDED = RealismClass.SEEDED_DEMO.value
LIVE = RealismClass.REAL_LIVE.value
_CORE_ENV = ("LAGO_API_URL", "LAGO_API_KEY", "TWENTY_BASE_URL", "TWENTY_API_KEY",
             "CHATWOOT_BASE_URL", "CHATWOOT_API_TOKEN")


def _obj(kind="customer", cid="cust-acme"):
    return CanonicalObject(canonical_id=cid, kind=kind, label="Acme Inc", attributes={"name": "Acme Inc"},
                           source_record_id="rec-1", provenance="ds", realism=LIVE)


def test_canonical_object_native_id_and_record():
    o = _obj()
    assert o.native_id("twenty") == "twenty-customer-cust-acme"
    rec = o.to_record("twenty", o.native_id("twenty"))
    assert rec["entity_id"] == "cust-acme" and rec["kind"] == "customer" and rec["label"] == "Acme Inc"


def test_in_memory_adapter_is_idempotent_and_labelled_seeded():
    a = InMemoryAdapter("twenty")
    nid = a.upsert(_obj())
    assert nid == "twenty-customer-cust-acme" and a.upsert(_obj()) == nid       # idempotent
    rec = a.get(nid)
    assert rec["entity_id"] == "cust-acme" and rec["projection_realism"] == SEEDED
    assert a.accepts("customer") and not a.accepts("subscription")             # per the app catalog


def test_registry_defaults_to_in_memory_when_no_core_configured(monkeypatch):
    for e in _CORE_ENV:
        monkeypatch.delenv(e, raising=False)
    a = AdapterRegistry().for_app("lago")
    assert a.name == "lago:in-memory" and a.realism == SEEDED


def test_registry_uses_a_real_core_when_available_and_labels_it_live():
    class FakeLive:
        name, realism = "lago:Lago", LIVE
        def accepts(self, k): return True
        def available(self): return True
        def upsert(self, o): return "lago-live-" + o.canonical_id
        def get(self, n): return {"native_id": n}
    reg = AdapterRegistry(overrides={"lago": FakeLive()})
    a = reg.for_app("lago")
    assert a.realism == LIVE and a.upsert(_obj()) == "lago-live-cust-acme"
    assert reg.resolved()["lago"]["realism"] == LIVE                           # honestly labelled LIVE


def test_http_core_adapter_is_unavailable_without_env(monkeypatch):
    for e in _CORE_ENV:
        monkeypatch.delenv(e, raising=False)
    assert LagoBillingAdapter().available() is False                          # no config -> falls back


def test_real_adapter_raises_core_unavailable_on_write_failure(monkeypatch):
    monkeypatch.setenv("LAGO_API_URL", "http://lago"); monkeypatch.setenv("LAGO_API_KEY", "k")
    monkeypatch.setattr(A, "_http_json", lambda *a, **k: (_ for _ in ()).throw(OSError("refused")))
    with pytest.raises(CoreUnavailable):
        LagoBillingAdapter().upsert(_obj())                                    # so the seeder can degrade


def test_allow_real_false_forces_in_memory(monkeypatch):
    monkeypatch.setenv("LAGO_API_URL", "http://lago"); monkeypatch.setenv("LAGO_API_KEY", "k")
    assert AdapterRegistry(allow_real=False).for_app("lago").name == "lago:in-memory"


def test_seeder_projects_one_identity_into_many_apps_and_records_realism(monkeypatch):
    for e in _CORE_ENV:
        monkeypatch.delenv(e, raising=False)
    auth = AuthorityContext(authority_id="c", principal=PrincipalRef(id="f", tenant="rd"), purpose="s", scope=(
        "read:crm", "read:geo", "write:quote", "write:crm"))
    orch = ScenarioOrchestrator()
    orch.run(ALL_WORLDS["after-hours-lead"], seed="s", authority=auth,
             answers={"What is the roof pitch?": "6/12"})
    projs = orch._seeder.projections
    apps = {p["app"] for p in projs}
    assert {"twenty", "erpnext"} <= apps                                       # same customer in CRM + books
    assert all(p["realism"] == SEEDED for p in projs)                         # offline -> in-memory, labelled
    # the same canonical id landed in both apps (one identity, many schemas)
    by_app = {p["app"]: p["canonical_id"] for p in projs if p["kind"] == "customer"}
    assert by_app.get("twenty") == by_app.get("erpnext")
    assert orch._seeder.adapters_in_use()["twenty"]["adapter"] == "twenty:in-memory"

"""Synthetic-demo isolation for the two surfaces that answer anonymously (audit items 2).

Both surfaces are meant to be open — a blanket authentication wall would break the demo and was ruled out. The
problem the audit found is not openness, it is *unbounded* openness: the intelligence router in demo mode uses
`body.tenant` as sent, so an anonymous caller can address any tenant that exists; and the unified server's
`/api/sidekick` silently tolerates `tenant` / `policy_refs` in the body. Isolation is what is added here, not
credentials: an open surface can be confined to tenants that were declared up front, and a demo surface refuses
fields it does not own instead of ignoring them quietly until some later handler starts reading them.
"""
from __future__ import annotations

import os
import tempfile

# unified_server builds its runtime at import, so its store has to exist first (same trick the existing
# unified-server tests use).
os.environ.setdefault("UNIFIED_STORE", os.path.join(tempfile.mkdtemp(), "iso_events.jsonl"))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from agentic_os.integrations.business.contracts import Provenance  # noqa: E402
from agentic_os.integrations.business.supply import InventoryPosition, PurchaseOrder  # noqa: E402
from agentic_os.intelligence import IntelligenceService, build_router  # noqa: E402
from agentic_os.intelligence.auth import StaticBearerAuthenticator  # noqa: E402
from agentic_os.intelligence.families import SupplyGraph, supply_registry, supply_synthesize  # noqa: E402
from agentic_os.mission import unified_server  # noqa: E402

RESOLVE = "/v1/intelligence/supply/shortage_risk"
DEMO_TENANTS = ("demo-acme", "demo-beta")
FORGED = "acme-real-customer-production"


class _Counting(IntelligenceService):
    calls = 0

    def resolve(self, need, **kw):
        type(self).calls += 1
        return super().resolve(need, **kw)


def _service():
    def pr(r):
        return Provenance(provider="erp", provider_ref=r)

    graph = SupplyGraph(
        inventory=[InventoryPosition(prov=pr("i"), part="Rim", site="A", on_hand=20.0)],
        open_supply=[PurchaseOrder(prov=pr("po1"), supplier_ref="s", site="A", part="Rim", quantity=50.0,
                                   promised_date="2026-02-15", ordered_at="2026-01-01T00:00:00Z")],
        demand=[])
    _Counting.calls = 0
    return _Counting().bind(supply_registry(graph), supply_synthesize)


def _client(**kw):
    app = FastAPI()
    app.include_router(build_router(_service(), **kw))
    return TestClient(app)


# ── intelligence: the open demo, unchanged, and the isolated one ───────────────────────────────────────

def test_the_plain_demo_build_still_honours_the_bodied_tenant():
    """Compatibility pinned: this is the posture the audit flagged, and it is not changed by this PR."""
    c = _client()
    assert c.post(RESOLVE, json={"subject_refs": ["Rim", "A"], "tenant": FORGED}).status_code == 200


def test_an_isolated_demo_answers_for_a_declared_synthetic_tenant():
    c = _client(synthetic_demo_tenants=DEMO_TENANTS)
    assert c.post(RESOLVE, json={"subject_refs": ["Rim", "A"], "tenant": "demo-acme"}).status_code == 200


def test_an_isolated_demo_cannot_be_pointed_at_a_real_tenant_by_naming_it():
    """The whole point of the isolation: no credential is asked for, and a real customer's tenant name is
    still unreachable. The refusal happens before the service is touched."""
    c = _client(synthetic_demo_tenants=DEMO_TENANTS)
    r = c.post(RESOLVE, json={"subject_refs": ["Rim", "A"], "tenant": FORGED})
    assert r.status_code == 403, r.text
    assert _Counting.calls == 0, "a refused tenant must not reach the intelligence service"


def test_an_isolated_demo_defaults_to_its_own_tenant_when_none_is_claimed():
    c = _client(synthetic_demo_tenants=DEMO_TENANTS)
    assert c.post(RESOLVE, json={"subject_refs": ["Rim", "A"]}).status_code == 200
    assert _Counting.calls == 1


def test_isolation_does_not_change_the_authenticated_posture():
    """With an authenticator the principal's tenant is still authoritative, and a body naming another tenant
    is refused exactly as before."""
    c = _client(authenticator=StaticBearerAuthenticator({"tok-a": ("svc-a", "tenant-a")}),
                synthetic_demo_tenants=DEMO_TENANTS)
    ok = {"Authorization": "Bearer tok-a"}
    assert c.post(RESOLVE, json={"subject_refs": ["Rim", "A"], "tenant": "tenant-a"}, headers=ok).status_code == 200
    assert c.post(RESOLVE, json={"subject_refs": ["Rim", "A"], "tenant": FORGED}, headers=ok).status_code == 403


# ── sidekick: still anonymous, no longer silently tolerant ────────────────────────────────────────────

def test_the_sidekick_demo_journey_still_works_anonymously():
    r = TestClient(unified_server.app).post("/api/sidekick", json={"text": "sync the pilot account into support"})
    assert r.status_code == 200, r.text
    assert r.json().get("mission_id"), "the demo journey creates the governed mission"


@pytest.mark.parametrize("claim", [{"tenant": FORGED}, {"policy_refs": ["anything"]},
                                   {"tenant": FORGED, "policy_refs": ["x"]}])
def test_sidekick_refuses_fields_it_does_not_own(claim):
    """The mission's goal, account and grants belong to the demo runtime. These keys used to be ignored;
    ignoring is what a future handler would silently upgrade into honouring."""
    body = {"text": "sync the pilot account into support", **claim}
    r = TestClient(unified_server.app).post("/api/sidekick", json=body)
    assert r.status_code == 400, r.text
    assert "tenant" in r.text or "grants" in r.text


def test_sidekick_still_answers_a_non_sync_intent_as_before():
    r = TestClient(unified_server.app).post("/api/sidekick", json={"text": "what is the weather"})
    assert r.status_code == 200, r.text

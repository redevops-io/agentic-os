"""The closed outcome loop, reachable through the Projects API. The CRM and Outreach producers feed the
'what needs me?' surface via select_action, and recording outcomes (POST /api/outcomes) changes which
action the surface selects next time — the loop, operable end-to-end (in simulation)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from agentic_os.projects_api import SampleProjectionProvider, create_app


def _crm_action(surface):
    """The CRM Next-Best-Action producer's chosen action in the surface (source_app 'crm'), if any."""
    for bucket in ("surfaced", "deferred"):
        for it in surface.get(bucket, []):
            if it["source_app"] == "crm" and it["candidate_id"].startswith("crm:Acme:"):
                return it["action_kind"]
    return None


def test_crm_and_outreach_producers_feed_the_surface():
    c = TestClient(create_app(SampleProjectionProvider()))
    s = c.get("/api/projects/customer-ops/priorities").json()
    apps = {i["source_app"] for i in s["surfaced"]} | {i["source_app"] for i in s["deferred"]}
    assert "crm" in apps and "outreach" in apps            # both producers ran through select_action
    assert s["learning"]["outcomes_recorded"] == 0 and s["learning"]["selection_adjusted"] is False


def test_recording_outcomes_changes_the_selected_action():
    # a fresh provider selects CRM's prior best (send_proposal); after outcomes show it losing while
    # schedule_call converts, the SAME surface selects schedule_call — the loop closed via the API.
    prov = SampleProjectionProvider()
    c = TestClient(create_app(prov))
    before = _crm_action(c.get("/api/projects/customer-ops/priorities").json())
    assert before == "send_proposal"

    for _ in range(30):
        r = c.post("/api/outcomes", json={"source_app": "crm", "action_kind": "send_proposal",
                                          "observed_reward": -0.6, "attribution_confidence": 0.9}).json()
        assert r["supported"] and r["recorded"] >= 1
        c.post("/api/outcomes", json={"source_app": "crm", "action_kind": "schedule_call",
                                      "observed_reward": 0.9, "attribution_confidence": 0.9})

    after_surface = c.get("/api/projects/customer-ops/priorities").json()
    assert after_surface["learning"]["selection_adjusted"] is True
    assert _crm_action(after_surface) == "schedule_call"    # selection changed from observed outcomes


def test_selection_stays_governed_after_learning():
    # whatever it learns, the chosen CRM action is outbound ⇒ still requires approval
    prov = SampleProjectionProvider()
    c = TestClient(create_app(prov))
    for _ in range(20):
        c.post("/api/outcomes", json={"source_app": "crm", "action_kind": "schedule_call",
                                      "observed_reward": 0.9})
    s = c.get("/api/projects/customer-ops/priorities").json()
    crm_items = [it for it in s["surfaced"] + s["deferred"]
                 if it["source_app"] == "crm" and it["candidate_id"].startswith("crm:Acme:")]
    # governance invariant: the chosen outbound action still requires approval (whether surfaced now or
    # deferred beyond the attention budget) — learning never turns it into an auto-execute.
    assert crm_items and all(it["requires_approval"] and it["action"] in ("request_approval", "defer")
                             for it in crm_items)


def test_outcomes_endpoint_reports_unsupported_when_provider_lacks_it():
    class _Bare(SampleProjectionProvider):
        record_outcome_event = None            # simulate a provider without the recorder
    c = TestClient(create_app(_Bare()))
    r = c.post("/api/outcomes", json={"source_app": "crm", "action_kind": "x"}).json()
    assert r["supported"] is False and r["recorded"] == 0

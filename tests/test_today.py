"""Today — AttentionItem projection + the governed approve/verify path (agentic_os.today).

No network. The adapter is the in-repo fake, so a 'send' is simulated; what is under test is the contract:
exact-payload binding, no duplicate send, no false 'done', consent, identity ambiguity, resurfacing.
"""
import pytest
from fastapi.testclient import TestClient

from agentic_os.projects_api import SampleProjectionProvider, create_app
from agentic_os.today import Today, TodayError, merge_items, project_attention, project_priorities, stable_id

DAY = 86400.0


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def today(clock):
    return Today(clock=clock)


def _id(today, key):
    return stable_id(today.tenant, today.project, "case:" + key)


def _digest(today, key, **kw):
    return today.item(_id(today, key), **kw)["action"]["payload_digest"]


# ── identity + projection ────────────────────────────────────────────────────────────────────────
def test_ids_are_stable_across_instances_and_tenant_scoped(clock):
    a, b = Today(clock=clock), Today(clock=clock)
    assert [i["id"] for i in a.feed()["items"]] == [i["id"] for i in b.feed()["items"]]
    other = Today(tenant="acme", clock=clock)
    assert not ({i["id"] for i in a.feed()["items"]} & {i["id"] for i in other.feed()["items"]})


def test_priorities_and_attention_dedupe_to_one_item_with_union_refs():
    prio = {"surfaced": [{"subject": "Sarah Chen — duplicate charge", "proposed_action": "Approve the $129 refund",
                          "rationale": "critical", "priority": 0.71, "confidence": 0.95,
                          "candidate_id": "crm:refund:4821", "risk_tier": "CRITICAL", "requires_approval": True}],
            "deferred": []}
    att = [{"id": "a1", "kind": "approval", "title": "Approve refund · $129", "reason": "Sarah Chen · duplicate charge",
            "priority": 90, "source_ref": "mission:4821"}]
    items = project_priorities(prio, tenant="t", project="p", now=1.0) + project_attention(att, tenant="t", project="p", now=1.0)
    merged = merge_items(items)
    assert len(merged) == 1
    assert set(merged[0]["source_refs"]) == {"crm:refund:4821", "mission:4821"}
    assert merged[0]["priority"]["score"] == 0.9                      # the higher of the two wins


def test_deferred_priorities_are_scheduled_not_lost():
    prio = {"surfaced": [], "deferred": [{"subject": "X", "proposed_action": "do", "rationale": "budget",
                                          "priority": 0.5, "candidate_id": "x"}]}
    (it,) = project_priorities(prio, tenant="t", project="p", now=1.0)
    assert it["state"] == "scheduled" and it["next_action"]["executable"] is False


def test_feed_counts_and_headline(today):
    f = today.feed()
    assert f["counts"]["needs_action"] == 5 and f["basis"] == "sample" and f["simulated_actions"] is True
    assert f["headline"] == "5 things need your decision."


# ── travel: the flagship journey ─────────────────────────────────────────────────────────────────
def test_travel_journey_suggested_approved_sent_verified_then_waits_then_outcome(today, clock):
    iid = _id(today, "travel-taylor-visa")
    d = today.item(iid)
    assert d["action"]["nothing_sent_yet"] and d["status_line"] == "Drafted — nothing has been sent."
    assert d["lifecycle"] == {"suggested": True, "approved": False, "sent": False,
                              "delivery_verified": False, "outcome_observed": False}
    out = today.approve(iid, expected_digest=d["action"]["payload_digest"])
    assert out["state"] == "waiting"                                          # a send does NOT close the work
    assert out["lifecycle"] == {"suggested": True, "approved": True, "sent": True,
                                "delivery_verified": True, "outcome_observed": False}
    assert out["receipt"]["status"] == "SUCCEEDED" and out["last_receipt_ref"] and out["approval_ref"]
    assert "waiting for the customer" in out["status_line"]
    done = today.observe_outcome(iid)
    assert done["state"] == "verified" and done["lifecycle"]["outcome_observed"] and done["outcome_ref"]


def test_approve_is_idempotent_no_duplicate_send(today):
    iid = _id(today, "travel-taylor-visa")
    dg = _digest(today, "travel-taylor-visa")
    first = today.approve(iid, expected_digest=dg)
    again = today.approve(iid, expected_digest=dg)
    assert first["replayed"] is False and again["replayed"] is True
    assert today._cases[iid].sends == 1                                       # exactly one provider submission
    with pytest.raises(TodayError) as e:
        today.approve(iid, expected_digest="sha256:" + "0" * 64)
    assert e.value.status == 409 and e.value.code == "stale_approval"


def test_wrong_or_stale_digest_is_refused_and_sends_nothing(today):
    iid = _id(today, "quote-acme-followup")
    with pytest.raises(TodayError) as e:
        today.approve(iid, expected_digest="sha256:" + "f" * 64)
    assert e.value.code == "stale_approval" and today._cases[iid].sends == 0


def test_mutated_content_invalidates_the_reviewed_digest(today):
    iid = _id(today, "quote-acme-followup")
    dg = _digest(today, "quote-acme-followup")
    today._cases[iid].body += " (P.S. 50% off!)"                               # content changed after review
    with pytest.raises(TodayError) as e:
        today.approve(iid, expected_digest=dg)
    assert e.value.code == "stale_approval" and today._cases[iid].sends == 0


# ── waiting resurfaces; snooze; dismiss ──────────────────────────────────────────────────────────
def test_waiting_item_resurfaces_after_follow_up_window(today, clock):
    iid = _id(today, "travel-taylor-visa")
    today.approve(iid, expected_digest=_digest(today, "travel-taylor-visa"))
    assert today.item(iid)["state"] == "waiting"
    clock.t += 3 * DAY + 1
    v = today.item(iid)
    assert v["state"] == "needs_action" and v["resurfaced"] is True            # never silently lost


def test_snooze_hides_then_returns(today, clock):
    iid = _id(today, "quote-acme-followup")
    assert today.snooze(iid, seconds=3600)["state"] == "scheduled"
    clock.t += 3601
    assert today.item(iid)["state"] == "needs_action"
    with pytest.raises(TodayError):
        today.snooze(iid, seconds=5)


def test_sent_item_cannot_be_dismissed_and_outcome_needs_a_send(today):
    iid = _id(today, "quote-acme-followup")
    with pytest.raises(TodayError) as e:
        today.observe_outcome(iid)
    assert e.value.code == "nothing_sent"
    today.approve(iid, expected_digest=_digest(today, "quote-acme-followup"))
    with pytest.raises(TodayError):
        today.dismiss(iid)


# ── clinic: consent + partial approval ───────────────────────────────────────────────────────────
def test_clinic_skips_patients_without_consent_even_if_requested(today):
    iid = _id(today, "clinic-unconfirmed")
    d = today.item(iid, targets=["p1", "p2", "p3"])
    assert [t["id"] for t in d["action"]["to"]] == ["p1", "p2"]
    assert d["action"]["skipped"][0]["reason"] == "No reminder consent on file"
    out = today.approve(iid, expected_digest=d["action"]["payload_digest"], targets=["p1", "p2", "p3"])
    assert out["state"] == "waiting"


def test_partial_approval_binds_the_exact_subset(today):
    iid = _id(today, "clinic-unconfirmed")
    full = _digest(today, "clinic-unconfirmed")
    subset = _digest(today, "clinic-unconfirmed", targets=["p1"])
    assert full != subset
    with pytest.raises(TodayError) as e:                                      # approving p1 with the full-set digest
        today.approve(iid, expected_digest=full, targets=["p1"])
    assert e.value.code == "stale_approval"
    with pytest.raises(TodayError) as e2:
        today.item(iid, targets=["nobody"])
    assert e2.value.code == "unknown_target"


# ── failure: no false done, no duplicate on retry ────────────────────────────────────────────────
def test_connector_failure_is_not_done_and_retry_needs_a_fresh_digest(today):
    iid = _id(today, "invoice-1042-reminder")
    dg = _digest(today, "invoice-1042-reminder")
    out = today.approve(iid, expected_digest=dg)
    assert out["state"] == "failed" and "connection needs attention" in out["status_line"]
    assert out["lifecycle"]["sent"] is False and out["lifecycle"]["approved"] is False
    assert out["last_receipt_ref"] and out["receipt"]["status"] == "FAILED"
    with pytest.raises(TodayError) as e:                                      # the old approval can't be replayed
        today.approve(iid, expected_digest=dg)
    assert e.value.code == "stale_approval"
    today.repair_connection(iid)
    again = today.approve(iid, expected_digest=_digest(today, "invoice-1042-reminder"))
    assert again["state"] == "waiting" and again["lifecycle"]["delivery_verified"] is True


# ── identity ambiguity ───────────────────────────────────────────────────────────────────────────
def test_ambiguous_identity_blocks_sending_until_disambiguated(today):
    iid = _id(today, "invoice-1050-ambiguous")
    d = today.item(iid)
    assert d["action"]["can_approve"] is False and d["action"]["payload_digest"] is None
    assert len(d["action"]["disambiguate"]) == 2 and "Choose which contact" in d["action"]["blocked_reason"]
    with pytest.raises(TodayError) as e:
        today.approve(iid, expected_digest="sha256:x")
    assert e.value.code == "identity_ambiguous"
    chosen = today.item(iid, identity_id="sam-a")
    assert chosen["action"]["to"][0]["address"] == "sam@harbor.example"
    out = today.approve(iid, expected_digest=chosen["action"]["payload_digest"], identity_id="sam-a")
    assert out["state"] == "waiting"
    with pytest.raises(TodayError) as e2:
        today.item(iid, identity_id="sam-z")
    assert e2.value.code == "unknown_identity"


# ── HTTP surface ─────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def http():
    return TestClient(create_app(SampleProjectionProvider()))


def _case_id(http, title_part):
    items = http.get("/api/projects/customer-ops/today").json()["items"]
    return next(i["id"] for i in items if title_part in i["user_title"])


def test_http_today_inspect_approve_verify(http):
    f = http.get("/api/projects/customer-ops/today").json()
    assert f["items"] and {"id", "state", "lifecycle", "priority", "next_action"} <= set(f["items"][0])
    iid = _case_id(http, "Visa documents")
    d = http.get(f"/api/projects/customer-ops/today/{iid}").json()
    r = http.post(f"/api/projects/customer-ops/today/{iid}/approve", json={"payload_digest": d["action"]["payload_digest"]})
    assert r.status_code == 200 and r.json()["state"] == "waiting" and r.json()["lifecycle"]["delivery_verified"]
    r2 = http.post(f"/api/projects/customer-ops/today/{iid}/approve", json={"payload_digest": d["action"]["payload_digest"]})
    assert r2.json()["replayed"] is True
    bad = http.post(f"/api/projects/customer-ops/today/{_case_id(http, 'Quote')}/approve", json={"payload_digest": "sha256:0"})
    assert bad.status_code == 409 and bad.json()["detail"]["error"] == "stale_approval"
    assert http.get("/api/projects/customer-ops/today/nope").status_code == 404


def test_http_sessions_are_isolated(http):
    iid = _case_id(http, "Visa documents")
    dg = http.get(f"/api/projects/customer-ops/today/{iid}", headers={"X-Demo-Session": "a"}).json()["action"]["payload_digest"]
    http.post(f"/api/projects/customer-ops/today/{iid}/approve", json={"payload_digest": dg}, headers={"X-Demo-Session": "a"})
    other = http.get(f"/api/projects/customer-ops/today/{iid}", headers={"X-Demo-Session": "b"}).json()
    assert other["state"] == "needs_action" and other["action"]["nothing_sent_yet"]


def test_http_signal_items_have_no_executable_action(http):
    items = http.get("/api/projects/customer-ops/today").json()["items"]
    sig = next(i for i in items if i["kind"] == "signal")
    d = http.get(f"/api/projects/customer-ops/today/{sig['id']}").json()
    assert d["action"] is None and d["next_action"]["executable"] is False


def test_http_unsupported_provider_degrades_honestly():
    class Bare(SampleProjectionProvider):
        today = None
        today_item = None
    c = TestClient(create_app(Bare()))
    assert c.get("/api/projects/x/today").json()["basis"] == "unavailable"
    assert c.get("/api/projects/x/today/abc").status_code == 501


def test_http_simulated_outcome_closes_only_after_send(http):
    iid = _case_id(http, "Quote")
    assert http.post(f"/api/projects/customer-ops/today/{iid}/simulate-outcome").status_code == 409
    dg = http.get(f"/api/projects/customer-ops/today/{iid}").json()["action"]["payload_digest"]
    http.post(f"/api/projects/customer-ops/today/{iid}/approve", json={"payload_digest": dg})
    assert http.post(f"/api/projects/customer-ops/today/{iid}/simulate-outcome").json()["state"] == "verified"


# ── fail-closed for a real (non-sample) deployment ───────────────────────────────────────────────
def test_non_sample_provider_serves_no_sample_cases_and_no_write_actions():
    from agentic_os.projects_api import RegisteredProducersProvider
    c = TestClient(create_app(RegisteredProducersProvider()))
    f = c.get("/api/projects/customer-ops/today").json()
    assert f["simulated_actions"] is False        # signals may be labelled sample, but nothing here is executable
    assert all(i["kind"] == "signal" and i["next_action"]["executable"] is False for i in f["items"])
    assert not any("Visa documents" in i["user_title"] for i in f["items"])
    iid = f["items"][0]["id"] if f["items"] else "att_x"
    for path, body in (("approve", {"payload_digest": "sha256:x"}), ("snooze", {"seconds": 3600}),
                       ("dismiss", {}), ("simulate-outcome", {})):
        r = c.post(f"/api/projects/customer-ops/today/{iid}/{path}", json=body)
        assert r.status_code == 501 and r.json()["detail"]["error"] == "unsupported", path


def test_sample_provider_is_unchanged_by_the_failclosed_mode(http):
    assert http.get("/api/projects/customer-ops/today").json()["simulated_actions"] is True


# ── contract: every action answers with the SAME full detail the inspect call gives ──────────────────────────────
# (the UI renders an action's response directly; a bare view crashed the whole page after the first approval)
DETAIL_KEYS = {"id", "state", "lifecycle", "user_title", "why", "evidence", "action", "history", "receipt", "next_action"}


def test_every_action_returns_the_full_detail_shape(today):
    for key in ("travel-taylor-visa",):
        iid = _id(today, key)
        d = today.item(iid)
        out = today.approve(iid, expected_digest=d["action"]["payload_digest"])
        assert DETAIL_KEYS <= set(out), set(DETAIL_KEYS) - set(out)
        assert out["action"]["nothing_sent_yet"] is False and out["history"] and out["evidence"]
        again = today.approve(iid, expected_digest=d["action"]["payload_digest"])
        assert DETAIL_KEYS <= set(again) and again["replayed"] is True
        assert DETAIL_KEYS <= set(today.observe_outcome(iid))
    q = _id(today, "quote-acme-followup")
    assert DETAIL_KEYS <= set(today.snooze(q, seconds=3600))
    assert DETAIL_KEYS <= set(today.dismiss(q))
    inv = _id(today, "invoice-1042-reminder")
    failed = today.approve(inv, expected_digest=today.item(inv)["action"]["payload_digest"])
    assert DETAIL_KEYS <= set(failed) and failed["state"] == "failed"


def test_http_action_responses_are_full_details(http):
    base = "/api/projects/customer-ops"
    items = {i["user_title"]: i["id"] for i in http.get(f"{base}/today").json()["items"] if i["kind"] == "case"}
    visa = items["Visa documents missing — Taylor trip"]
    dg = http.get(f"{base}/today/{visa}").json()["action"]["payload_digest"]
    r = http.post(f"{base}/today/{visa}/approve", json={"payload_digest": dg}).json()
    assert DETAIL_KEYS <= set(r)
    quote = items["Quote hasn't been answered — Acme"]
    assert DETAIL_KEYS <= set(http.post(f"{base}/today/{quote}/snooze", json={"seconds": 3600}).json())
    assert DETAIL_KEYS <= set(http.post(f"{base}/today/{quote}/dismiss").json())
    assert DETAIL_KEYS <= set(http.post(f"{base}/today/{visa}/simulate-outcome").json())

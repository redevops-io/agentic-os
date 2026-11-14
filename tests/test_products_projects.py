"""Projects + products end to end over real HTTP: real rows from the product's own API, explicit unavailable rows, the
single-use digest-bound decision flow attributed to a linked product user, and the Today approve control driving both
the decision and (a separate click) the execution."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentic_os.app_kit.route_authz import unclassified, write_routes
from agentic_os.projects_api import SampleProjectionProvider, create_app
from tests._products_fakes import ACT, CASE, FakeSentinel, write_registry

PID = "customer-ops"


@pytest.fixture
def stack(monkeypatch, tmp_path):
    for k in ("AGENTIC_OS_PROFILE", "AGENTIC_OS_REQUIRE_AUTHORITY", "AGENTIC_OS_CONTROL_TOKEN", "AGENTIC_OS_TENANT_TOKENS",
              "AGENTIC_OS_APPROVER_TOKENS", "PROJECTS_APPS", "AGENTIC_OS_SERVICE_TOKENS", "AGENTIC_OS_APPROVAL_SECRET"):
        monkeypatch.delenv(k, raising=False)
    fake = FakeSentinel()
    base = fake.serve()
    monkeypatch.setenv("AGENTIC_OS_PRODUCTS", write_registry(tmp_path, base))
    client = TestClient(create_app(SampleProjectionProvider()))
    yield fake, client, base, tmp_path, monkeypatch
    fake.close()


def test_without_a_registry_nothing_changes(monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_PRODUCTS", raising=False)
    c = TestClient(create_app(SampleProjectionProvider()))
    assert c.get("/api/products").status_code == 404
    assert any(m["id"] == "4821" for m in c.get(f"/api/projects/{PID}/missions").json())      # the sample demo is unchanged


def test_apps_row_comes_from_the_real_probe_with_capabilities_and_why_unavailable(stack):
    fake, c, base, *_ = stack
    row = next(a for a in c.get(f"/api/projects/{PID}/apps").json() if a["provider"] == "edge-sentinel")
    assert (row["state"], row["health"]) == ("MISSION_READY", "ok") and row["setup_url"].startswith(base)
    assert "security.sentinel_posture" in row["capabilities"] and row["credential_fields"] == []
    assert any("no route yet" in s for s in row["manual_steps"])                       # the product's own unavailable[] is shown
    prods = c.get("/api/products").json()
    p = prods["products"][0]
    assert p["health"]["state"] == "ok" and p["version"] == "0.6.3" and p["gate"] == {"open": True}
    from tests._products_fakes import DECISION_TOKEN, SECRET, SERVICE_TOKEN
    blob = str(prods)
    assert not any(x in blob for x in (SERVICE_TOKEN, DECISION_TOKEN, SECRET, "service_token_file", "/secrets/"))


def test_missions_attention_and_activity_show_the_products_own_records_and_drop_the_sample_rows(stack):
    fake, c, *_ = stack
    ms = c.get(f"/api/projects/{PID}/missions").json()
    assert [m["id"] for m in ms] == [f"edge-sentinel:{ACT}"]                  # no 'Refund Sarah Chen'
    assert ms[0]["state"] == "needs" and f"edge-sentinel:{CASE}" in ms[0]["source_refs"] and ms[0]["source_runtime"] == "security"
    att = c.get(f"/api/projects/{PID}/attention").json()
    assert len(att) == 1 and att[0]["kind"] == "approval" and "203.0.113.50" in att[0]["title"]
    assert "Dry run" in att[0]["consequence"] and att[0]["source_refs"][0] == f"edge-sentinel:{ACT}"
    act = c.get(f"/api/projects/{PID}/activity").json()
    assert any("action proposed" in a["text"] for a in act)


def test_mission_detail_has_the_real_lifecycle_and_the_products_evidence(stack):
    fake, c, *_ = stack
    d = c.get(f"/api/projects/{PID}/missions/edge-sentinel:{ACT}").json()
    assert [s["capability"] for s in d["steps"]][:3] == ["sentinel.observe", "sentinel.propose", "approval.request"]
    assert d["steps"][2]["status"] == "waiting" and d["steps"][3]["status"] == "todo"
    assert d["context_used"][0]["identity"]["fingerprint"] == "ab" * 32 and d["context_used"][0]["provider"] == "edge-sentinel"
    assert c.get(f"/api/projects/{PID}/missions/4821").json() == {}                # sample ids are gone with the sample rows


def test_keep_sample_rows_is_an_explicit_operator_choice(stack, tmp_path):
    fake, c, base, tp, mp = stack
    p = tp / "products.yaml"
    p.write_text(p.read_text().replace("schema: aos.products/v1", "schema: aos.products/v1\nkeep_sample_rows: true"))
    c2 = TestClient(create_app(SampleProjectionProvider()))
    ids = [m["id"] for m in c2.get(f"/api/projects/{PID}/missions").json()]
    assert "4821" in ids and f"edge-sentinel:{ACT}" in ids


def test_a_product_that_is_down_is_an_explicit_unavailable_row_never_a_fake_mission(stack):
    fake, c, *_ = stack
    fake.ready = False
    c.app.state.products_hub.invalidate("edge-sentinel")
    ms = c.get(f"/api/projects/{PID}/missions").json()
    assert [m["id"] for m in ms] == ["edge-sentinel:unavailable"] and ms[0]["progress"] == "Unavailable"
    assert "readiness probe" in ms[0]["context_used"][0]
    att = c.get(f"/api/projects/{PID}/attention").json()
    assert att[0]["kind"] == "reconnect" and "Reconnect Edge Sentinel" == att[0]["title"]
    row = next(a for a in c.get(f"/api/projects/{PID}/apps").json() if a["provider"] == "edge-sentinel")
    assert (row["state"], row["health"]) == ("NOT_CONNECTED", "bad") and row["manual_steps"]
    assert c.get(f"/api/projects/{PID}/missions/edge-sentinel:unavailable").json()["summary"]["state"] == "failed"


def test_a_product_without_a_service_token_says_so(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTIC_OS_PROFILE", raising=False)
    fake = FakeSentinel()
    base = fake.serve()
    try:
        monkeypatch.setenv("AGENTIC_OS_PRODUCTS", write_registry(tmp_path, base, with_tokens=False))
        c = TestClient(create_app(SampleProjectionProvider()))
        row = next(a for a in c.get(f"/api/projects/{PID}/apps").json() if a["provider"] == "edge-sentinel")
        assert row["state"] == "NOT_CONNECTED" and "no service token" in " ".join(row["manual_steps"])
        assert not any(call["path"].startswith("/api/v1") for call in fake.calls)       # never called without a credential
    finally:
        fake.close()


# ── the decision flow ───────────────────────────────────────────────────────────────────────────
def _card(c):
    return c.get(f"/api/products/edge-sentinel/records/{ACT}").json()


def test_decide_then_execute_are_two_human_steps_attributed_to_the_linked_user_and_execute_once(stack):
    fake, c, *_ = stack
    card = _card(c)
    assert card["kind"] == "approval" and len(card["digest"]) == 64 and card["record"]["request_id"] == ACT
    r = c.post(f"/api/products/edge-sentinel/actions/{ACT}/decision", json={"approved": True, "digest": card["digest"]})
    assert r.status_code == 200, r.text
    a = fake.actions[ACT]
    assert a["state"] == "APPROVED" and a["decision"]["actor"] == "user:alex" and a["decision"]["via"] == "projects"
    assert fake.executions == 0                                                  # approve never executes
    r = c.post(f"/api/products/edge-sentinel/actions/{ACT}/execute", json={"digest": card["digest"]})
    assert r.status_code == 200 and r.json()["result"]["verification"]["state"] == "dry_run"
    assert fake.executions == 1 and fake.actions[ACT]["state"] == "APPROVED" and fake.actions[ACT]["verification"]["state"] == "dry_run"   # the product keeps a dry run executable
    again = c.post(f"/api/products/edge-sentinel/actions/{ACT}/execute", json={"digest": card["digest"]})
    assert again.status_code == 409 and fake.executions == 1                      # once
    redo = c.post(f"/api/products/edge-sentinel/actions/{ACT}/decision", json={"approved": True, "digest": card["digest"]})
    assert redo.status_code == 409
    ms = c.get(f"/api/projects/{PID}/missions").json()
    assert ms[0]["state"] == "completed" and ms[0]["progress"] == "Dry run"


def test_the_service_credential_is_never_the_approval_and_a_token_is_bound_to_the_exact_digest(stack):
    fake, c, *_ = stack
    card = _card(c)
    wrong = "0" * 64
    r = c.post(f"/api/products/edge-sentinel/actions/{ACT}/decision", json={"approved": True, "digest": wrong})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "stale_approval"
    assert fake.actions[ACT]["state"] == "AWAITING_APPROVAL"
    assert not any(x["path"].endswith("/decision") for x in fake.calls)             # refused before any call to the product
    # a token minted for one digest does not verify for another (product-side check on the same functions)
    from agentic_os.mission.approval import mint_approval_token, verify_approval_token
    from tests._products_fakes import SECRET
    tok = mint_approval_token(SECRET, operator="edge-sentinel", capability="sentinel.decide", mission_id=None, node_id=None,
                              inputs={"case_id": CASE, "request_id": ACT, "approved": True, "digest": card["digest"]},
                              marker={"by": "projects:x"}, ttl_s=60)
    assert verify_approval_token(SECRET, tok, operator="edge-sentinel", capability="sentinel.decide",
                                 inputs={"case_id": CASE, "request_id": ACT, "approved": True, "digest": wrong}) is None


def test_an_unlinked_projects_user_cannot_decide_and_the_reason_is_shown(stack):
    fake, c, *_ = stack
    fake.linked = {}
    card = _card(c)
    r = c.post(f"/api/products/edge-sentinel/actions/{ACT}/decision", json={"approved": True, "digest": card["digest"]})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "actor_not_linked"
    assert fake.actions[ACT]["state"] == "AWAITING_APPROVAL"


def test_no_principal_no_decision_and_a_body_cannot_name_who_approved(stack, monkeypatch, tmp_path):
    fake, c, base, tp, mp = stack
    p = tp / "products.yaml"
    p.write_text(p.read_text().replace("principal: {single_owner: local-owner}\n", ""))
    c2 = TestClient(create_app(SampleProjectionProvider()))
    card = c2.get(f"/api/products/edge-sentinel/records/{ACT}").json()
    r = c2.post(f"/api/products/edge-sentinel/actions/{ACT}/decision", json={"approved": True, "digest": card["digest"]})
    assert r.status_code == 401 and r.json()["detail"]["error"] == "principal_required"
    r = c2.post(f"/api/products/edge-sentinel/actions/{ACT}/decision",
                json={"approved": True, "digest": card["digest"], "actor": "projects:local-owner"})
    assert r.status_code == 422                                                  # unknown body keys are refused outright
    assert not any(x["path"].endswith("/decision") for x in fake.calls)


def test_a_bearer_control_credential_is_the_principal_on_a_hosted_deployment(stack, monkeypatch):
    fake, c, base, tp, mp = stack
    p = tp / "products.yaml"
    p.write_text(p.read_text().replace("principal: {single_owner: local-owner}\n", ""))
    mp.setenv("AGENTIC_OS_CONTROL_TOKEN", "admin-token")
    fake.linked = {"projects:admin": "root"}
    c2 = TestClient(create_app(SampleProjectionProvider()))
    card = c2.get(f"/api/products/edge-sentinel/records/{ACT}").json()
    r = c2.post(f"/api/products/edge-sentinel/actions/{ACT}/decision", json={"approved": True, "digest": card["digest"]},
                headers={"Authorization": "Bearer admin-token"})
    assert r.status_code == 200 and fake.actions[ACT]["decision"]["actor"] == "user:root"
    assert c2.post(f"/api/products/edge-sentinel/actions/{ACT}/execute", json={"digest": card["digest"]},
                   headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_every_write_route_the_products_add_is_classified_human_only(stack):
    fake, c, *_ = stack
    pol = c.app.state.route_policies
    assert unclassified(c.app, pol) == []
    prod = {k: v for k, v in pol.items() if "/api/products/" in k}
    assert len(prod) == 3 and all(v.access == "human_only" for v in prod.values())
    assert not set(prod) - set(write_routes(c.app))


def test_production_profile_refuses_an_unauthenticated_decision_before_the_handler(stack, monkeypatch):
    fake, c, base, tp, mp = stack
    import secrets
    mp.setenv("AGENTIC_OS_PROFILE", "production")
    mp.setenv("AGENTIC_OS_APPROVAL_SECRET", secrets.token_hex(24))
    mp.setenv("AGENTIC_OS_REPLAY_DB", str(tp / "replay.db"))
    mp.setenv("AGENTIC_OS_CONTROL_TOKEN", "admin-token")
    c2 = TestClient(create_app(SampleProjectionProvider()), raise_server_exceptions=False)
    card = {"digest": "1" * 64}
    r = c2.post(f"/api/products/edge-sentinel/actions/{ACT}/decision", json={"approved": True, "digest": card["digest"]})
    assert r.status_code in (401, 403) and not any(x["path"].endswith("/decision") for x in fake.calls)


# ── Today: the shipped UI's approve control ─────────────────────────────────────────────────────
def _today_item(c):
    feed = c.get(f"/api/projects/{PID}/today").json()
    its = [i for i in feed["items"] if i.get("kind") == "case" and i["source_refs"] and i["source_refs"][0] == f"edge-sentinel:{ACT}"]
    assert len(its) == 1, feed
    return feed, its[0]


def test_today_item_drives_decide_then_a_separate_execute_with_a_different_digest(stack):
    fake, c, *_ = stack
    feed, item = _today_item(c)
    assert feed["counts"]["needs_action"] >= 1 and item["state"] == "needs_action" and item["simulated"] is False
    d = c.get(f"/api/projects/{PID}/today/{item['id']}").json()
    assert d["action"]["can_approve"] and d["action"]["to"][0]["label"] == "203.0.113.50" and "Dry run" in d["action"]["body"]
    d1 = d["action"]["payload_digest"]
    r = c.post(f"/api/projects/{PID}/today/{item['id']}/approve", json={"payload_digest": d1})
    assert r.status_code == 200, r.text
    after = r.json()
    assert fake.actions[ACT]["state"] == "APPROVED" and fake.executions == 0
    assert after["lifecycle"]["approved"] and "click again to execute" in after["status_line"]
    d2 = after["action"]["payload_digest"]
    assert d2 != d1                                                                  # a double click cannot decide AND execute
    stale = c.post(f"/api/projects/{PID}/today/{item['id']}/approve", json={"payload_digest": d1})
    assert stale.status_code == 409 and fake.executions == 0
    done = c.post(f"/api/projects/{PID}/today/{item['id']}/approve", json={"payload_digest": d2})
    assert done.status_code == 200 and fake.executions == 1
    body = done.json()
    assert body["state"] == "verified" and "Dry run" in body["status_line"] and body["receipt"]["receipt_id"] == "rcpt-0123456789abcdef"
    assert fake.actions[ACT]["decision"]["via"] == "projects"
    twice = c.post(f"/api/projects/{PID}/today/{item['id']}/approve", json={"payload_digest": d2})
    assert twice.status_code == 409 and fake.executions == 1


def test_today_approve_needs_a_principal(stack):
    fake, c, base, tp, mp = stack
    feed, item = _today_item(c)
    d = c.get(f"/api/projects/{PID}/today/{item['id']}").json()
    p = tp / "products.yaml"
    p.write_text(p.read_text().replace("principal: {single_owner: local-owner}\n", ""))
    c2 = TestClient(create_app(SampleProjectionProvider()))
    feed2, item2 = _today_item(c2)
    r = c2.post(f"/api/projects/{PID}/today/{item2['id']}/approve", json={"payload_digest": d["action"]["payload_digest"]})
    assert r.status_code == 401 and r.json()["detail"]["error"] == "principal_required"
    assert fake.actions[ACT]["state"] == "AWAITING_APPROVAL"


def test_unverified_readback_is_shown_as_failed_and_never_as_done(stack):
    fake, c, *_ = stack
    fake.dry_run, fake.verify_ok = False, False
    fake.actions[ACT]["mode"] = {"dry_run": False}
    card = _card(c)
    c.post(f"/api/products/edge-sentinel/actions/{ACT}/decision", json={"approved": True, "digest": card["digest"]})
    c.post(f"/api/products/edge-sentinel/actions/{ACT}/execute", json={"digest": card["digest"]})
    m = c.get(f"/api/projects/{PID}/missions").json()[0]
    assert m["state"] == "failed" and "Read-back" in m["progress"]
    att = c.get(f"/api/projects/{PID}/attention").json()
    assert any("read-back failed" in a["title"] for a in att)


def test_the_operator_basic_credential_the_shipped_server_asks_for_is_a_principal(stack, monkeypatch):
    import base64
    fake, c, base, tp, mp = stack
    p = tp / "products.yaml"
    p.write_text(p.read_text().replace("principal: {single_owner: local-owner}\n", ""))
    mp.setenv("OPERATOR_BASIC_AUTH", "ops:pw-secret-1234")
    fake.linked = {"projects:operator": "alex"}
    c2 = TestClient(create_app(SampleProjectionProvider()))
    card = c2.get(f"/api/products/edge-sentinel/records/{ACT}").json()
    good = {"Authorization": "Basic " + base64.b64encode(b"ops:pw-secret-1234").decode()}
    bad = {"Authorization": "Basic " + base64.b64encode(b"ops:nope").decode()}
    assert c2.post(f"/api/products/edge-sentinel/actions/{ACT}/decision", json={"approved": True, "digest": card["digest"]}, headers=bad).status_code in (401, 403)
    r = c2.post(f"/api/products/edge-sentinel/actions/{ACT}/decision", json={"approved": True, "digest": card["digest"]}, headers=good)
    assert r.status_code == 200 and fake.actions[ACT]["decision"]["actor"] == "user:alex"

"""A live publish must be released by an authenticated approver; the actor comes from the credential."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agentic_os.content import Channel, ContentBrief, FakePublisher, NoOpPublisher  # noqa: E402
from agentic_os.content.app import create_content_app  # noqa: E402
from agentic_os.content.projection import ContentMissionRegistry  # noqa: E402
from agentic_os.projects.approver_auth import approving_actor, is_live_publisher  # noqa: E402

for_env = ("AGENTIC_OS_CONTROL_TOKEN", "AGENTIC_OS_TENANT_TOKENS", "AGENTIC_OS_APPROVER_TOKENS")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in for_env:
        monkeypatch.delenv(k, raising=False)


class _LivePublisher:
    """Stands in for a publisher that can reach a real account: counts how many times it posted."""
    def __init__(self):
        self.posts = 0

    def publish(self, draft):
        self.posts += 1
        draft.status = "published"
        draft.post_id, draft.post_url = "p1", "https://example.test/p1"
        return draft


def _brief():
    return ContentBrief(campaign_id="c1", subject="Acme", angle="a launch", key_message="We shipped.",
                        channels=(Channel.X,), cta="Read more")


def test_simulated_publishers_are_not_live():
    assert not is_live_publisher(None)
    assert not is_live_publisher(FakePublisher())
    assert not is_live_publisher(NoOpPublisher())
    assert is_live_publisher(_LivePublisher())


def test_simulated_publisher_needs_no_token_and_keeps_claimed_actor():
    assert approving_actor(None, live=False, claimed="alice") == "alice"
    assert approving_actor(None, live=False) == "owner"


def test_live_publisher_without_any_token_configured_is_refused_503():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        approving_actor(None, live=True)
    assert e.value.status_code == 503


def test_live_publisher_missing_or_wrong_token_is_401(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "a" * 40)
    for hdr in (None, "Bearer nope"):
        with pytest.raises(HTTPException) as e:
            approving_actor(hdr, live=True)
        assert e.value.status_code == 401


def test_live_actor_comes_from_the_credential_not_the_body(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "a" * 40)
    assert approving_actor("Bearer " + "a" * 40, live=True, claimed="ceo@evil") == "admin"


def test_requester_cannot_approve_when_approver_credentials_exist(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", "acme=" + "r" * 40)
    monkeypatch.setenv("AGENTIC_OS_APPROVER_TOKENS", "acme=" + "p" * 40)
    with pytest.raises(HTTPException) as e:
        approving_actor("Bearer " + "r" * 40, live=True)
    assert e.value.status_code == 403
    assert approving_actor("Bearer " + "p" * 40, live=True, tenant="acme") == "approver:acme"


def test_content_approve_route_with_live_registry_never_posts_unauthenticated(monkeypatch):
    pub = _LivePublisher()
    reg = ContentMissionRegistry(publisher_factory=lambda: pub)
    assert reg.live_publisher
    run = reg.open(_brief())
    client = TestClient(create_content_app(reg))
    url = f"/api/projects/content-studio/missions/{run.mission_id}/approve"
    r = client.post(url, json={"decision": "approve"})
    assert r.status_code == 503 and pub.posts == 0                      # no credential configured: refused
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "a" * 40)
    assert client.post(url, json={"decision": "approve"}).status_code == 401 and pub.posts == 0
    ok = client.post(url, json={"decision": "approve"}, headers={"Authorization": "Bearer " + "a" * 40})
    assert ok.status_code == 200


def test_content_approve_route_with_simulated_registry_still_works_without_a_token():
    reg = ContentMissionRegistry()
    assert not reg.live_publisher
    run = reg.open(_brief())
    client = TestClient(create_content_app(reg))
    r = client.post(f"/api/projects/content-studio/missions/{run.mission_id}/approve", json={"decision": "approve"})
    assert r.status_code == 200


def _projects_client(publisher):
    from agentic_os.projects import ProjectsContentService, ProjectsServer, create_projects_app
    from agentic_os.content import FakeGenerator
    brief = _brief()
    svc = ProjectsContentService(brief, owner="Alex", generator=FakeGenerator(), publisher=publisher)
    server = ProjectsServer()
    server.add(svc)
    return TestClient(create_projects_app(server)), svc


def test_projects_decide_with_live_publisher_posts_nothing_without_a_credential(monkeypatch):
    pub = _LivePublisher()
    client, svc = _projects_client(pub)
    assert svc.live_publisher
    ids = [a.artifact_id for a in svc.artifacts.values() if a.subtype in {c.value for c in Channel}]
    body = {"action": "approve", "selected_ids": ids, "actor": "ceo@evil"}
    url = f"/api/missions/{svc.mission_id}/decide"
    assert client.post(url, json=body).status_code == 503 and pub.posts == 0
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "a" * 40)
    assert client.post(url, json=body).status_code == 401 and pub.posts == 0
    ok = client.post(url, json=body, headers={"Authorization": "Bearer " + "a" * 40})
    assert ok.status_code == 200 and pub.posts >= 1
    assert svc.decision.actor == "admin"                      # recorded from the credential, not the body's claim


def test_projects_decide_with_simulated_publisher_still_works_without_a_token():
    client, svc = _projects_client(FakePublisher(publishable=(Channel.X,)))
    assert not svc.live_publisher
    ids = [a.artifact_id for a in svc.artifacts.values() if a.subtype in {c.value for c in Channel}]
    r = client.post(f"/api/missions/{svc.mission_id}/decide", json={"action": "approve", "selected_ids": ids})
    assert r.status_code == 200


# ── authentication is not authorisation: tenant, channel and grant scope ────────────────────────────────────
def _tenant_env(monkeypatch, grants):
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", "acme=" + "r" * 40 + ",globex=" + "g" * 40)
    monkeypatch.setenv("AGENTIC_OS_APPROVER_TOKENS", "acme=" + "p" * 40 + ",globex=" + "q" * 40)
    import json
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({"acme": {"policy_refs": grants}, "globex": {"policy_refs": ["content.publish"]}}))


def test_an_approver_for_another_tenant_cannot_publish_to_this_account(monkeypatch):
    from fastapi import HTTPException
    _tenant_env(monkeypatch, ["content.publish"])
    with pytest.raises(HTTPException) as e:
        approving_actor("Bearer " + "q" * 40, live=True, tenant="acme", channels=["x"])      # globex approver
    assert e.value.status_code == 403 and "not authorised for that account" in e.value.detail
    assert approving_actor("Bearer " + "p" * 40, live=True, tenant="acme", channels=["x"]) == "approver:acme"


def test_a_service_with_no_owning_tenant_refuses_every_tenant_scoped_credential(monkeypatch):
    from fastapi import HTTPException
    _tenant_env(monkeypatch, ["content.publish"])
    with pytest.raises(HTTPException) as e:
        approving_actor("Bearer " + "p" * 40, live=True, tenant="", channels=["x"])
    assert e.value.status_code == 403


def test_channel_grants_are_enforced_per_channel(monkeypatch):
    from fastapi import HTTPException
    _tenant_env(monkeypatch, ["content.publish.x"])                   # acme may publish to X only
    assert approving_actor("Bearer " + "p" * 40, live=True, tenant="acme", channels=["x"]) == "approver:acme"
    with pytest.raises(HTTPException) as e:
        approving_actor("Bearer " + "p" * 40, live=True, tenant="acme", channels=["x", "linkedin"])
    assert e.value.status_code == 403 and "linkedin" in e.value.detail


def test_a_tenant_with_no_publish_grant_cannot_publish_but_can_reject(monkeypatch):
    from fastapi import HTTPException
    _tenant_env(monkeypatch, [])
    with pytest.raises(HTTPException):
        approving_actor("Bearer " + "p" * 40, live=True, tenant="acme", channels=["x"])
    assert approving_actor("Bearer " + "p" * 40, live=True, tenant="acme", channels=["x"], publishing=False) == "approver:acme"


def test_only_the_unscoped_admin_token_is_global(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "a" * 40)
    assert approving_actor("Bearer " + "a" * 40, live=True, tenant="whoever", channels=["x", "tiktok"]) == "admin"


def test_the_routes_apply_the_scope(monkeypatch):
    _tenant_env(monkeypatch, ["content.publish.x"])
    pub = _LivePublisher()
    reg = ContentMissionRegistry(publisher_factory=lambda: pub, tenant="acme")
    run = reg.open(ContentBrief(campaign_id="c1", subject="Acme", angle="a", key_message="k",
                                channels=(Channel.X, Channel.LINKEDIN), cta="c"))
    client = TestClient(create_content_app(reg))
    url = f"/api/projects/content-studio/missions/{run.mission_id}/approve"
    assert client.post(url, json={"decision": "approve"}, headers={"Authorization": "Bearer " + "q" * 40}).status_code == 403
    assert client.post(url, json={"decision": "approve"}, headers={"Authorization": "Bearer " + "p" * 40}).status_code == 403   # no linkedin grant
    assert pub.posts == 0

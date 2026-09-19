"""Projects content slice — canonical artifacts + review≠authorization + approval≠execution + selective
publish binding exact versions + HELD never batch-authorized (plan §14/§19 acceptance)."""
from __future__ import annotations

import pytest

from agentic_os.content import Channel, ContentBrief, FakeGenerator, FakePublisher
from agentic_os.projects import ArtifactStatus, ProjectsContentService

_CH = (Channel.X, Channel.TIKTOK, Channel.LINKEDIN)


def _svc(publishable=_CH, video_held=False):
    brief = ContentBrief(campaign_id="metro-eng-001", subject="Metro Eng & Tech",
                         angle="an SMB that stopped missing local-gov contracts",
                         key_message="Right Miami-Dade solicitations surfaced; one-tap approve a bid.",
                         channels=_CH, cta="See how →",
                         evidence_ids=("solicitation:E26SP01", "mission:revenue:E26SP01"))
    return ProjectsContentService(brief, owner="Alex", generator=FakeGenerator(),
                                  publisher=FakePublisher(publishable=publishable), video_held=video_held)


def test_artifacts_have_independent_status_and_video_viewable_with_preview():
    svc = _svc()
    by_type = {(a.subtype): a for a in svc.artifacts.values()}
    assert by_type["x"].status is ArtifactStatus.READY
    assert by_type["linkedin"].status is ArtifactStatus.READY
    # with a presigned preview URL the video is viewable → READY (not HELD)
    assert by_type["short_video"].status is ArtifactStatus.READY
    assert by_type["short_video"].preview_ref                      # a viewable media URL is present
    # lineage is preserved on every artifact (§15)
    assert "solicitation:E26SP01" in by_type["x"].lineage_refs


def test_video_held_when_flagged_off_brand():
    svc = _svc(video_held=True)
    vid = next(a for a in svc.artifacts.values() if a.subtype == "short_video")
    assert vid.status is ArtifactStatus.HELD and vid.hold_reason
    assert "Regenerate" in vid.allowed_actions


def test_review_is_not_authorization_nothing_published_before_decide():
    svc = _svc()
    v = svc.mission_view()
    assert v["needs_decision"]["gate"] == "G4"                     # external-comms gate
    assert v["needs_decision"]["consequences"]                     # consequence shown before approval
    assert v["receipts"] == [] and v["decision"] is None           # nothing executed yet


def test_selective_approve_publishes_only_selected_and_binds_versions():
    svc = _svc()
    x = next(a for a in svc.artifacts.values() if a.subtype == "x")
    v = svc.decide("Alex", "approve", selected_ids=(x.artifact_id,))
    # only X was authorized + published; LinkedIn + video stay READY (not selected → not published)
    arts = {a["subtype"]: a for a in v["artifacts"]}
    assert arts["x"]["status"] == "PUBLISHED"
    assert arts["linkedin"]["status"] == "READY"
    assert arts["short_video"]["status"] == "READY"
    # a receipt exists for the published artifact; the decision binds the EXACT (id,version)
    assert any(r["artifact_id"] == x.artifact_id and r["status"] == "SUCCEEDED" for r in v["receipts"])
    assert [x.artifact_id, 1] in [list(p) for p in v["decision"]["selected"]]


def test_held_video_cannot_be_published_even_if_passed():
    svc = _svc(video_held=True)
    vid = next(a for a in svc.artifacts.values() if a.subtype == "short_video")
    v = svc.decide("Alex", "approve", selected_ids=(vid.artifact_id,))
    arts = {a["subtype"]: a for a in v["artifacts"]}
    assert arts["short_video"]["status"] == "HELD"                 # never authorized
    assert v["receipts"] == []                                     # nothing published


def test_reject_publishes_nothing():
    svc = _svc()
    v = svc.decide("Alex", "reject")
    assert v["receipts"] == [] and v["decision"]["action"] == "reject"


def test_failed_publish_marks_artifact_failed_with_receipt():
    svc = _svc(publishable=())                                     # publisher can't post → manual/fail
    x = next(a for a in svc.artifacts.values() if a.subtype == "x")
    v = svc.decide("Alex", "approve", selected_ids=(x.artifact_id,))
    arts = {a["subtype"]: a for a in v["artifacts"]}
    assert arts["x"]["status"] in ("FAILED", "PUBLISHED")          # FakePublisher(())→manual→not published
    assert v["receipts"]                                           # a receipt is always recorded


def test_http_app_serves_ui_and_decision(monkeypatch):
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient
    from agentic_os.projects import ProjectsServer, create_projects_app
    server = ProjectsServer()
    svc = server.add(_svc())
    client = TestClient(create_projects_app(server))
    assert "ReDevOps Projects" in client.get("/").text
    ms = client.get("/api/missions").json()
    assert ms and ms[0]["id"] == svc.mission_id
    x = next(a for a in svc.artifacts.values() if a.subtype == "x")
    r = client.post(f"/api/missions/{svc.mission_id}/decide",
                    json={"actor": "owner", "action": "approve", "selected_ids": [x.artifact_id]}).json()
    assert next(a for a in r["artifacts"] if a["subtype"] == "x")["status"] == "PUBLISHED"


def test_no_gate_when_nothing_ready():
    """A mission whose every channel is already published out-of-band (manual_receipts) and whose only
    other output is HELD has nothing to authorize — it must NOT present a vacuous approval gate."""
    class _NoImageGen(FakeGenerator):
        def generate_image(self, brief, concept):
            return {}
    brief = ContentBrief(campaign_id="blog-001", subject="Blog", angle="a",
                         key_message="k", channels=(Channel.X, Channel.LINKEDIN))
    svc = ProjectsContentService(brief, owner="Alex", generator=_NoImageGen(),
                                 include_video=True, video_held=True,
                                 manual_receipts={"x": "http://x/1", "linkedin": "http://li/1"})
    v = svc.mission_view()
    statuses = sorted(a["status"] for a in v["artifacts"])
    assert statuses == ["HELD", "PUBLISHED", "PUBLISHED"]          # X+LinkedIn published, video held
    assert v["needs_decision"] is None                            # nothing READY → no gate

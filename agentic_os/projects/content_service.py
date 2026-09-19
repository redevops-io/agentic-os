"""Content Mission as canonical Projects objects — the immediate engineering slice (plan §20).

Wraps a ContentMissionRun (which generates the concept + reel + per-channel drafts and parks at the gate)
and exposes its outputs as canonical Artifacts with INDEPENDENT status (X/LinkedIn READY, Video HELD), a
HumanRequest to authorize publication (gate G4), and — on a Decision over the EXACT selected artifact
versions — a governed selective publish that yields ActionReceipts and flips only the published artifacts
to PUBLISHED. Review ≠ authorization; approval ≠ execution; HELD/unselected artifacts are never implicitly
authorized (§14). The mission itself is completed for its governance/timeline via a no-op publisher so the
real, selective publish is owned here.
"""
from __future__ import annotations

from typing import Optional

from ..content import (
    Channel, ChannelDraft, ContentBrief, ContentMissionRun, FakeGenerator, FakePublisher, Generator,
    NoOpPublisher, Publisher, PublishOutcome,
)
from .contracts import (
    ActionReceipt, Artifact, ArtifactStatus, Decision, HumanGate, HumanRequest, HumanRequestType,
    LifecycleClass, _now,
)


class ProjectsContentService:
    """One content campaign as a durable Mission with canonical artifacts + a governed publish decision."""

    def __init__(self, brief: ContentBrief, *, owner: str = "",
                 generator: Optional[Generator] = None, publisher: Optional[Publisher] = None,
                 project_id: str = "content-studio", project_name: str = "Content Studio",
                 video_held: bool = False,
                 video_hold_reason: str = "narration off-brand — re-render with scenario detalization",
                 include_video: bool = True, manual_receipts: Optional[dict] = None):
        self.project_id = project_id
        self.project_name = project_name
        self.owner = owner
        self.brief = brief
        self._video_held = video_held
        self._video_hold_reason = video_hold_reason
        self._include_video = include_video
        # channels already posted out-of-band (e.g. a LinkedIn post published manually): {channel: post_url}
        self._manual_receipts = manual_receipts or {}
        self._publisher = publisher or FakePublisher(publishable=brief.channels)
        # generate + park at the gate; the mission's own publish gate uses a no-op (we publish selectively)
        self.run = ContentMissionRun(brief, owner=owner, generator=generator or FakeGenerator(),
                                     publisher=NoOpPublisher())
        self.mission_id = self.run.mission_id
        self.receipts: list[ActionReceipt] = []
        self.decision: Optional[Decision] = None
        self.artifacts: dict[str, Artifact] = {}
        self._build_artifacts()
        self.human_request = self._build_request()

    # ── artifacts from the generated campaign ──
    def _build_artifacts(self) -> None:
        pv = self.run.preview()
        lineage = tuple(self.brief.evidence_ids) + (f"content:{self.brief.campaign_id}",)
        by_channel = {d["channel"]: d for d in pv.get("drafts", [])}
        for ch, label in (("x", "X / Twitter post"), ("linkedin", "LinkedIn post")):
            if ch not in {c.value for c in self.brief.channels}:
                continue
            text = (by_channel.get(ch) or {}).get("text", "")
            prepub = ch in self._manual_receipts          # already posted out-of-band (e.g. manual LinkedIn)
            a = Artifact(
                mission_id=self.mission_id, app_id="content", type="SocialPost", subtype=ch,
                project_id=self.project_id, title=label, summary=text[:90], content=text,
                mime_type="text/plain",
                status=ArtifactStatus.PUBLISHED if prepub else ArtifactStatus.READY,
                lifecycle_class=LifecycleClass.REVIEWABLE,
                allowed_actions=() if prepub else ("Edit", "Approve"),
                content_ref=self._manual_receipts.get(ch, "") if prepub else "",
                evidence_refs=lineage, lineage_refs=lineage)
            self.artifacts[a.artifact_id] = a
            if prepub:
                self.receipts.append(ActionReceipt(
                    artifact_id=a.artifact_id, capability=f"social.publish.{ch}", provider=ch,
                    status="SUCCEEDED", external_url=self._manual_receipts.get(ch, ""),
                    error="posted manually (out-of-band)"))
        # optional generated hero image (viewable) — an Image artifact when one was produced
        if pv.get("image_url"):
            img = Artifact(
                mission_id=self.mission_id, app_id="content", type="Image", subtype="hero_image",
                project_id=self.project_id, title="Hero image", summary="generated visual",
                preview_ref=pv["image_url"], content_ref=pv["image_url"], mime_type="image/png",
                status=ArtifactStatus.READY, lifecycle_class=LifecycleClass.REVIEWABLE,
                allowed_actions=("Edit", "Approve", "Regenerate"), evidence_refs=lineage, lineage_refs=lineage)
            self.artifacts[img.artifact_id] = img
        # video: viewable when a presigned preview exists. Without a preview it MUST be HELD (can't publish
        # invisible media); an explicit video_held (e.g. off-brand narration) also holds it (§11). A campaign
        # with no video component (include_video=False, e.g. the text-only blog announcement) skips it.
        self._video_id = ""
        if self._include_video:
            preview = pv.get("reel_preview_url", "")
            held = self._video_held or not preview
            video = Artifact(
                mission_id=self.mission_id, app_id="content", type="Video", subtype="short_video",
                project_id=self.project_id, title="Short video (9:16)",
                summary="seedance · 3/3 rendered", content="", preview_ref=preview, mime_type="video/mp4",
                status=ArtifactStatus.HELD if held else ArtifactStatus.READY,
                lifecycle_class=LifecycleClass.REVIEWABLE,
                allowed_actions=("Edit brief", "Regenerate") if held else ("Edit", "Approve"),
                hold_reason=(self._video_hold_reason if self._video_held else
                             ("no viewable preview yet" if not preview else "")),
                evidence_refs=lineage, lineage_refs=lineage)
            self.artifacts[video.artifact_id] = video
            self._video_id = video.artifact_id

    def _build_request(self) -> HumanRequest:
        # the consequence text is shown BEFORE an irreversible action — it must describe THIS mission's
        # actual ready/held outputs, not a generic template (§11).
        ready = [a for a in self.artifacts.values() if a.status is ArtifactStatus.READY]
        ready_ids = [a.artifact_id for a in ready]
        ready_labels = ", ".join(a.subtype for a in ready) or "none"
        channels = ", ".join(dict.fromkeys(a.subtype for a in ready)) or "the selected channels"
        held = [a for a in self.artifacts.values() if a.status is ArtifactStatus.HELD]
        cons = f"Posts publicly to the connected {channels} account(s) — irreversible."
        if held:
            cons += " Held output(s) — " + ", ".join(a.subtype for a in held) + " — are NOT authorized."
        return HumanRequest(
            mission_id=self.mission_id, type=HumanRequestType.AUTHORIZE_EXTERNAL_ACTION,
            gate=HumanGate.G4_EXTERNAL_COMMS, artifact_ids=tuple(ready_ids),
            prompt=f"Approve & publish the ready outputs ({ready_labels}).",
            consequences=cons)

    # ── the governed decision (§4/§14): selective, binds exact versions ──
    def decide(self, actor: str, action: str, selected_ids: tuple[str, ...] = ()) -> dict:
        req = self.human_request
        if action == "reject":
            self.decision = Decision(req.request_id, actor or self.owner, "reject", ())
            for a in self.artifacts.values():
                if a.status is ArtifactStatus.READY:
                    a.status = ArtifactStatus.REJECTED
            req.status = "CLOSED"
            self.run.reject()
            return self.mission_view()

        # approve: only READY + explicitly-selected artifacts are ever authorized (never HELD/unselected)
        selected = [self.artifacts[i] for i in selected_ids if i in self.artifacts]
        publishable = [a for a in selected if a.status is ArtifactStatus.READY]
        self.decision = Decision(req.request_id, actor or self.owner, "approve",
                                 tuple((a.artifact_id, a.version) for a in publishable))
        for a in publishable:
            a.status = ArtifactStatus.EXECUTING
            draft = ChannelDraft(Channel(a.subtype), text=a.content, media_url=a.preview_ref or "")
            self._publisher.publish(draft)
            ok = draft.status == PublishOutcome.PUBLISHED.value
            a.status = ArtifactStatus.PUBLISHED if ok else ArtifactStatus.FAILED
            a.updated_at = _now()
            self.receipts.append(ActionReceipt(
                artifact_id=a.artifact_id, capability=f"social.publish.{a.subtype}", provider=a.subtype,
                status="SUCCEEDED" if ok else "FAILED", external_id=draft.post_id,
                external_url=draft.post_url, error=draft.error, decision_id=self.decision.decision_id))
        req.status = "CLOSED"
        self.run.approve()          # complete mission governance/timeline (no-op publisher → no double post)
        return self.mission_view()

    # ── the §9/§11 mission-detail projection ──
    def mission_view(self) -> dict:
        arts = [a.to_dict() for a in self.artifacts.values()]
        published = sum(1 for a in self.artifacts.values() if a.status is ArtifactStatus.PUBLISHED)
        ready = sum(1 for a in self.artifacts.values() if a.status is ArtifactStatus.READY)
        held = sum(1 for a in self.artifacts.values() if a.status is ArtifactStatus.HELD)
        state = ("completed" if self.decision and self.decision.action == "approve"
                 else "rejected" if self.decision else "needs")
        return {
            "project": {"id": self.project_id, "name": self.project_name},
            "mission": {"id": self.mission_id, "goal": self.brief.key_message,
                        "title": f"Create campaign — {self.brief.subject}", "state": state,
                        "owner": self.owner, "summary": f"{ready} READY · {held} HELD · {published} PUBLISHED"},
            "inputs": [{"ref": e, "kind": "reference"} for e in self.brief.evidence_ids],
            "artifacts": arts,
            "needs_decision": (self.human_request.to_dict() if self.human_request.status == "OPEN" else None),
            "receipts": [r.to_dict() for r in self.receipts],
            "decision": self.decision.to_dict() if self.decision else None,
            "timeline": self.run.rt.repo.timeline(self.mission_id),
        }

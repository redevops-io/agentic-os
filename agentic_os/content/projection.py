"""Project live Content Missions into the Projects UI's read contracts.

The Projects UI (`agentic_os/projects_api.py`) reads a `ProjectionProvider` — missions(), mission_detail()
(concept + previews as `context_used` artifacts, each with an optional `preview` URL), and attention()
(the pending approval, with Review/Approve/Reject). This provider projects a `ContentMissionRegistry`'s
live runs into exactly those shapes, so a content mission renders and is approvable in the UI. Approval
routes back through the registry (each run resolves its own mission's publish gate via `rt.approve`).
"""
from __future__ import annotations

from typing import Any, Optional

from ..mission.types import MissionState
from .contracts import ContentBrief
from .runner import ContentMissionRun


def _prov(runtime: str, *refs: str) -> dict:                 # mirrors projects_api._prov
    return {"source_runtime": runtime, "source_refs": list(refs)}


class ContentMissionRegistry:
    """Holds the live content missions for a project. `open(brief)` runs one to its publish gate; the
    provider projects them and approval flows back through here."""

    def __init__(self, *, owner: str = "", generator_factory=None, publisher_factory=None,
                 project_id: str = "content-studio", project_name: str = "Content Studio"):
        self.owner = owner
        self._genf = generator_factory
        self._pubf = publisher_factory
        self.project_id = project_id
        self.project_name = project_name
        self._runs: dict[str, ContentMissionRun] = {}

    def open(self, brief: ContentBrief) -> ContentMissionRun:
        run = ContentMissionRun(brief, owner=self.owner,
                                generator=self._genf() if self._genf else None,
                                publisher=self._pubf() if self._pubf else None)
        self._runs[run.mission_id] = run
        return run

    def runs(self) -> list[ContentMissionRun]:
        return list(self._runs.values())

    def run_for(self, mid: str) -> Optional[ContentMissionRun]:
        return self._runs.get(mid)

    def approve(self, mid: str):
        run = self._runs.get(mid)
        return run.approve() if run else None

    def reject(self, mid: str):
        run = self._runs.get(mid)
        return run.reject() if run else None


_UI_STATE = {MissionState.WAITING_HUMAN: "needs", MissionState.SUCCEEDED: "completed"}


class ContentProjectionProvider:
    """A `ProjectionProvider` (structural) over a ContentMissionRegistry."""

    def __init__(self, registry: ContentMissionRegistry):
        self.reg = registry

    def projects(self) -> list:
        return [{"id": self.reg.project_id, "name": self.reg.project_name, "health": "ok"}]

    def overview(self, _pid: str) -> dict:
        runs = self.reg.runs()
        return {"id": self.reg.project_id, "name": self.reg.project_name, "missions": len(runs),
                "awaiting_approval": sum(1 for r in runs if r.state is MissionState.WAITING_HUMAN)}

    def missions(self, _pid: str) -> list:
        out = []
        for run in self.reg.runs():
            pv = run.preview()
            st = _UI_STATE.get(run.state, "running")
            out.append({"id": run.mission_id, "title": f"Content — {run.brief.subject}",
                        "workflow": "Content Distribution", "state": st,
                        "progress": {"needs": "awaiting approval", "completed": "published"}.get(st, "…"),
                        "context_used": [f"{c} draft" for c in pv["channels"]],
                        **_prov("mission", f"mission:{run.mission_id}")})
        return out

    def mission_detail(self, pid: str, mid: str) -> dict:
        run = self.reg.run_for(mid)
        if not run:
            return {}
        pv = run.preview()
        concept = pv["concept"]
        done = run.state is MissionState.SUCCEEDED
        summary = next((m for m in self.missions(pid) if m["id"] == mid), None)
        steps = [
            {"n": 1, "capability": "content.generate_concept", "provider": "vibexgen", "tier": 0,
             "status": "done", "why": concept.get("hook", "")},
            {"n": 2, "capability": "content.render_reel", "provider": "vibexgen", "tier": 0,
             "status": "done", "why": "short-form vertical video rendered for preview"},
            {"n": 3, "capability": "content.draft_x", "provider": "vibexgen", "tier": 0,
             "status": "done", "why": "X post drafted"},
            {"n": 4, "capability": "content.draft_linkedin", "provider": "vibexgen", "tier": 0,
             "status": "done", "why": "LinkedIn post drafted"},
            {"n": 5, "capability": "content.publish", "provider": "social-publisher", "tier": 4,
             "status": "done" if done else "waiting",
             "why": "published to the approved channels" if done
                    else "PUBLIC POSTING — awaiting human approval before it goes live"},
            {"n": 6, "capability": "content.track", "provider": "social-publisher", "tier": 1,
             "status": "done" if done else "todo", "why": "track engagement after publishing"},
        ]
        context_used = [
            {"source_id": "concept", "source_name": "Content concept", "provider": "vibexgen",
             "kind": "artifact", "evidence_kind": "file", "retrieved": None, "identity": {"version": "v1"},
             "refs": [{"ref": "artifact:concept", "summary": concept.get("hook", "")}],
             "why": f"angle: {run.brief.angle}"},
        ]
        if pv.get("reel_preview_url"):
            context_used.append(
                {"source_id": "reel", "source_name": "Short-form video", "provider": "vibexgen",
                 "kind": "artifact", "evidence_kind": "file", "retrieved": None, "identity": {"version": "v1"},
                 "refs": [{"ref": "artifact:reel", "summary": concept.get("narrative", "")}],
                 "preview": pv["reel_preview_url"], "why": "rendered vertical video — preview before publish"})
        for d in pv.get("drafts", []):
            entry = {"source_id": f"draft-{d['channel']}", "source_name": f"{d['channel']} draft",
                     "provider": "vibexgen", "kind": "artifact", "evidence_kind": "file", "retrieved": None,
                     "identity": {"version": "v1"},
                     "refs": [{"ref": f"artifact:{d['channel']}", "summary": (d.get("text", "") or "")[:140]}],
                     "why": "per-channel copy for approval"}
            if d.get("media_url"):
                entry["preview"] = d["media_url"]
            context_used.append(entry)
        return {"summary": summary, "steps": steps, "context_used": context_used,
                "context_plan_note": "Content is generated for preview; nothing publishes until the owner "
                                     "approves the publish gate."}

    def attention(self, _pid: str) -> list:
        out = []
        for run in self.reg.runs():
            if run.state is MissionState.WAITING_HUMAN:
                pv = run.preview()
                out.append({"id": f"approve-{run.mission_id}", "kind": "approval",
                            "title": f"Approve & publish · {run.brief.subject}",
                            "reason": pv["concept"].get("hook", ""),
                            "consequence": f"Publishes to {', '.join(pv['channels'])} (tier 4)",
                            "available_actions": ["Review", "Approve", "Reject"], "priority": 88,
                            **_prov("governance", f"mission:{run.mission_id}")})
        return out

    # remaining ProjectionProvider methods — sensible empties for a content-only project
    def workflows(self, _pid: str) -> list:
        return [{"id": "content_distribution", "name": "Content Distribution",
                 "steps": ["Concept", "Render", "Draft", "Approve", "Publish", "Track"],
                 "cadence": "On demand", "state": "active", "runsNote": "approval before publish",
                 **_prov("mission", "workflow:content_distribution")}]

    def priorities(self, pid: str) -> dict:
        return {"items": self.attention(pid)}

    def discovery(self, _pid: str) -> list:
        return []

    def apps(self, _pid: str) -> list:
        return []

    def connect_app(self, provider: str) -> dict:
        return {"provider": provider, "state": "NOT_CONNECTED"}

    def sources(self, _pid: str) -> list:
        return []

    def runtime(self, _pid: str) -> dict:
        return {"missions": len(self.reg.runs())}

    def templates(self, _pid: str) -> list:
        return [{"id": "content_distribution", "name": "Content Distribution"}]

    def activity(self, _pid: str) -> list:
        return []

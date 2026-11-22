"""agentic-social-autopilot as a Mission Runtime operator (Phase-1 production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) onto the app so the
Mission Runtime can drive social-autopilot as a capability operator — the same core Postiz
actions the console exposes at `/agent/run`, now discoverable + idempotent on the wire.

Capabilities (syscalls):
  social.draft    — generate post copy + stage it as a DRAFT in Postiz (real postgres write)
  social.draft_visual — the same, with a Vibexgen `visual.export` result attached as the post's media
  social.publish  — publish a post to the social networks       [approval gate: publish]

Publishing is the one action that pushes content OUT to the public, so social.publish carries
approval_required=True (matches modules.yaml `approval_required:[publish]`); the runtime parks
it as a HumanTask before execution. Drafting only persists a private DRAFT to Postiz, so it is
side_effecting but not gated.

Heavy content-generation surfaces (the vibexgen trends/generate adapter and the host-side UI
"demo reel" recorder) are intentionally NOT capabilities: they depend on external GPU/browser
workers and are not pure Postiz-core actions. They stay in app.py as UI-only endpoints.
"""
from __future__ import annotations

import os

from agentic_os.mission.operator_sdk import Operator, capability

from . import core


def _revision_aware_default() -> bool:
    return bool((os.environ.get("AGENTIC_OS_REVISION_LEDGER") or "").strip())


# Revision-aware approvals (docs/REVISION_AWARE_APPROVALS.md). A staged post is an artifact ``social/post/<id>``: drafting and revising it
# PRODUCE revisions; publishing is bound to the HEAD revision a human approved and consumes that approval exactly once.
_POST_REVISION = {"artifact": "social/post/{post_id}", "digest": ["copy", "media"], "target": "{post_id}"}
_PUBLISH_BOUND = {"artifact": "social/post/{id}", "digest": "head", "target": "{id}"}


def build_social_operator(revision_aware: bool | None = None) -> Operator:
    """``revision_aware`` (default: on when ``AGENTIC_OS_REVISION_LEDGER`` is set) declares the revision metadata above. The runtime must then be given
    the same ledger; a revision-bound publish on a runtime without one fails closed rather than running unbound."""
    aware = _revision_aware_default() if revision_aware is None else bool(revision_aware)
    prod = {"revision_produces": dict(_POST_REVISION)} if aware else {}
    bound = {"revision_bound": dict(_PUBLISH_BOUND)} if aware else {}
    revise = [capability(
        "social.revise_draft",
        lambda inp: core.revise_draft(inp),
        provides=["draft_revised"],
        inputs={"id": "str", "copy": "str"},
        outputs={"draft_revised": "the draft's copy was changed; approvals given for the previous text no longer apply"},
        side_effecting=True,
        compensation={"supported": False, "disposition": "PRESERVE", "notes": "only a private DRAFT is edited; the previous text is not published anywhere"},
        permissions=["social:write"], estimated_value="medium", latency_ms=800,
        concurrency_mode="exclusive", concurrency_key="social:post:{id}",
        revision_produces={"artifact": "social/post/{post_id}", "digest": ["copy", "media"], "target": "{post_id}"},
    )] if aware else []
    return Operator("social-autopilot", [
        capability(
            "social.draft",
            lambda inp: core.draft(inp),
            provides=["draft_staged"],
            outputs={"draft_staged": "post copy generated and staged as a DRAFT in Postiz"},
            side_effecting=True,
            compensation={"supported": False, "disposition": "PRESERVE", "notes": "a drafted post is kept for human review; it is never published or auto-removed by a failed workflow"},
            permissions=["social:write"], estimated_value="medium", latency_ms=1200,
            concurrency_key="provider:postiz", max_parallelism=3,  # bounded Postiz writes; drafts are independent
            **prod,
        ),
        capability(
            "social.draft_visual",
            lambda inp: core.draft_visual(inp),
            provides=["visual_draft_staged"],
            inputs={"visual_export": "dict", "topic": "str"},
            outputs={"visual_draft_staged": "post copy and a validated, labelled visual staged as a DRAFT in Postiz"},
            side_effecting=True,
            compensation={"supported": False, "disposition": "PRESERVE", "notes": "a drafted post is kept for human review; it is never published or auto-removed by a failed workflow"},
            permissions=["social:write"], estimated_value="medium", latency_ms=1200,
            concurrency_key="provider:postiz", max_parallelism=3,
            **prod,
        ),
        capability(
            "social.publish",
            lambda inp: core.publish(inp),
            provides=["publish_staged"],
            outputs={"publish_staged": "post staged for human approval before it goes live"},
            side_effecting=True,
            compensation={"supported": False, "reversibility": "IRREVERSIBLE", "notes": "publishing externally is NOT reliably reversible"}, approval_required=True,
            permissions=["social:write"], estimated_value="high", latency_ms=800,
            concurrency_mode="exclusive", concurrency_key="social:post:{id}",  # never double-publish one post
            **bound,
        ),
        *revise,
    ])

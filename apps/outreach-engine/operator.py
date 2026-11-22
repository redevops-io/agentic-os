"""agentic-outreach-engine as a Mission Runtime operator (Phase-1 production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) onto the app so the
Mission Runtime can drive outreach as a capability operator — the same core Twenty CRM
actions the console exposes at `/agent/run`, now discoverable + idempotent on the wire.

Capabilities (syscalls):
  outreach.refresh      — rebuild the ranked pipeline from live GitHub/HN signals (no CRM write)
  outreach.enrich       — resolve a verified business email via the configured provider (Hunter by default)
  outreach.draft_pitch  — LLM-draft a personalized pitch (+ evidence visualization + the lead's link)
  outreach.approve      — human sign-off that syncs a real pilot to Twenty (Company+Person+Opportunity)  [approval gate]
  outreach.send_all     — dispatch the approved outreach sequences to prospects  [approval gate: send_sequence]
  outreach.check_replies — check the sender's own inbox for a prospect's reply (no CRM write, no send)
  outreach.follow_up    — draft the second touch (reply-aware thank-you, or a no-reply nudge); send_all sends it

Per control-plane modules.yaml the gate is `approval_required: [send_sequence]` — nothing reaches a
prospect without a human sign-off — so `outreach.send_all` carries approval_required=True and the
runtime parks it as a HumanTask before execution.
"""
from __future__ import annotations

import os

from agentic_os.mission.operator_sdk import Operator, capability, pending_approval

from . import core


def _revision_aware_default() -> bool:
    return bool((os.environ.get("AGENTIC_OS_REVISION_LEDGER") or "").strip())


# Revision-aware approvals (docs/REVISION_AWARE_APPROVALS.md): an account's pitch is an artifact ``outreach/account/<account>``. Drafting it PRODUCES a
# revision (subject + body); approving the account is bound to the HEAD revision the human saw, and re-drafting supersedes a pending approval.
_PITCH_REVISION = {"artifact": "outreach/account/{account}", "digest": ["subject", "body"], "target": "{account}"}
_APPROVE_BOUND = {"artifact": "outreach/account/{account}", "digest": "head", "target": "{account}"}


def _draft_pitch(inp):
    # deferred import: app.py needs the governed LLM, which core.py deliberately never touches, so the
    # draft_pitch logic lives there; app.py already defers ITS import of this module (the router mount
    # happens inside a function, near the bottom of the file), so neither module needs the other at
    # module-load time regardless of which one a caller imports first.
    from . import app as _app
    return _app.draft_pitch(inp)


def _follow_up(inp):
    from . import app as _app  # same reasoning as _draft_pitch above
    return _app.follow_up(inp)


def build_outreach_operator(revision_aware: bool | None = None) -> Operator:
    """``revision_aware`` (default: on when ``AGENTIC_OS_REVISION_LEDGER`` is set) declares the revision metadata above; the runtime must be given the
    same ledger, and a revision-bound approve on a runtime without one fails closed."""
    aware = _revision_aware_default() if revision_aware is None else bool(revision_aware)
    prod = {"revision_produces": dict(_PITCH_REVISION)} if aware else {}
    bound = {"revision_bound": dict(_APPROVE_BOUND)} if aware else {}
    return Operator("outreach-engine", [
        capability(
            "outreach.refresh",
            lambda inp: core.refresh(inp),
            provides=["outreach_pipeline"],
            outputs={"outreach_pipeline": "ranked accounts + drafted openers rebuilt from live signals"},
            estimated_value="low", deterministic=False, latency_ms=1200,
            concurrency_mode="read_only",   # rebuilds pipeline, no CRM write
        ),
        capability(
            "outreach.enrich",
            lambda inp: core.enrich(inp),
            provides=["contact_email"],
            outputs={"contact_email": "verified business email resolved via the configured enrichment "
                                       "provider (Hunter by default)"},
            estimated_value="medium", deterministic=False, latency_ms=900,
            concurrency_mode="read_only",   # resolves + attaches an email, no CRM write, no send
        ),
        capability(
            "outreach.draft_pitch",
            lambda inp: _draft_pitch(inp),
            provides=["pitch_drafted"],
            outputs={"pitch_drafted": "LLM-personalized pitch (evidence visualization + the lead's real "
                                       "link) attached to the account's draft"},
            estimated_value="medium", deterministic=False, latency_ms=1500,
            concurrency_mode="read_only",   # only writes the in-memory pipeline row, no CRM write, no send
            **prod,
        ),
        capability(
            "outreach.approve",
            lambda inp: (core.approve(inp) if inp.get("_approval")
                         else pending_approval("outreach.approve", account=inp.get("account", ""),
                                               lead_synced=False)),
            provides=["lead_synced"],
            outputs={"lead_synced": "approved lead synced to Twenty (Company + Person + Opportunity)"},
            side_effecting=True,
            compensation={"supported": False, "reversibility": "IRREVERSIBLE", "notes": "approval syncs the lead to Twenty (external CRM); not automatically reversible"}, approval_required=True,
            permissions=["outreach:write"], estimated_value="high", latency_ms=1500,
            concurrency_mode="exclusive", concurrency_key="outreach:account:{account}",
            **bound,
        ),
        capability(
            "outreach.send_all",
            # Consequential: reach prospects ONLY on a runtime approval. The open /invoke path stages
            # (pending_approval) instead of auto-sending — matching the gate the manifest declares.
            lambda inp: (core.send_all(inp) if inp.get("_approval")
                         else pending_approval("outreach.send_all",
                                               staged=sorted(core._STATE["approved"]), sequences_sent=False)),
            provides=["sequences_sent"],
            outputs={"sequences_sent": "approved outreach sequences dispatched to prospects"},
            side_effecting=True,
            compensation={"supported": False, "reversibility": "IRREVERSIBLE", "notes": "sent email cannot be recalled"}, approval_required=True,
            permissions=["outreach:write"], estimated_value="high", latency_ms=800,
            concurrency_mode="exclusive", resource_keys=["outreach:send"],  # one dispatch run at a time
        ),
        capability(
            "outreach.check_replies",
            lambda inp: core.check_replies(inp),
            provides=["reply_checked"],
            outputs={"reply_checked": "whether a reply from the prospect was found in the sender's inbox"},
            estimated_value="medium", deterministic=False, latency_ms=2000,
            concurrency_mode="read_only",   # reads the sender's own mailbox only, no CRM write, no send
        ),
        capability(
            "outreach.follow_up",
            lambda inp: _follow_up(inp),
            provides=["followup_drafted"],
            outputs={"followup_drafted": "the second-touch draft (reply-aware thank-you, or a no-reply "
                                          "nudge) attached to the account; send_all dispatches it"},
            estimated_value="medium", deterministic=False, latency_ms=1500,
            concurrency_mode="read_only",   # only writes the in-memory pipeline row, no CRM write, no send
        ),
    ])

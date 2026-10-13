"""agentic-crm as a Mission Runtime operator (Phase-1 production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) onto the app so
the Mission Runtime can drive CRM as a capability operator — the same core ERPNext-CRM
actions the console exposes at `/agent/run`, now discoverable + idempotent on the wire.

Capabilities (syscalls):
  crm.score_lead      — lead score 0-100 (method llm|rules), written back as a Lead Comment + lead_score
  crm.research        — enrichment brief (optionally web-enriched), saved as a Lead Comment
  crm.draft_outreach  — draft a first-touch email, saved as a Lead Comment for human review
  crm.qualify         — advance a Lead's status (pipeline progression)

Gate classification: NOTHING here sends to a prospect (`draft_outreach` only saves a Comment draft that a human
sends). `crm.qualify` CHANGES A LEAD'S STATUS (a mutation of the customer record beyond a draft), so it is
`approval_required=True` and the gate lives INSIDE `core.qualify` (stage `pending_approval` unless a
runtime-delivered `_approval`), which also covers the app's HTTP routes. `score_lead` (agent-owned `lead_score` field
+ Comment), `research` and `draft_outreach` (Comment only) are annotations/drafts and stay ungated. Every action
writes to ERPNext, so all carry `side_effecting=True` and the runtime dedupes them exactly-once on the
Idempotency-Key.
"""
from __future__ import annotations

from agentic_os.approval_gate import gated
from agentic_os.mission.operator_sdk import Operator, capability

from . import core


def build_crm_operator() -> Operator:
    LEAD = "crm:lead:{lead}"   # one writer per Lead: different leads parallelize, same lead serializes
    return Operator("agentic-crm", [
        capability(
            "crm.score_lead",
            lambda inp: core.score_lead(inp),
            provides=["lead_score"],
            outputs={"lead_score": "0-100 fit score + rationale written back to the Lead (Comment + lead_score)"},
            side_effecting=True,
            permissions=["crm:write"], estimated_value="high", latency_ms=1500,
            concurrency_mode="exclusive", concurrency_key=LEAD,
        ),
        capability(
            "crm.research",
            lambda inp: core.research(inp),
            provides=["lead_research"],
            outputs={"lead_research": "firmographic + buying-signal enrichment brief saved as a Lead Comment"},
            side_effecting=True,
            permissions=["crm:write"], estimated_value="medium", latency_ms=1800,
            concurrency_mode="exclusive", concurrency_key=LEAD,
        ),
        capability(
            "crm.draft_outreach",
            lambda inp: core.draft_outreach(inp),
            provides=["outreach_draft"],
            outputs={"outreach_draft": "personalised first-touch email drafted + saved for human review (never auto-sent)"},
            side_effecting=True,
            permissions=["crm:write"], estimated_value="high", latency_ms=1500,
            concurrency_mode="exclusive", concurrency_key=LEAD,
        ),
        capability(
            "crm.qualify",
            gated("crm.qualify", core.qualify, lead_qualified=False),
            provides=["lead_qualified"],
            outputs={"lead_qualified": "Lead status advanced (pipeline progression)"},
            side_effecting=True, approval_required=True,
            permissions=["crm:write"], estimated_value="medium", latency_ms=800,
            concurrency_mode="exclusive", concurrency_key=LEAD,
        ),
    ])

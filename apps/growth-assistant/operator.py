"""growth-assistant as a Mission Runtime operator (Phase-1 production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) onto the app so
the Mission Runtime can drive growth-assistant as a capability operator — the same core
actions the console exposes at `/agent/run`, now discoverable + idempotent on the wire.

Capabilities (syscalls):
  assistant.playbook             — 4-pillar growth playbook (LLM/template; asset store)
  assistant.subreddit_plan       — brand subreddit + first-threads plan
  assistant.founder_content      — founder-voice X/LinkedIn posts   (push -> Postiz drafts)
  assistant.community_blueprint  — lead-magnet community blueprint  (push -> Listmonk list)
  assistant.cold_outreach        — audit-Loom outreach kit          (push -> ERPNext Leads)
  assistant.hire_brief           — freelancer JD + scorecard + search links
  assistant.ask                  — read-only Q&A over saved assets + core connectivity

Per apps/control-plane/modules.yaml, growth-assistant declares `approval_required: []` — NO
capability is gated. Capabilities that can WRITE to a core (Postiz / Listmonk / ERPNext) when
`push` is set are marked side_effecting=True; the rest are read-only or asset-store-only.

The operator handlers call `core.*` with the default `gen=None`, so every capability is
deterministic (template) and exercisable without an LLM. The FastAPI app injects the real
LLM callback for its own `/agent/run` route.
"""
from __future__ import annotations

from agentic_os.mission.operator_sdk import Operator, capability

from . import core


def build_growth_assistant_operator() -> Operator:
    return Operator("growth-assistant", [
        capability(
            "assistant.playbook",
            lambda inp: core.playbook(inp),
            provides=["growth_playbook"],
            outputs={"growth_playbook": "4-pillar zero-to-traction playbook (saved asset)"},
            estimated_value="high", deterministic=False, latency_ms=1500,
        ),
        capability(
            "assistant.subreddit_plan",
            lambda inp: core.subreddit_plan(inp),
            provides=["subreddit_plan"],
            outputs={"subreddit_plan": "brand-subreddit incubation plan + seed threads"},
            estimated_value="medium", deterministic=False, latency_ms=1600,
        ),
        capability(
            "assistant.founder_content",
            lambda inp: core.founder_content(inp),
            provides=["founder_content"],
            outputs={"founder_content": "founder-voice posts; push=true drafts them to Postiz"},
            side_effecting=True, permissions=["postiz:write"],
            estimated_value="high", deterministic=False, latency_ms=1600,
        ),
        capability(
            "assistant.community_blueprint",
            lambda inp: core.community_blueprint(inp),
            provides=["community_blueprint"],
            outputs={"community_blueprint": "lead-magnet community blueprint; push=true creates a Listmonk list"},
            side_effecting=True, permissions=["listmonk:write"],
            estimated_value="medium", deterministic=False, latency_ms=1200,
        ),
        capability(
            "assistant.cold_outreach",
            lambda inp: core.cold_outreach(inp),
            provides=["cold_outreach_kit"],
            outputs={"cold_outreach_kit": "audit-Loom outreach kit; push=true creates ERPNext Leads"},
            side_effecting=True, permissions=["erpnext:write"],
            estimated_value="high", deterministic=False, latency_ms=1500,
        ),
        capability(
            "assistant.hire_brief",
            lambda inp: core.hire_brief(inp),
            provides=["hire_brief"],
            outputs={"hire_brief": "freelancer JD + vetting scorecard + freelancer search links"},
            estimated_value="medium", deterministic=False, latency_ms=1100,
        ),
        capability(
            "assistant.ask",
            lambda inp: core.ask(inp),
            provides=["growth_answer"],
            outputs={"growth_answer": "NL answer over the saved assets + core connectivity"},
            estimated_value="low", deterministic=False, latency_ms=500,
        ),
    ])

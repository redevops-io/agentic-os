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

Generation is free; PUSHING is gated. The generation capabilities only write a local asset file and NEVER call
a core; a legacy `push:true` returns the asset plus a staged push preview. `assistant.push_assets` (taking an
`asset_id`) is the only capability that writes to Postiz / Listmonk / ERPNext and is approval_required.
playbook / subreddit_plan / hire_brief write a local asset JSON file, so they are side_effecting too (not
read_only); `ask` only reads.

The operator handlers call `core.*` with the default `gen=None`, so every capability is
deterministic (template) and exercisable without an LLM. The FastAPI app injects the real
LLM callback for its own `/agent/run` route.
"""
from __future__ import annotations

from agentic_os.mission.operator_sdk import Operator, capability

from . import core


def _generate_only(fn, atype: str):
    """Generation capabilities NEVER push. A legacy ``push:true`` returns the generated asset plus a staged push
    preview telling the caller to invoke the approval-gated ``assistant.push_assets``."""
    def handler(inp: dict) -> dict:
        res = fn({**inp, "push": False})
        if inp.get("push") and res.get("asset_id"):
            res.update(core.staged_push(res["asset_id"], atype))
        return res
    return handler


def _push_assets(inp: dict) -> dict:
    # Execution is only reachable with a runtime-delivered `_approval`; defence in depth for direct invoke.
    if not inp.get("_approval"):
        # The approval gate comes FIRST: an unapproved caller always gets a staged response (never a
        # validation error), so the gate can't be bypassed or used to probe state. The preview is for the
        # approver; execution re-validates the asset and returns asset_not_found if it is gone.
        aid = str(inp.get("asset_id") or "")
        a = core._find_asset(aid) if aid else None
        return {"status": "pending_approval", "action": "push_assets", "executed": False,
                "push_executed": False, "asset_id": aid, "asset_found": bool(a),
                "push_preview": core.push_preview(a) if a else None}
    return core.push_asset(inp)


def build_growth_assistant_operator() -> Operator:
    return Operator("growth-assistant", [
        capability(
            "assistant.playbook",
            lambda inp: core.playbook(inp),
            provides=["growth_playbook"],
            outputs={"growth_playbook": "4-pillar zero-to-traction playbook (saved asset)"},
            estimated_value="high", deterministic=False, latency_ms=1500,
            side_effecting=True,  # writes a local asset JSON (not read_only)
        ),
        capability(
            "assistant.subreddit_plan",
            lambda inp: core.subreddit_plan(inp),
            provides=["subreddit_plan"],
            outputs={"subreddit_plan": "brand-subreddit incubation plan + seed threads"},
            estimated_value="medium", deterministic=False, latency_ms=1600,
            side_effecting=True,  # writes a local asset JSON (not read_only)
        ),
        capability(
            "assistant.founder_content",
            _generate_only(core.founder_content, "founder_content"),
            provides=["founder_content"],
            outputs={"founder_content": "founder-voice posts; push=true only stages (see assistant.push_assets)"},
            side_effecting=True,  # writes a local asset JSON; never pushes
            estimated_value="high", deterministic=False, latency_ms=1600,
        ),
        capability(
            "assistant.community_blueprint",
            _generate_only(core.community_blueprint, "community_blueprint"),
            provides=["community_blueprint"],
            outputs={"community_blueprint": "lead-magnet community blueprint; push=true only stages"},
            side_effecting=True,  # writes a local asset JSON; never pushes
            estimated_value="medium", deterministic=False, latency_ms=1200,
        ),
        capability(
            "assistant.cold_outreach",
            _generate_only(core.cold_outreach, "cold_outreach"),
            provides=["cold_outreach_kit"],
            outputs={"cold_outreach_kit": "audit-Loom outreach kit; push=true only stages"},
            side_effecting=True,  # writes a local asset JSON; never pushes
            estimated_value="high", deterministic=False, latency_ms=1500,
        ),
        capability(
            "assistant.push_assets",
            _push_assets,
            provides=["asset_push"],
            outputs={"asset_push": "pushes a saved founder_content/community_blueprint/cold_outreach asset to "
                                   "Postiz / Listmonk / ERPNext (approval-gated)"},
            side_effecting=True,
            compensation={"supported": False, "reversibility": "IRREVERSIBLE", "notes": "pushing assets to an external channel is NOT reliably reversible"}, approval_required=True,
            permissions=["postiz:write", "listmonk:write", "erpnext:write"],
            estimated_value="medium", deterministic=False, latency_ms=1500,
            concurrency_key="provider:push", max_parallelism=2,
        ),
        capability(
            "assistant.hire_brief",
            lambda inp: core.hire_brief(inp),
            provides=["hire_brief"],
            outputs={"hire_brief": "freelancer JD + vetting scorecard + freelancer search links"},
            estimated_value="medium", deterministic=False, latency_ms=1100,
            side_effecting=True,  # writes a local asset JSON (not read_only)
        ),
        capability(
            "assistant.ask",
            lambda inp: core.ask(inp),
            provides=["growth_answer"],
            outputs={"growth_answer": "NL answer over the saved assets + core connectivity"},
            estimated_value="low", deterministic=False, latency_ms=500,
            concurrency_mode="read_only",
        ),
    ])

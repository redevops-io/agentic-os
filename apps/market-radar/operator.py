"""agentic-market-radar as a Mission Runtime operator (Phase-1 production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) onto the app so
the Mission Runtime can drive market-radar as a capability operator — the same core
changedetection.io actions the console exposes at `/agent/run`, now discoverable +
idempotent on the wire.

Capabilities (syscalls):
  radar.add_watch  — start monitoring a new competitor/price page (creates a watch)
  radar.brief      — read-only competitive-intelligence summary of what changed (fact `market_brief`)
  radar.synthetic_study — read-only SYNTHETIC buyer study of message variants (off unless AGENTIC_OS_SYNTHETIC_RESEARCH=1);
                     its output is a hypothesis for human review, never evidence

Per modules.yaml there is NO approval gate for market-radar: add_watch is non-destructive
(it only starts observing a public page) and brief is read-only, so neither is parked as a
HumanTask. add_watch is side-effecting (it creates a watch); the runtime's idempotency-key
dedupe gives it exactly-once semantics on the wire.
"""
from __future__ import annotations

from agentic_os.mission.operator_sdk import Operator, capability

from . import core, loop, synthetic


def build_market_radar_operator() -> Operator:
    return Operator("market-radar", [
        capability(
            "radar.add_watch",
            lambda inp: core.add_watch(inp),
            provides=["watch"],
            outputs={"watch": "a new changedetection.io watch monitoring a competitor/price page"},
            side_effecting=True,
            compensation={"supported": False, "disposition": "PRESERVE", "notes": "a registered watch is kept; removing it is a separate human decision, not an automatic undo"},
            permissions=["market-radar:write"], estimated_value="medium", latency_ms=1200,
            # one watch per target URL: adding DIFFERENT watches runs concurrently, the same URL
            # serializes (pairs with the idempotency-key dedupe). Unresolved url → serialize conservatively.
            concurrency_mode="exclusive", concurrency_key="market-radar:watch:{url}",
        ),
        capability(
            "radar.brief",
            lambda inp: core.brief(inp),
            provides=["market_brief"],
            outputs={"market_brief": "read-only summary of what changed across the tracked watches"},
            estimated_value="high", deterministic=False, latency_ms=800,
            concurrency_mode="read_only",
        ),
        capability(
            "radar.synthetic_study",
            lambda inp: synthetic.synthetic_study(inp),
            provides=["synthetic_study_run"],
            inputs={"variants": "2-4 message variants ({id, headline, body, cta})", "audience": "sme | enterprise"},
            outputs={"synthetic_study_run": "a SYNTHETIC buyer study of the variants: hypotheses for human review, never evidence"},
            estimated_value="low", deterministic=False, latency_ms=120000,
            permissions=["market-radar:write"], concurrency_mode="read_only",   # no data is written; it spends model calls, so it needs the stricter permission
        ),
        # ── the loop on the company's own site (loop.py): audit, recommend, approve, verify, reassess. All read-only except the approval,
        #    which is a person's decision and writes nothing either. The site itself is changed elsewhere; verify refuses until it is.
        capability("radar.site_audit", lambda inp: loop.site_audit(inp), provides=["site_audit"],
                   inputs={"site": "the site to audit, https://..."},
                   outputs={"site_audit": "findings on discoverability and funnel instrumentation, each with a machine check"},
                   estimated_value="high", deterministic=False, latency_ms=20000, concurrency_mode="read_only"),
        capability("radar.recommend", lambda inp: loop.recommend(inp), provides=["recommendations"],
                   outputs={"recommendations": "a ranked list with evidence, checks and a digest"},
                   estimated_value="high", latency_ms=50, concurrency_mode="read_only"),
        capability("radar.approve_plan", lambda inp: loop.approve_plan(inp), provides=["change_plan_approved"],
                   outputs={"change_plan_approved": "the recommendations a person approved, bound to their digest"},
                   approval_required=True, estimated_value="high", latency_ms=50, permissions=["market-radar:write"]),
        capability("radar.verify_change", lambda inp: loop.verify_change(inp), provides=["change_verified"],
                   outputs={"change_verified": "every approved item's checks pass on the live site"},
                   estimated_value="high", deterministic=False, latency_ms=20000, concurrency_mode="read_only"),
        capability("radar.reassess", lambda inp: loop.reassess(inp), provides=["reassessment"],
                   outputs={"reassessment": "resolved, remaining and new findings after the change: the next round's input"},
                   estimated_value="medium", deterministic=False, latency_ms=20000, concurrency_mode="read_only"),
    ])

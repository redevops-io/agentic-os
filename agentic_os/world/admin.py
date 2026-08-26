"""Admin snapshot — the Business-OS governance/admin view over the world-runtime.

Where the Attention home answers "what needs me now", the admin view answers "is the whole system healthy and
honest": which OSS cores are actually wired LIVE vs the in-memory demo store, whether every governed mission's
invariants held (zero violations), what budgets are committed vs remaining, and where each mission sits on the
Observe → Recommend → Approve → Autonomous ladder. It is a read over the engine — it runs the worlds and
reports what actually happened, never a hand-maintained status page.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from runtime_contracts.world import RealismClass

from .adapters import AdapterRegistry
from .attention import _AUTOPLAY_ANSWERS, _WORLD_BLOCK
from .objects import APP_META
from .orchestrator import ScenarioOrchestrator

_LIVE = RealismClass.REAL_LIVE.value


def core_status(registry: Optional[AdapterRegistry] = None) -> List[Dict[str, Any]]:
    """Per OSS-core app: which World Adapter it resolved to and whether it is a real LIVE core or the in-memory
    demo store. Honest — an app reads LIVE only when its core answered a health probe."""
    reg = registry or AdapterRegistry()
    rows = []
    for app, (core, system) in APP_META.items():
        a = reg.for_app(app)
        rows.append({"app": app, "core": core, "business_system": system, "adapter": a.name,
                     "realism": a.realism, "status": "LIVE" if a.realism == _LIVE else "SEEDED"})
    return rows


def build_admin_snapshot(worlds: Dict[str, Any], *, authority: Any = None,
                         seeds: Optional[Dict[str, str]] = None, offline: bool = True) -> Dict[str, Any]:
    seeds = seeds or {}
    cores = core_status()
    governance: List[Dict[str, Any]] = []
    budgets: List[Dict[str, Any]] = []
    autonomy: List[Dict[str, Any]] = []
    worlds_summary: List[Dict[str, Any]] = []
    audit: List[Dict[str, Any]] = []

    for wid, world in worlds.items():
        orch = ScenarioOrchestrator()          # fresh per world -> clean per-world projections
        try:
            run = orch.run(world, seed=seeds.get(wid, "seed-0"), authority=authority,
                           answers=_AUTOPLAY_ANSWERS, offline=offline)
        except Exception:  # noqa: BLE001 — a world that errors is reported, never crashes the console
            governance.append({"world": wid, "invariants": 0, "violations": 0, "clean": False, "error": True})
            continue
        o = run.outcome or {}
        gov = o.get("governance")
        if isinstance(gov, dict):
            governance.append({"world": wid, "invariants": len(gov),
                               "violations": sum(1 for v in gov.values() if v), "clean": not any(gov.values())})
        if isinstance(o.get("governor"), dict):
            budgets.append({"world": wid, **o["governor"]})
        raises_approval = any(getattr(m, "needs_you", "") for m in getattr(run.trace, "milestones", []))
        autonomy.append({"world": wid, "business_system": _WORLD_BLOCK.get(wid, "runtime"),
                         "rung": "APPROVE" if raises_approval else "AUTONOMOUS"})
        d = world.descriptor()
        worlds_summary.append({"world": wid, "title": d.title, "realism": d.realism,
                               "business_system": _WORLD_BLOCK.get(wid, "runtime")})
        for p in orch._seeder.projections[:6]:
            audit.append({"world": wid, "app": p["app"], "entity": p["canonical_id"], "realism": p["realism"]})

    summary = {"cores_live": sum(1 for c in cores if c["status"] == "LIVE"), "cores_total": len(cores),
               "worlds": len(worlds_summary),
               "governance_clean": bool(governance) and all(g["clean"] for g in governance),
               "approval_gated": sum(1 for a in autonomy if a["rung"] == "APPROVE"),
               "budget_committed": round(sum(b.get("committed_total", 0) for b in budgets), 2),
               "budget_remaining": round(sum(b.get("remaining", 0) for b in budgets), 2)}
    return {"summary": summary, "cores": cores, "governance": governance, "budgets": budgets,
            "autonomy": autonomy, "worlds": worlds_summary, "audit": audit}

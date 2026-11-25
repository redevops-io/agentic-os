"""The dashboards service: the catalog (only dashboards whose applications are connected), one build per request from
the shared source cache, the drill-down that lists the records a number counted, and the actions a finding leads to.

Actions never execute anything by themselves. They hand the finding to the surfaces that already own the decision:
Sidekick (explain, plan a Mission), Today/Attention (track it), and the registered product's own human-only routes
(escalate a failed run, start a re-test, approve or reject an action).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import access as accessmod
from .access import Access
from .build import Build
from .builders import business, ops, qa
from .config import DashboardsConfig, config_from_env
from .contracts import NOT_CONNECTED, UNAUTHORIZED, Dashboard, Drill, Filters, Record
from .sources import SOURCE_META, Context, SourceCache
from .store import DAY, DashboardStore, open_store

MAX_DRILL = 200
FRESH_MIN_INTERVAL_S = 5.0


@dataclass(frozen=True)
class Definition:
    id: str
    title: str
    priority: str
    group: str
    needs_any: Tuple[str, ...]
    builder: Callable[[Build], None]
    about: str


DEFINITIONS: Tuple[Definition, ...] = (
    Definition("portfolio", "Portfolio health", "P0", "Overview", ("missions", "qa", "qa_ingest", "sentinel"), ops.portfolio,
               "Every project's release readiness, security findings, missions and what is waiting for a person."),
    Definition("qa", "QA and release readiness", "P0", "Quality", ("qa", "qa_ingest"), qa.build,
               "Test execution and requirement coverage, release readiness, platforms, regressions, flaky tests and evidence."),
    Definition("security", "Security and remediation", "P0", "Security", ("sentinel",), ops.security,
               "Open findings by severity, remediation actions and whether each was verified."),
    Definition("missions", "Missions and workflows", "P0", "Operations", ("missions",), ops.missions,
               "How missions end, what fails, and what is waiting for a person."),
    Definition("revenue", "Revenue and sales", "P1", "Business", ("twenty", "metabase"), business.revenue, "Pipeline, wins and stalled deals from the CRM."),
    Definition("finance", "Financial health", "P1", "Business", ("erpnext", "lago"), business.finance, "Receivables, payables, overdue invoices and billing."),
    Definition("market", "Market and conversion", "P1", "Business", ("umami",), business.market, "Traffic, top pages and the path from visitor to won deal."),
    Definition("support", "Customer support", "P1", "Business", ("chatwoot",), business.support, "Backlog, response time and conversations waiting on you."),
    Definition("procurement", "Suppliers and procurement", "P1", "Business", ("erpnext",), business.procurement, "Open and late purchase orders, spend by supplier."),
    Definition("marketing", "Marketing and social", "P1", "Business", ("postiz", "listmonk"), business.marketing, "Publishing cadence and email campaign results."),
    Definition("ai_costs", "AI usage and cost", "P1", "Operations", ("ai_usage",), ops.ai_costs, "Steps the Mission Runtime executed, their duration, and spend where it is recorded."),
    Definition("infrastructure", "Infrastructure and deployments", "P1", "Operations", ("runtime", "deployment"), ops.infrastructure,
               "Component health, product readiness and deployment findings."),
    Definition("knowledge", "Knowledge and data quality", "P2", "Operations", ("context_sources",), ops.knowledge,
               "Context sources, what was indexed or skipped, and what is stale."),
)
BY_ID = {d.id: d for d in DEFINITIONS}
#: every source a dashboard reads (not only the ones that decide whether it is offered), fetched together before it is built
WARM = {"portfolio": ("missions", "attention", "qa", "qa_ingest", "sentinel"), "missions": ("missions", "attention"), "market": ("umami", "twenty"),
        "revenue": ("twenty", "metabase"), "finance": ("erpnext", "lago", "metabase"), "marketing": ("postiz", "listmonk")}


class NotFound(LookupError):
    pass


class Forbidden(PermissionError):
    pass


def parse_filters(q: Mapping[str, Any], *, now: float) -> Filters:
    def s(k: str) -> str:
        v = q.get(k) or ""
        return v.strip()[:120] if isinstance(v, str) else ""

    def f(k: str) -> float:
        try:
            v = float(q.get(k) or 0)
        except (TypeError, ValueError):
            return 0.0
        return v if 0 < v <= now + DAY else 0.0
    try:
        days = int(q.get("days") or 30)
    except (TypeError, ValueError):
        days = 30
    since, until = f("since"), f("until")
    if since and until and since >= until:
        since = until = 0.0
    return Filters(project=s("project"), since=since, until=until, product=s("product"), environment=s("environment"), platform=s("platform"),
                   days=min(366, max(1, days)))


class DashboardService:
    def __init__(self, provider: Any, *, config: Optional[DashboardsConfig] = None, store: Optional[DashboardStore] = None,
                 clock: Callable[[], float] = time.time, clients: Optional[Dict[str, Any]] = None) -> None:
        self.provider = provider
        self.config = config if config is not None else config_from_env()
        self.store = store if store is not None else open_store(self.config.data_dir)
        self.clock = clock
        self.ctx = Context(provider=provider, config=self.config, store=self.store, default_project=self._default_project(), now=clock,
                           clients=dict(clients or {}))
        self.cache = SourceCache(self.ctx)
        self._last_fresh = -1e9

    # ── projects ──────────────────────────────────────────────────────────────────────────────────
    def _provider_projects(self) -> List[Dict[str, Any]]:
        try:
            return [dict(p) for p in (self.provider.projects() or [])]
        except Exception:  # noqa: BLE001
            return []

    def _default_project(self) -> str:
        """Records a source cannot attribute go to the provider's single project — unless the operator declared
        projects, in which case they stay unattributed (visible to an unscoped caller only)."""
        pp = self._provider_projects()
        if self.config.projects:
            return pp[0]["id"] if len(pp) == 1 and pp[0].get("id") in self.config.projects else ""
        return str(pp[0]["id"]) if pp else ""

    def projects(self) -> List[Dict[str, Any]]:
        if self.config.projects:
            return [{"id": p.id, "name": p.name, "product": p.product, "environment": p.environment} for p in self.config.projects.values()]
        return [{"id": str(p.get("id")), "name": str(p.get("name") or p.get("id")), "product": "", "environment": ""} for p in self._provider_projects()]

    def access(self, principal: Optional[str]) -> Access:
        return accessmod.resolve(self.config, self.provider, principal)

    # ── catalog ───────────────────────────────────────────────────────────────────────────────────
    def _source_state(self, sid: str, acc: Access) -> str:
        if not self.cache.enabled(sid):
            return NOT_CONNECTED
        if not acc.source(sid):
            return UNAUTHORIZED
        return self.cache.get(sid).status.state

    def catalog(self, acc: Access) -> Dict[str, Any]:
        shown, hidden = [], []
        self.cache.warm([sid for df in DEFINITIONS for sid in df.needs_any if acc.source(sid)])
        for df in DEFINITIONS:
            states = {sid: self._source_state(sid, acc) for sid in df.needs_any}
            if any(st not in (NOT_CONNECTED, UNAUTHORIZED) for st in states.values()):
                shown.append({"id": df.id, "title": df.title, "priority": df.priority, "group": df.group, "about": df.about,
                              "sources": [{"id": sid, "label": SOURCE_META[sid][0], "state": st} for sid, st in states.items() if st != NOT_CONNECTED]})
            elif any(st == NOT_CONNECTED for st in states.values()) and UNAUTHORIZED not in states.values():
                # Not offered: say why, name every application it could be built on, and how each is enabled. A title alone
                # ("3 more dashboards appear when their applications are connected") left a person with nothing to act on.
                needs = []
                for sid in df.needs_any:
                    st = self.cache.get(sid).status
                    needs.append({"id": sid, "label": SOURCE_META[sid][0], "state": st.state, "detail": st.detail, "remedy": st.remedy})
                one = len(needs) == 1
                hidden.append({"id": df.id, "title": df.title, "priority": df.priority, "about": df.about,
                               "connect": sorted({SOURCE_META[sid][0] for sid in df.needs_any}),
                               "why": ("The application this dashboard reads is not connected on this deployment." if one else
                                       "None of the applications this dashboard reads is connected on this deployment; connecting any one of them makes it appear."),
                               "needs": needs})
        projects = [p for p in self.projects() if acc.project(str(p["id"]))]
        return {"dashboards": shown, "not_connected": hidden, "projects": projects,
                "access": {"principal": acc.principal, "scoped": acc.scoped, "basis": acc.basis},
                "history": {"durable": self.store.durable}, "config_errors": len(self.config.errors), "generated_at": self.clock()}

    # ── build / drill ─────────────────────────────────────────────────────────────────────────────
    def build(self, dashboard_id: str, filters: Filters, acc: Access, *, fresh: bool = False) -> Dashboard:
        df = BY_ID.get(dashboard_id)
        if df is None:
            raise NotFound(dashboard_id)
        states = [st for st in (self._source_state(sid, acc) for sid in df.needs_any) if st != NOT_CONNECTED]
        if states and all(st == UNAUTHORIZED for st in states):
            raise Forbidden(dashboard_id)
        if filters.project and not acc.project(filters.project):
            raise Forbidden(filters.project)
        if filters.project and filters.project not in {str(p["id"]) for p in self.projects()}:
            raise NotFound(filters.project)
        if fresh and time.monotonic() - self._last_fresh >= FRESH_MIN_INTERVAL_S:
            # a caller may ask for a new read, but not make this server hammer the connected applications
            self._last_fresh = time.monotonic()
            self.cache.invalidate()
        self.cache.warm([sid for sid in WARM.get(df.id, df.needs_any) if acc.source(sid)])
        d = Dashboard(id=df.id, title=df.title, priority=df.priority)
        b = Build(dashboard=d, cache=self.cache, access=acc, store=self.store, filters=filters, projects=self.projects(), now=self.clock())
        df.builder(b)
        b.finish()
        if not states and not d.empty:
            # None of the applications this dashboard is built on is connected, so it is not offered; asked for directly it may
            # still show figures from a secondary source. It must then say what is missing, or half a dashboard reads as a whole.
            need = ", ".join(sorted({SOURCE_META[sid][0] for sid in df.needs_any}))
            d.empty = f"This dashboard needs {need}, which is not connected on this deployment. What is shown comes from other connected applications only."
        connected = [s for s in d.sources if s.state != NOT_CONNECTED]
        d.sources = connected or d.sources          # a source that was never connected is not listed next to ones that are
        return d

    def drill(self, dashboard_id: str, key: str, filters: Filters, acc: Access, *, limit: int = MAX_DRILL) -> Dict[str, Any]:
        d = self.build(dashboard_id, filters, acc)
        dr = d.drills.get(key)
        if dr is None:
            raise NotFound(key)
        limit = max(1, min(MAX_DRILL, limit))
        recs = sorted(dr.records, key=lambda r: -r.at)
        sources = sorted({r.source for r in recs})
        return {"dashboard": d.id, "key": key, "title": dr.title, "note": dr.note, "total": len(recs), "truncated": len(recs) > limit,
                "columns": list(dr.columns), "records": [r.to_dict() for r in recs[:limit]],
                "sources": [s.to_dict() for s in d.sources if s.id in sources],
                "actions": self.actions(d, dr, acc), "generated_at": d.generated_at}

    # ── actions ───────────────────────────────────────────────────────────────────────────────────
    def actions(self, d: Dashboard, dr: Drill, acc: Access) -> List[Dict[str, Any]]:
        ref = f"dashboard:{d.id}#{dr.key}"
        out: List[Dict[str, Any]] = [
            {"kind": "sidekick", "label": "Ask Sidekick about this", "ref": ref, "send": True,
             "prompt": f"Explain \"{dr.title}\" on the {d.title} dashboard: what it counts, the evidence behind it and what to do next."}]
        recs = dr.records
        if not recs:
            return out
        out.append({"kind": "track", "label": "Track in Today", "ref": ref,
                    "detail": "Adds a note to your intake with these records linked. Nothing is sent or changed."})
        hub = getattr(self.provider, "products_hub", None)
        failed_runs = sorted({(r.get("origin"), r.get("run_id")) for r in recs if r.source == "qa" and r.status == "failed" and r.get("run_id")})
        if hub is not None and failed_runs:
            prod, run = failed_runs[0]
            more = f" (first of {len(failed_runs)} failed runs)" if len(failed_runs) > 1 else ""
            sec = next((s.id for s in hub.specs() if s.kind == "security" and hub.ready(s.id)), "")
            if sec:
                out.append({"kind": "product", "label": "Escalate to a security case", "method": "POST", "human_only": True,
                            "path": f"/api/products/{prod}/runs/{run}/escalation", "body": {"severity": "high"},
                            "confirm": f"Open a security case for QA run {run}{more}? The case proposes no action by itself.", "ref": f"{prod}:{run}"})
            out.append({"kind": "product", "label": "Request a re-test", "method": "POST", "human_only": True,
                        "path": f"/api/products/{prod}/runs/{run}/retest", "body": {},
                        "confirm": f"Request a re-test of QA run {run}{more}? The new run still needs its own approval.", "ref": f"{prod}:{run}"})
        if any(r.kind == "security_action" and r.status in ("AWAITING_APPROVAL", "APPROVED") for r in recs) or \
                any(r.kind == "mission" and r.status == "needs" for r in recs) or any(r.kind == "attention" for r in recs):
            out.append({"kind": "navigate", "label": "Review in Today", "section": "today",
                        "detail": "Approvals are decided there, bound to the exact action you reviewed."})
        if any(r.status in ("failed", "blocked", "unexecuted") or r.get("open") or r.get("overdue") for r in recs) or d.id in ("security", "qa"):
            out.append({"kind": "sidekick", "label": "Plan a fix as a Mission", "ref": ref, "send": False,
                        "prompt": f"Propose a governed Mission to resolve \"{dr.title}\" ({len(recs)} record(s)) on {d.title}. List the steps and which need my approval."})
        if any(r.kind == "mission" for r in recs):
            out.append({"kind": "navigate", "label": "Open Missions", "section": "missions"})
        return out

    def track(self, dashboard_id: str, key: str, filters: Filters, acc: Access, *, session: str, note: str = "") -> Dict[str, Any]:
        """Hand a finding to the durable intake (Quick Capture). It never sends or approves anything."""
        d = self.build(dashboard_id, filters, acc)
        dr = d.drills.get(key)
        if dr is None:
            raise NotFound(key)
        fn = getattr(self.provider, "capture", None)
        if fn is None:
            raise NotImplementedError("this deployment does not provide capture")
        refs = sorted({r.ref for r in dr.records if r.ref})[:20]
        text = f"{d.title}: {dr.title} ({len(dr.records)} record(s))." + (f" {note.strip()[:300]}" if note.strip() else "")
        if refs:
            text += " Evidence: " + ", ".join(refs[:8]) + (f" and {len(refs) - 8} more" if len(refs) > 8 else "") + "."
        pid = filters.project or self.ctx.default_project or (self.projects()[0]["id"] if self.projects() else "")
        return fn(pid, session=session, content=text[:1900], source="manual")

    # ── Sidekick ──────────────────────────────────────────────────────────────────────────────────
    def explain(self, ref: str, acc: Access, filters: Optional[Filters] = None) -> Optional[Dict[str, Any]]:
        """A grounded answer about one number: what it counts, from which systems and how fresh, the records behind
        it, and what a person can do. ``ref`` is ``dashboard:<id>#<drill key>`` (or ``dashboard:<id>``)."""
        if not ref.startswith("dashboard:"):
            return None
        did, _, key = ref[len("dashboard:"):].partition("#")
        if did not in BY_ID:
            return None
        try:
            d = self.build(did, filters or parse_filters({}, now=self.clock()), acc)
        except (Forbidden, NotFound):
            return {"text": "You do not have access to that dashboard.", "topic": "Dashboards"}
        srcs = "; ".join(f"{s.label} ({s.state}{', read ' + _ago(d.generated_at - s.observed_at) if s.observed_at else ''})" for s in d.sources) or "no source"
        dr = d.drills.get(key) if key else None
        if dr is None:
            return {"text": f"{d.title}. {d.summary or d.empty} Sources: {srcs}.", "topic": "Dashboards",
                    "actions": [{"label": f"Open {d.title}", "kind": "navigate", "ref": "dashboards"}]}
        metric = next((m for p in d.panels for m in p.metrics if m.drill == key), None)
        lead = f"{dr.title}: {len(dr.records)} record(s)."
        if metric is not None and metric.value is not None:
            lead = f"{metric.label} is {metric.value:g}{metric.unit if metric.unit == '%' else ' ' + metric.unit if metric.unit else ''}"
            if metric.denominator:
                lead += f" ({metric.numerator:g} of {metric.denominator:g})"
            lead += f". The list behind it has {len(dr.records)} record(s): {dr.title.lower()}."
        top = sorted(dr.records, key=lambda r: -r.at)[:5]
        items = [{"title": r.title or r.id, "detail": " · ".join(x for x in (r.status, str(r.get("suite_name") or r.get("severity") or r.get("workflow") or ""), r.ref) if x)}
                 for r in top]
        acts = [a for a in self.actions(d, dr, acc) if a["kind"] in ("navigate", "product")]
        nxt = "; ".join(a["label"] for a in acts) or "open the records and decide"
        text = f"{lead} {dr.note + ' ' if dr.note else ''}Sources: {srcs}. Next: {nxt}."
        return {"text": text, "topic": "Dashboards", "items": items,
                "actions": [{"label": a["label"], "kind": "navigate", "ref": a.get("section", "dashboards")} for a in acts if a["kind"] == "navigate"]
                + [{"label": f"Open {d.title}", "kind": "navigate", "ref": "dashboards"}]}


def _ago(seconds: float) -> str:
    s = max(0, int(seconds))
    return "just now" if s < 60 else f"{s // 60} min ago" if s < 3600 else f"{s // 3600} h ago"


__all__ = ["DashboardService", "Definition", "DEFINITIONS", "BY_ID", "NotFound", "Forbidden", "parse_filters", "MAX_DRILL"]

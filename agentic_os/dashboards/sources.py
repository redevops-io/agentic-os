"""Sources: each reads ONE connected system through the client the kernel already has for it and returns that system's
facts as ``Record`` rows plus a ``SourceStatus`` saying whether, and when, the system answered.

Reuse, not a parallel engine: the registered products go through ``agentic_os.products`` (the same service tokens,
read cache and gates as Missions/Attention), Missions and Attention through the Projects provider, and the business
applications through the ``agentic_os.integrations`` clients and the Metabase client of ``agentic_os.visualization``.

Rules every source follows:
* a system that is not configured is ``not_connected`` (its dashboards are not offered); one that is configured but does
  not answer is ``unavailable`` and its metrics become "not known" — never zero, never the last value passed off as new;
* nothing is written to the system, and nothing read is stored here beyond a short read cache;
* a business source is read only when the operator enabled it in the dashboards file.
"""
from __future__ import annotations

import calendar
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .config import DashboardsConfig
from .contracts import GATED, NOT_CONNECTED, OK, STALE, UNAVAILABLE, Record, SourceStatus
from .store import DAY, DashboardStore

CACHE_TTL_S = 20.0
#: sources whose application rate-limits its API are read less often
SOURCE_TTL_S = {"postiz": 300.0}
EVIDENCE_TTL_S = 3600.0          # a finished run's evidence does not change
MAX_QA_RUNS = 40

#: source id -> (label, application id, the dashboards-file key that enables it or "" for always-on)
SOURCE_META: Dict[str, Tuple[str, str, str]] = {
    "missions": ("Missions", "", ""),
    "attention": ("Attention", "", ""),
    "runtime": ("Runtime health", "", ""),
    "context_sources": ("Context sources", "", ""),
    "deployment": ("Deployment inspection", "", ""),
    "ai_usage": ("Mission Runtime usage", "", ""),
    "qa": ("QA runs", "redevops-qa", ""),
    "qa_ingest": ("Reported test results", "", ""),
    "sentinel": ("Edge Sentinel", "edge-sentinel", ""),
    "twenty": ("Twenty CRM", "twenty", "twenty"),
    "erpnext": ("ERPNext", "erpnext", "erpnext"),
    "chatwoot": ("Chatwoot", "chatwoot", "chatwoot"),
    "lago": ("Lago billing", "lago", "lago"),
    "umami": ("Umami analytics", "umami", "umami"),
    "postiz": ("Postiz", "postiz", "postiz"),
    "listmonk": ("Listmonk", "listmonk", "listmonk"),
    "metabase": ("Control Tower (Metabase)", "metabase", "metabase"),
}


def parse_ts(v: Any) -> float:
    """Epoch seconds from an ISO-8601 string, a date, epoch seconds or epoch milliseconds; 0.0 when unknown."""
    if isinstance(v, bool) or v in (None, ""):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v) / 1000.0 if v > 1e11 else float(v)
    s = str(v).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?", s)
    if not m:
        return 0.0
    y, mo, d, h, mi, sec = (int(x) if x else 0 for x in m.groups())
    try:
        return float(calendar.timegm((y, mo, d, h, mi, sec, 0, 0, 0)))
    except (ValueError, OverflowError):
        return 0.0


@dataclass
class SourceResult:
    status: SourceStatus
    records: List[Record] = field(default_factory=list)


@dataclass
class Context:
    provider: Any
    config: DashboardsConfig
    store: DashboardStore
    default_project: str = ""
    now: Callable[[], float] = time.time
    clients: Dict[str, Any] = field(default_factory=dict)       # test seam: source id -> client (bypasses *_from_env)

    @property
    def hub(self) -> Any:
        return getattr(self.provider, "products_hub", None)


def _status(sid: str, state: str = OK, *, detail: str = "", at: float = 0.0, n: int = 0, basis: str = "live",
            remedy: str = "") -> SourceStatus:
    label, app, _ = SOURCE_META[sid]
    return SourceStatus(id=sid, label=label, app=app, state=state, detail=detail, observed_at=at, records=n, basis=basis, remedy=remedy)


#: The most records one collector reads of one kind. A source that holds more is NOT summarised as if this were all of it:
#: the source is then reported as partial and says so (``_partial``).
READ_CAP = 5000


def _partial(sid: str, what: Sequence[str], *, at: float, n: int) -> SourceStatus:
    """A source that holds more than the collector read. Counts and sums built on it are a LOWER BOUND, so the status says
    exactly that instead of letting a truncated read look like the whole application."""
    return _status(sid, STALE, at=at, n=n, detail=f"only the first {READ_CAP} {', '.join(what)} were read: counts and sums here are a lower bound",
                   remedy="narrow what this deployment reads, or raise the read limit")


class RemoteMissionPlane:
    """A deployed Mission Plane read over its own HTTP API (``GET /missions`` and ``GET /missions/{id}/events``), shaped
    like the in-process runtime the collectors already read. Read-only; ``MISSION_PLANE_URL`` (+ optional
    ``MISSION_PLANE_TOKEN``) name it, and it is used only when the operator enabled ``sources.mission_plane``."""

    def __init__(self, url: str, token: str = "", get: Optional[Callable[..., Tuple[int, Any]]] = None) -> None:
        self.url, self.token, self._get = url.rstrip("/"), token, get or _http_json
        self.repo = self

    def _read(self, path: str) -> Any:
        code, body = self._get(self.url + path, headers={"Authorization": f"Bearer {self.token}"} if self.token else {})
        if code >= 400 or body is None:
            raise RuntimeError(f"the Mission Plane answered {code}")
        return body

    def missions(self) -> List[Dict[str, Any]]:
        body = self._read("/missions")
        rows = body.get("missions") if isinstance(body, Mapping) else body
        return [dict(r) for r in rows or [] if isinstance(r, Mapping) and r.get("id")]

    def timeline(self, mission_id: str) -> List[Dict[str, Any]]:
        if not re.match(r"^[A-Za-z0-9_.:-]{1,120}$", str(mission_id)):
            return []
        body = self._read(f"/missions/{mission_id}/events")
        rows = body.get("timeline") if isinstance(body, Mapping) else body
        return [dict(r) for r in rows or [] if isinstance(r, Mapping)]


def _runtime(ctx: Context) -> Any:
    """The Mission Runtime the dashboards read: the one bound to the provider, else an enabled remote Mission Plane."""
    rt = getattr(ctx.provider, "mission_runtime", None)
    if rt is not None or "mission_plane" not in ctx.config.sources:
        return rt
    c = ctx.clients.get("mission_plane")
    if c is not None:
        return c
    import os  # noqa: PLC0415
    url = os.environ.get("MISSION_PLANE_URL", "").strip()
    return RemoteMissionPlane(url, os.environ.get("MISSION_PLANE_TOKEN", "").strip()) if url else None


_MISSION_STATE = {"succeeded": "completed", "completed": "completed", "failed": "failed", "compensation_failed": "failed", "cancelled": "failed",
                  "waiting_human": "needs", "planning": "running", "running": "running", "scheduled": "scheduled"}


def _basis(ctx: Context, *, product_rows: bool = False) -> str:
    """Whether the provider's own rows are fixtures, labelled so on every panel that uses them. A provider says so itself
    (``serves_fixtures``); a products registry replaces the fixture rows of the views it feeds (missions, attention, activity)
    unless the operator kept them, and it never replaces the provider's own sources or runtime units."""
    if not getattr(ctx.provider, "serves_fixtures", False):
        return "live"
    if product_rows and ctx.hub is not None and not getattr(ctx.hub.registry, "keep_sample_rows", False):
        return "live"
    return "sample"


def missions(ctx: Context) -> SourceResult:
    now = ctx.now()
    rows: List[Record] = []
    rt = _runtime(ctx)
    rt_error = ""
    if rt is not None:
        try:
            for m in rt.missions():
                tl = rt.repo.timeline(m["id"])
                created = next((e["ts"] for e in tl if e["type"] == "MissionCreated"), 0.0)
                ended = next((e["ts"] for e in tl if e["type"] == "MissionOutcome"), 0.0)
                state = str(m.get("state", ""))
                tmpl = str(m.get("template") or m.get("title") or "mission")
                title = str(m.get("title") or m.get("goal") or tmpl)
                rows.append(Record(
                    source="missions", kind="mission", id=str(m["id"]), title=title, at=float(ended or created or 0.0),
                    status="needs" if m.get("awaiting_human") else _MISSION_STATE.get(state, "running"),
                    project=ctx.config.project_for_mission(tmpl, title) or ctx.config.source_project("mission_plane") or ctx.default_project,
                    fields={"workflow": tmpl, "raw_state": state, "duration_ms": round((ended - created) * 1000) if created and ended else None,
                            "awaiting_human": bool(m.get("awaiting_human")), "awaiting_reconciliation": bool(m.get("awaiting_reconciliation")),
                            "created_at": created, "progress": state.replace("_", " "), "runtime": "mission"},
                    ref=f"mission:{m['id']}"))
        except Exception as e:  # noqa: BLE001
            if getattr(ctx.provider, "mission_runtime", None) is not None:
                return SourceResult(_status("missions", UNAVAILABLE, detail=f"the Mission Runtime did not answer ({type(e).__name__})"))
            rt_error = f"the Mission Plane did not answer ({type(e).__name__})"          # the provider's own rows are still shown, with this said
            rows = []
    try:
        listed = ctx.provider.missions(ctx.default_project)
    except Exception as e:  # noqa: BLE001
        return SourceResult(_status("missions", UNAVAILABLE, detail=f"missions could not be read ({type(e).__name__})"))
    from_runtime = bool(rows)
    for m in listed:
        refs = list(m.get("source_refs") or [])
        if str(m.get("id", "")).endswith(":unavailable"):
            continue
        if (from_runtime or rt is not None) and str(m.get("source_runtime", "")) not in ("security", "quality"):
            continue                                            # with a runtime read, only the products' own rows are added to it                                            # an explicit "product unavailable" row is a source status, not a mission
        wf = str(m.get("workflow") or "")
        product = str(m.get("id", "")).split(":", 1)[0] if ":" in str(m.get("id", "")) else ""
        rows.append(Record(
            source="missions", kind="mission", id=str(m.get("id", "")), title=str(m.get("title", "")), status=str(m.get("state", "")),
            project=(ctx.config.project_for_product(product) if product else "") or ctx.config.project_for_mission(wf, str(m.get("title", ""))) or ctx.default_project,
            at=0.0, fields={"workflow": wf, "progress": str(m.get("progress", "")), "runtime": str(m.get("source_runtime", ""))},
            ref=refs[0] if refs else f"mission:{m.get('id', '')}"))
    if rt_error:
        return SourceResult(_status("missions", STALE if rows else UNAVAILABLE, detail=rt_error, at=now if rows else 0.0, n=len(rows)), rows)
    return SourceResult(_status("missions", at=now, n=len(rows), basis="live" if rt is not None else _basis(ctx, product_rows=True)), rows)


def attention(ctx: Context) -> SourceResult:
    now = ctx.now()
    try:
        listed = ctx.provider.attention(ctx.default_project)
    except Exception as e:  # noqa: BLE001
        return SourceResult(_status("attention", UNAVAILABLE, detail=f"attention could not be read ({type(e).__name__})"))
    rows = []
    for a in listed:
        refs = list(a.get("source_refs") or [])
        product = str(a.get("id", "")).split(":", 1)[0] if ":" in str(a.get("id", "")) else ""
        rows.append(Record(
            source="attention", kind="attention", id=str(a.get("id", "")), title=str(a.get("title", "")), status=str(a.get("kind", "")),
            project=(ctx.config.project_for_product(product) if product else "") or ctx.default_project, at=0.0,
            fields={"reason": str(a.get("reason", "")), "consequence": str(a.get("consequence", "")), "priority": a.get("priority", 0),
                    "runtime": str(a.get("source_runtime", ""))},
            ref=refs[0] if refs else f"attention:{a.get('id', '')}"))
    return SourceResult(_status("attention", at=now, n=len(rows), basis=_basis(ctx, product_rows=True)), rows)


def runtime(ctx: Context) -> SourceResult:
    """Deployment health: the runtime units and models the provider reports, plus each registered product's readiness."""
    now = ctx.now()
    rows: List[Record] = []
    try:
        rt = ctx.provider.runtime(ctx.default_project) or {}
    except Exception as e:  # noqa: BLE001
        return SourceResult(_status("runtime", UNAVAILABLE, detail=f"runtime health could not be read ({type(e).__name__})"))
    for u in rt.get("runtimes") or []:
        rows.append(Record(source="runtime", kind="runtime_unit", id=str(u.get("name", "")), title=str(u.get("name", "")), status=str(u.get("state", "")),
                           at=now, fields={"detail": str(u.get("detail", "")), "group": "runtime"}, ref=f"runtime:{u.get('name', '')}"))
    for m in rt.get("models") or []:
        rows.append(Record(source="runtime", kind="model", id=str(m.get("name", "")), title=str(m.get("name", "")), status=str(m.get("state", "")),
                           at=now, fields={"role": str(m.get("role", "")), "group": "model"}, ref=f"model:{m.get('name', '')}"))
    hub = ctx.hub
    if hub is not None:
        for spec in hub.specs():
            st = hub.status(spec.id)
            h = st["health"]
            rows.append(Record(source="runtime", kind="product", id=spec.id, title=spec.display_name,
                               status="ok" if st["ready"] else "bad" if h["state"] == "down" else "warn", at=float(h.get("checked_at") or now),
                               project=ctx.config.project_for_product(spec.id),
                               fields={"detail": h.get("detail", ""), "version": st.get("version", ""), "group": "product",
                                       "platform": str((st.get("info") or {}).get("platform") or "")},
                               ref=f"product:{spec.id}", url=spec.ui_url))
    return SourceResult(_status("runtime", at=now, n=len(rows), basis=_basis(ctx)), rows)


def context_sources(ctx: Context) -> SourceResult:
    now = ctx.now()
    try:
        listed = ctx.provider.sources(ctx.default_project) or []
    except Exception as e:  # noqa: BLE001
        return SourceResult(_status("context_sources", UNAVAILABLE, detail=f"context sources could not be read ({type(e).__name__})"))
    rows = []
    for s in listed:
        h = s.get("health") if isinstance(s.get("health"), Mapping) else {}
        st = s.get("stats") if isinstance(s.get("stats"), Mapping) else {}

        def num(k: str) -> Optional[float]:
            v = st.get(k)
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        rows.append(Record(source="context_sources", kind="context_source", id=str(s.get("source_id", "")), title=str(s.get("name", "")),
                           status=str(h.get("state") or "unknown"), project=ctx.default_project, at=parse_ts(h.get("last_observed_at")),
                           fields={"kind": str(s.get("kind", "")), "provider": str(s.get("provider", "")), "detail": str(h.get("detail", "")),
                                   "discovered": num("discovered") if num("discovered") is not None else num("files"), "indexed": num("indexed"),
                                   "skipped": num("skipped"), "tables": num("tables"), "access_mode": str(s.get("access_mode", "")),
                                   "indexing": str(s.get("indexing_policy", "")), "last_verified": str(s.get("last_verified", ""))},
                           ref=(list(s.get("source_refs") or []) or [f"source:{s.get('source_id', '')}"])[0]))
    if not rows:
        return SourceResult(_status("context_sources", NOT_CONNECTED, detail="no context source is connected",
                                    remedy="connect a folder, a database or a drive under Connections"))
    return SourceResult(_status("context_sources", at=now, n=len(rows), basis=_basis(ctx)), rows)


def deployment(ctx: Context) -> SourceResult:
    fn = getattr(ctx.provider, "deployment_inspection", None)
    if fn is None:
        return SourceResult(_status("deployment", NOT_CONNECTED, detail="this deployment does not provide deployment inspection"))
    now = ctx.now()
    try:
        view = fn(ctx.default_project) or {}
    except Exception as e:  # noqa: BLE001
        return SourceResult(_status("deployment", UNAVAILABLE, detail=f"the inspection could not be read ({type(e).__name__})"))
    if (view.get("boundary") or {}).get("inspection") == "unavailable":
        return SourceResult(_status("deployment", NOT_CONNECTED, detail="no deployment is named for inspection (PROJECTS_INSPECTION_TARGET)"))
    live = bool(view.get("connected"))
    rows = [Record(source="deployment", kind="deployment_finding", id=f"{f.get('source', '')}:{f.get('kind', '')}:{i}", title=str(f.get("detail") or f.get("kind") or ""),
                   status=str(f.get("severity") or "unknown").lower(), project=ctx.default_project, at=now,
                   fields={"plane": str(f.get("source", "")), "finding": str(f.get("kind", "")), "target": str(view.get("target", ""))},
                   ref=str(f.get("evidence_ref") or f"deployment:{f.get('kind', '')}"))
            for i, f in enumerate(view.get("findings") or []) if isinstance(f, Mapping)]
    return SourceResult(_status("deployment", at=now, n=len(rows), basis="live" if live else "sample",
                                detail="" if live else "an offline sample snapshot, not a live inspection"), rows)


def ai_usage(ctx: Context) -> SourceResult:
    """What the Mission Runtime recorded about its own work: one row per executed step (capability, duration, and the
    spend/tokens ONLY when the step event carries them — most runtimes record none) and one per plan with the planner's
    projected cost. Nothing is estimated here; without a runtime the dashboard is not offered."""
    rt = _runtime(ctx)
    if rt is None:
        return SourceResult(_status("ai_usage", NOT_CONNECTED, detail="no Mission Runtime is bound to this deployment, so no usage is recorded"))
    now, rows = ctx.now(), []
    try:
        for m in rt.missions():
            tmpl = str(m.get("template") or m.get("title") or "mission")
            project = ctx.config.project_for_mission(tmpl, str(m.get("title") or m.get("goal") or "")) or ctx.config.source_project("mission_plane") or ctx.default_project
            ends: Dict[str, Tuple[float, str]] = {}
            tl = rt.repo.timeline(m["id"])
            for e in tl:
                if e.get("type") in ("NodeSucceeded", "NodeFailed", "NodeCompensated"):
                    ends.setdefault(str((e.get("payload") or {}).get("node_id")), (float(e.get("ts") or 0.0), str(e.get("type"))[4:].lower()))
            for e in tl:
                p = e.get("payload") or {}
                ts = float(e.get("ts") or 0.0)
                if e.get("type") == "NodeDispatched":
                    end = ends.get(str(p.get("node_id")))
                    cost, tokens = p.get("cost_usd"), p.get("tokens")
                    rows.append(Record(source="ai_usage", kind="usage", id=f"{m['id']}:{p.get('node_id', '')}", title=str(p.get("capability") or ""),
                                       status=end[1] if end else "running", project=project, at=ts,
                                       fields={"mission": str(m["id"]), "workflow": tmpl, "capability": str(p.get("capability") or ""),
                                               "operator": str(p.get("capability") or "").split(".", 1)[0], "model": str(p.get("model") or ""),
                                               "cost_usd": float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
                                               "tokens": int(tokens) if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) else None,
                                               "duration_ms": round((end[0] - ts) * 1000) if end and end[0] >= ts else None},
                                       ref=f"mission:{m['id']}"))
                elif e.get("type") == "PlanCreated" and isinstance(p.get("projection"), Mapping):
                    pr = p["projection"]
                    usd = pr.get("expected_usd")
                    rows.append(Record(source="ai_usage", kind="plan", id=f"{m['id']}:{p.get('plan_id', '')}", title=str(p.get("signature") or p.get("plan_id") or "plan")[:160],
                                       status=str(p.get("reason") or "plan"), project=project, at=ts,
                                       fields={"mission": str(m["id"]), "workflow": tmpl, "expected_usd": float(usd) if isinstance(usd, (int, float)) and not isinstance(usd, bool) else None,
                                               "expected_success": pr.get("expected_success"), "expected_approvals": pr.get("expected_approvals"),
                                               "within_budget": pr.get("within_budget"), "revision": p.get("revision")},
                                       ref=f"mission:{m['id']}"))
    except Exception as e:  # noqa: BLE001
        return SourceResult(_status("ai_usage", UNAVAILABLE, detail=f"the Mission Runtime did not answer ({type(e).__name__})"))
    return SourceResult(_status("ai_usage", at=now, n=len(rows)), rows)


# ── registered products ─────────────────────────────────────────────────────────────────────────────────────────────
def _product_gate(ctx: Context, sid: str, kind: str) -> Tuple[List[Any], Optional[SourceStatus]]:
    hub = ctx.hub
    specs = [s for s in hub.specs() if s.kind == kind] if hub is not None else []
    if not specs:
        return [], _status(sid, NOT_CONNECTED, detail="no product of this kind is registered",
                           remedy="register it in the products registry (AGENTIC_OS_PRODUCTS)")
    ready = [s for s in specs if hub.ready(s.id)]
    if not ready:
        why = hub.unavailable_reason(specs[0].id)
        gated = str(why.get("code", "")).endswith("not-met")
        return [], _status(sid, GATED if gated else UNAVAILABLE, detail=str(why.get("reason", "")), remedy=str(why.get("remedy", "")))
    return ready, None


_QA_VERDICT = {"VERIFIED": "passed", "FAILED": "failed"}


def qa_cell_status(verdict: str, native: Optional[str]) -> str:
    """One assertion's result as the dashboards count it. An engine that could not run on the platform is
    ``unsupported``; anything else that produced no verdict is ``blocked``. Neither is ever counted as executed."""
    if verdict in _QA_VERDICT:
        return _QA_VERDICT[verdict]
    if native == "NATIVE_UNAVAILABLE":
        return "unsupported"
    return "blocked"


def qa(ctx: Context) -> SourceResult:
    specs, gate = _product_gate(ctx, "qa", "quality")
    if gate is not None:
        return SourceResult(gate)
    from agentic_os.products.client import ProductError  # noqa: PLC0415
    hub, now, rows, problems = ctx.hub, ctx.now(), [], []
    for spec in specs:
        ad = hub.qa(spec.id)
        try:
            listed = hub.cached(f"{spec.id}:dash:runs", lambda: ad.runs(limit=MAX_QA_RUNS, actor="projects:dashboards", source="projects"), CACHE_TTL_S)
        except ProductError as e:
            hub.invalidate(spec.id)
            problems.append(f"{spec.display_name}: {e.reason or e.detail or e.code}")
            continue
        for r in (listed.get("items") or []) if isinstance(listed, Mapping) else []:
            rid, target, profile = str(r.get("id", "")), str(r.get("target_id") or ""), str(r.get("profile") or r.get("plan") or "")
            suite = ctx.config.suite_for_run(target=target, profile=profile)
            suite_id = suite.id if suite else f"{target or 'target'}/{profile or 'run'}"
            project = (suite.project if suite else ctx.config.project_for_qa(target=target)) or ctx.config.project_for_product(spec.id) or ctx.default_project
            state, outcome = str(r.get("state") or ""), str(r.get("outcome") or "")
            at = parse_ts(r.get("finished") or r.get("started") or r.get("created"))
            base = {"suite": suite_id, "suite_name": suite.name if suite else suite_id, "target": target, "profile": profile,
                    "environment": suite.environment if suite else "", "product": (suite.product if suite else "") or spec.id,
                    "outcome": outcome, "level": str(r.get("level") or ""), "ceiling": str(r.get("level_ceiling") or ""),
                    "state": state, "origin": spec.id}
            url = spec.ui_link(f"/ui/runs/{rid}")
            run_fields = dict(base)
            if state == "finished" and r.get("bundle"):
                try:
                    ev = hub.cached(f"{spec.id}:dash:evidence:{rid}", lambda rid=rid: ad.evidence(rid, actor="projects:dashboards", source="projects"), EVIDENCE_TTL_S)
                except ProductError as e:
                    ev = None
                    run_fields["evidence_error"] = e.code
                if isinstance(ev, Mapping):
                    lanes = {str(l.get("lane_id")): l for l in ev.get("lanes") or [] if isinstance(l, Mapping)}
                    accepted = (ev.get("verification") or {}).get("accepted")
                    run_fields["evidence_verified"] = bool(accepted) if accepted is not None else None
                    run_fields["platforms"] = sorted({_lane_platform(l) for l in lanes.values()})
                    for a in ev.get("assertions") or []:
                        if not isinstance(a, Mapping):
                            continue
                        lane = lanes.get(str(a.get("lane_id")), {})
                        aid = str(a.get("assertion_id", ""))
                        rows.append(Record(
                            source="qa", kind="qa_result", id=f"{rid}/{aid}", title=aid, at=at, project=project,
                            status=qa_cell_status(str((a.get("verdict") or {}).get("state") or ""), a.get("native_status")),
                            fields={**base, "run_id": rid, "test_id": aid, "platform": _lane_platform(lane),
                                    "reason": str((a.get("verdict") or {}).get("reason_code") or ""), "native": a.get("native_status"),
                                    "evidence_verified": run_fields["evidence_verified"]},
                            ref=f"{spec.id}:{rid}", url=url))
            rows.append(Record(source="qa", kind="qa_run", id=rid, title=f"QA run {rid}", at=at, project=project,
                               status=_qa_run_status(state, outcome), fields=run_fields, ref=f"{spec.id}:{rid}", url=url))
    if problems and not rows:
        return SourceResult(_status("qa", UNAVAILABLE, detail="; ".join(problems)))
    return SourceResult(_status("qa", at=now, n=len(rows), detail="; ".join(problems)), rows)


def _lane_platform(lane: Mapping[str, Any]) -> str:
    eng = lane.get("engine") if isinstance(lane.get("engine"), Mapping) else {}
    return str(eng.get("name") or lane.get("lane_id") or "default")


def _qa_run_status(state: str, outcome: str) -> str:
    if state == "finished":
        return {"VERIFIED": "passed", "CORROBORATED": "passed", "FAILED": "failed"}.get(outcome, "blocked")
    if state in ("running", "queued"):
        return "running"
    if state == "awaiting_approval":
        return "awaiting_approval"
    return "blocked"                 # refused, cancelled, timed out, interrupted, failed to start: nothing was verified


def qa_ingest(ctx: Context) -> SourceResult:
    """Results a CI lane reported with ``POST /api/dashboards/qa/results`` (project, suite and platform are stated by
    the reporter, the requirement links by the reporter or the catalog)."""
    now = ctx.now()
    runs = ctx.store.qa_runs(since=now - max(ctx.config.inventory_days, 90) * DAY)
    if not runs:
        return SourceResult(_status("qa_ingest", NOT_CONNECTED, detail="no test results have been reported to this deployment",
                                    remedy="report a run with POST /api/dashboards/qa/results (see docs/DASHBOARDS.md)", basis="ingested"))
    keys = [(r["project"], r["suite"], r["platform"], r["run_id"]) for r in runs]
    results = ctx.store.qa_results(keys)
    rows: List[Record] = []
    for r in runs:
        key = (r["project"], r["suite"], r["platform"], r["run_id"])
        spec = ctx.config.suites.get(r["suite"])
        base = {"suite": r["suite"], "suite_name": spec.name if spec else r["suite"], "environment": r["environment"] or (spec.environment if spec else ""),
                "product": r["product"] or (spec.product if spec else ""), "platform": r["platform"], "origin": r["origin"],
                "evidence_verified": None if r["evidence_verified"] is None else bool(r["evidence_verified"]),
                "release": r["release"] if "release" in r.keys() else ""}
        res = results.get(key, [])
        failed = sum(1 for x in res if x["status"] == "failed")
        ref = r["ref"] or f"{r['origin']}:{r['run_id']}"
        rows.append(Record(source="qa_ingest", kind="qa_run", id=f"{r['suite']}/{r['platform']}/{r['run_id']}", title=f"{r['suite']} on {r['platform']}",
                           at=r["at"], project=r["project"], status="failed" if failed else "passed" if any(x["status"] == "passed" for x in res) else "blocked",
                           fields={**base, "run_id": r["run_id"], "platforms": [r["platform"]]}, ref=ref, url=r["url"]))
        for x in res:
            rows.append(Record(source="qa_ingest", kind="qa_result", id=f"{r['run_id']}/{r['platform']}/{x['test_id']}", title=x["test_id"], at=r["at"],
                               project=r["project"], status=x["status"],
                               fields={**base, "run_id": r["run_id"], "test_id": x["test_id"], "severity": x["severity"], "reason": x["detail"],
                                       "requirements": list(x["requirements"]), "duration_ms": x["duration_ms"]},
                               ref=ref, url=r["url"]))
    return SourceResult(_status("qa_ingest", at=max(r["ingested_at"] for r in runs), n=len(rows), basis="ingested"), rows)


def sentinel(ctx: Context) -> SourceResult:
    specs, gate = _product_gate(ctx, "sentinel", "security")
    if gate is not None:
        return SourceResult(gate)
    from agentic_os.products.client import ProductError  # noqa: PLC0415
    hub, now, rows, problems = ctx.hub, ctx.now(), [], []
    for spec in specs:
        ad = hub.sentinel(spec.id)
        owner = ctx.config.project_for_product(spec.id)
        try:
            cases = hub.cached(f"{spec.id}:dash:cases", lambda: ad.cases(limit=100, actor="projects:dashboards"), CACHE_TTL_S)
            actions = hub.cached(f"{spec.id}:dash:actions", lambda: ad.actions(limit=100, actor="projects:dashboards"), CACHE_TTL_S)
            posture = hub.cached(f"{spec.id}:dash:posture", lambda: ad.posture(actor="projects:dashboards"), CACHE_TTL_S)
        except ProductError as e:
            hub.invalidate(spec.id)
            problems.append(f"{spec.display_name}: {e.reason or e.detail or e.code}")
            continue
        case_project: Dict[str, str] = {}
        for c in (cases.get("items") or []) if isinstance(cases, Mapping) else []:
            cid = str(c.get("case_id", ""))
            asset = str(c.get("asset") or c.get("host") or c.get("ip") or "")
            project = ctx.config.project_for_asset(asset, str(c.get("title", "")), str(c.get("case_type", ""))) or owner or ctx.default_project
            case_project[cid] = project
            status = str(c.get("status") or "").upper()
            rows.append(Record(
                source="sentinel", kind="security_case", id=cid, title=str(c.get("title") or cid), status=status, project=project,
                at=parse_ts(c.get("opened_at") or c.get("created_at") or c.get("first_seen") or c.get("updated_at")),
                fields={"severity": str(c.get("severity") or "unknown").lower(), "case_type": str(c.get("case_type") or ""),
                        "open": status not in ("RESOLVED", "CLOSED", "DISMISSED", "FALSE_POSITIVE"),
                        "evidence": (c.get("counts") or {}).get("evidence", len(c.get("evidence") or [])) if isinstance(c.get("counts"), Mapping) else len(c.get("evidence") or []), "closed_at": parse_ts(c.get("closed_at") or c.get("resolved_at")),
                        "environment": str(c.get("environment") or ""), "product": spec.id, "asset": asset,
                        "control": str(c.get("control") or c.get("framework") or "")},
                ref=f"{spec.id}:{cid}", url=spec.ui_link(f"/ui/cases/{cid}")))
        for a in (actions.get("items") or []) if isinstance(actions, Mapping) else []:
            rid, cid = str(a.get("request_id", "")), str(a.get("case_id", ""))
            ver = a.get("verification") if isinstance(a.get("verification"), Mapping) else {}
            rows.append(Record(
                source="sentinel", kind="security_action", id=rid, title=str(a.get("capability") or rid), status=str(a.get("state") or "").upper(),
                project=case_project.get(cid) or owner or ctx.default_project, at=parse_ts(a.get("created_at") or ver.get("at")),
                fields={"case_id": cid, "capability": str(a.get("capability") or ""), "verification": str(ver.get("state") or "not_run"),
                        "attempts": a.get("attempts", 0), "product": spec.id},
                ref=f"{spec.id}:{rid}", url=spec.ui_link(f"/ui/cases/{cid}")))
        if isinstance(posture, Mapping):
            rows.append(Record(source="sentinel", kind="security_posture", id=spec.id, title=f"{spec.display_name} posture", status=str(posture.get("source") or ""),
                               project=owner or ctx.default_project, at=now,
                               fields={"decisions_active": posture.get("decisions_active"), "alerts_total": posture.get("alerts_total"),
                                       "by_severity": dict(posture.get("by_severity") or {}), "connected": posture.get("connected"), "product": spec.id},
                               ref=f"{spec.id}:posture", url=spec.ui_url))
    if problems and not rows:
        return SourceResult(_status("sentinel", UNAVAILABLE, detail="; ".join(problems)))
    return SourceResult(_status("sentinel", at=now, n=len(rows), detail="; ".join(problems)), rows)


# ── business applications (opt-in) ──────────────────────────────────────────────────────────────────────────────────
def _client(ctx: Context, sid: str, factory: Callable[[], Any], env_hint: str) -> Tuple[Any, Optional[SourceStatus]]:
    c = ctx.clients.get(sid)
    if c is None:
        try:
            c = factory()
        except Exception:  # noqa: BLE001
            c = None
    if c is None:
        return None, _status(sid, NOT_CONNECTED, detail="no connection is configured for this application", remedy=f"set {env_hint}")
    probe = getattr(c, "connected", None)
    if probe is not None:
        try:
            up = bool(probe())
        except Exception:  # noqa: BLE001
            up = False
        if not up:
            return None, _status(sid, UNAVAILABLE, detail="the application did not answer or refused the credential",
                                 remedy="check that it is running and that its credential is current")
    return c, None


def twenty(ctx: Context) -> SourceResult:
    from agentic_os.integrations.twenty import twenty_from_env  # noqa: PLC0415
    c, bad = _client(ctx, "twenty", twenty_from_env, "TWENTY_BASE_URL and TWENTY_API_KEY")
    if bad:
        return SourceResult(bad)
    now, project = ctx.now(), ctx.config.source_project("twenty")
    opps, more_o = _twenty_all(c, "opportunities")
    companies, more_c = _twenty_all(c, "companies")
    if not opps and not companies and not getattr(c, "connected", None):
        # this client swallows transport errors; one cheap probe tells an empty CRM from an unreachable one
        import httpx  # noqa: PLC0415
        try:
            r = httpx.get(f"{c.base_url}/rest/companies", params={"limit": 1}, headers={"Authorization": f"Bearer {c.api_key}"}, timeout=6.0)
            if r.status_code >= 400:
                return SourceResult(_status("twenty", UNAVAILABLE, detail=f"Twenty answered {r.status_code}", remedy="check the API key"))
        except Exception as e:  # noqa: BLE001
            return SourceResult(_status("twenty", UNAVAILABLE, detail=f"Twenty did not answer ({type(e).__name__})"))
    names = {str(co.get("id")): str(co.get("name") or "") for co in companies}
    rows: List[Record] = []
    for o in opps:
        amt = o.get("amount") if isinstance(o.get("amount"), Mapping) else {}
        micros = amt.get("amountMicros")
        amount = float(micros) / 1e6 if isinstance(micros, (int, float)) and not isinstance(micros, bool) else None
        stage = str(o.get("stage") or "").upper()
        rows.append(Record(source="twenty", kind="deal", id=str(o.get("id", "")), title=str(o.get("name") or ""), status=stage, project=project,
                           at=parse_ts(o.get("createdAt")),
                           fields={"amount": amount, "currency": str(amt.get("currencyCode") or ""), "company": names.get(str(o.get("companyId")), ""),
                                   "close_date": parse_ts(o.get("closeDate")), "updated_at": parse_ts(o.get("updatedAt")),
                                   "won": stage in ("CUSTOMER", "WON", "CLOSED_WON"), "lost": stage in ("LOST", "CLOSED_LOST")},
                           ref=f"twenty:{o.get('id', '')}", url=f"{c.base_url}/object/opportunity/{o.get('id', '')}"))
    for co in companies:
        rows.append(Record(source="twenty", kind="account", id=str(co.get("id", "")), title=str(co.get("name") or ""), status="account", project=project,
                           at=parse_ts(co.get("createdAt")), fields={"employees": co.get("employees")},
                           ref=f"twenty:{co.get('id', '')}", url=f"{c.base_url}/object/company/{co.get('id', '')}"))
    cut = [w for w, m in (("opportunities", more_o), ("companies", more_c)) if m]
    return SourceResult(_partial("twenty", cut, at=now, n=len(rows)) if cut else _status("twenty", at=now, n=len(rows)), rows)


def _twenty_all(c: Any, obj: str) -> Tuple[List[Dict[str, Any]], bool]:
    """Every record of one Twenty object, following the REST cursor (the first page alone is 60-200 records, which made a CRM
    with 599 companies read as 200). Returns ``(rows, truncated)``; a transport failure returns what the client's own
    single-page read gives, so an unreachable CRM is still told apart from an empty one by the caller's probe."""
    import httpx  # noqa: PLC0415
    rows: List[Dict[str, Any]] = []
    cursor = None
    try:
        with httpx.Client(timeout=getattr(c, "timeout", 10.0)) as client:
            while len(rows) < READ_CAP:
                r = client.get(f"{c.base_url}/rest/{obj}", headers={"Authorization": f"Bearer {c.api_key}"},
                               params={"limit": 200, **({"starting_after": cursor} if cursor else {})})
                if r.status_code >= 400:
                    return (rows, False) if rows else (getattr(c, obj)(limit=200), False)
                body = r.json()
                data = body.get("data") or {}
                page = (data.get(obj) if isinstance(data, Mapping) else data) or []
                rows.extend(x for x in page if isinstance(x, Mapping))
                info = body.get("pageInfo") or {}
                cursor = info.get("endCursor")
                if not page or not info.get("hasNextPage") or not cursor:
                    return rows, False
    except Exception:  # noqa: BLE001
        return (rows, False) if rows else (getattr(c, obj)(limit=200), False)
    return rows[:READ_CAP], True


def erpnext(ctx: Context) -> SourceResult:
    from agentic_os.integrations.erpnext import erpnext_from_env  # noqa: PLC0415
    c, bad = _client(ctx, "erpnext", erpnext_from_env, "ERPNEXT_URL, ERPNEXT_API_KEY and ERPNEXT_API_SECRET")
    if bad:
        return SourceResult(bad)
    now, project, rows = ctx.now(), ctx.config.source_project("erpnext"), []
    base = c.base_url

    def money(v: Any) -> Optional[float]:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    for d in c.get_list("Sales Invoice", ["name", "customer", "grand_total", "outstanding_amount", "due_date", "status", "posting_date", "currency"], limit=READ_CAP + 1)[:READ_CAP + 1]:
        rows.append(Record(source="erpnext", kind="invoice", id=str(d.get("name")), title=f"{d.get('name')} · {d.get('customer') or ''}", status=str(d.get("status") or ""),
                           project=project, at=parse_ts(d.get("posting_date")),
                           fields={"amount": money(d.get("grand_total")), "outstanding": money(d.get("outstanding_amount")), "due": parse_ts(d.get("due_date")),
                                   "customer": str(d.get("customer") or ""), "currency": str(d.get("currency") or "")},
                           ref=f"erpnext:Sales Invoice/{d.get('name')}", url=f"{base}/app/sales-invoice/{d.get('name')}"))
    for d in c.get_list("Purchase Invoice", ["name", "supplier", "grand_total", "outstanding_amount", "due_date", "status", "posting_date", "currency"], limit=READ_CAP + 1)[:READ_CAP + 1]:
        rows.append(Record(source="erpnext", kind="bill", id=str(d.get("name")), title=f"{d.get('name')} · {d.get('supplier') or ''}", status=str(d.get("status") or ""),
                           project=project, at=parse_ts(d.get("posting_date")),
                           fields={"amount": money(d.get("grand_total")), "outstanding": money(d.get("outstanding_amount")), "due": parse_ts(d.get("due_date")),
                                   "supplier": str(d.get("supplier") or ""), "currency": str(d.get("currency") or "")},
                           ref=f"erpnext:Purchase Invoice/{d.get('name')}", url=f"{base}/app/purchase-invoice/{d.get('name')}"))
    for d in c.get_list("Purchase Order", ["name", "supplier", "grand_total", "status", "transaction_date", "schedule_date", "per_received", "currency"], limit=READ_CAP + 1)[:READ_CAP + 1]:
        rows.append(Record(source="erpnext", kind="purchase_order", id=str(d.get("name")), title=f"{d.get('name')} · {d.get('supplier') or ''}", status=str(d.get("status") or ""),
                           project=project, at=parse_ts(d.get("transaction_date")),
                           fields={"amount": money(d.get("grand_total")), "supplier": str(d.get("supplier") or ""), "expected": parse_ts(d.get("schedule_date")),
                                   "received_pct": money(d.get("per_received")), "currency": str(d.get("currency") or "")},
                           ref=f"erpnext:Purchase Order/{d.get('name')}", url=f"{base}/app/purchase-order/{d.get('name')}"))
    for d in c.get_list("Supplier", ["name", "supplier_group", "disabled", "on_hold"], limit=READ_CAP + 1)[:READ_CAP + 1]:
        rows.append(Record(source="erpnext", kind="supplier", id=str(d.get("name")), title=str(d.get("name")), project=project, at=0.0,
                           status="on_hold" if d.get("on_hold") else "disabled" if d.get("disabled") else "active",
                           fields={"group": str(d.get("supplier_group") or "")}, ref=f"erpnext:Supplier/{d.get('name')}", url=f"{base}/app/supplier/{d.get('name')}"))
    kinds = {"invoice": "sales invoices", "bill": "supplier bills", "purchase_order": "purchase orders", "supplier": "suppliers"}
    cut = [label for k, label in kinds.items() if sum(1 for r in rows if r.kind == k) > READ_CAP]
    if cut:                                             # keep the cap, and say the read was cut
        seen: Dict[str, int] = {}
        kept = []
        for r in rows:
            seen[r.kind] = seen.get(r.kind, 0) + 1
            if seen[r.kind] <= READ_CAP:
                kept.append(r)
        return SourceResult(_partial("erpnext", cut, at=now, n=len(kept)), kept)
    return SourceResult(_status("erpnext", at=now, n=len(rows)), rows)


#: Chatwoot lists 25 conversations a page; five pages (125) silently cut a busy desk short.
CHATWOOT_PAGES = 80


def chatwoot(ctx: Context) -> SourceResult:
    from agentic_os.integrations.chatwoot import chatwoot_from_env  # noqa: PLC0415
    c, bad = _client(ctx, "chatwoot", chatwoot_from_env, "CHATWOOT_API_URL and CHATWOOT_API_TOKEN")
    if bad:
        return SourceResult(bad)
    now, project, rows = ctx.now(), ctx.config.source_project("chatwoot"), []
    cut: List[str] = []
    for status in ("open", "pending", "resolved"):
        listed = c.conversations(status=status, max_pages=CHATWOOT_PAGES)
        if len(listed) >= CHATWOOT_PAGES * 25:
            cut.append(f"{status} conversations")
        for cv in listed:
            cid = str(cv.get("id", ""))
            created, first = parse_ts(cv.get("created_at")), parse_ts(cv.get("first_reply_created_at"))
            sender = (cv.get("meta") or {}).get("sender") or {}
            rows.append(Record(source="chatwoot", kind="conversation", id=cid, title=str(sender.get("name") or f"Conversation {cid}"), status=str(cv.get("status") or status),
                               project=project, at=created,
                               fields={"priority": str(cv.get("priority") or "none"), "first_reply_s": (first - created) if first and created and first >= created else None,
                                       "waiting_since": parse_ts(cv.get("waiting_since")), "unread": cv.get("unread_count", 0),
                                       "channel": str((cv.get("meta") or {}).get("channel") or ""), "assigned": bool((cv.get("meta") or {}).get("assignee")),
                                       "last_activity": parse_ts(cv.get("last_activity_at"))},
                               ref=f"chatwoot:{cid}", url=f"{c.base_url}/app/accounts/{c.account_id}/conversations/{cid}"))
    if cut:
        return SourceResult(_status("chatwoot", STALE, at=now, n=len(rows), detail=f"only the first {CHATWOOT_PAGES * 25} {', '.join(cut)} were read: counts here are a lower bound"), rows)
    return SourceResult(_status("chatwoot", at=now, n=len(rows)), rows)


def lago(ctx: Context) -> SourceResult:
    from agentic_os.integrations.lago import lago_from_env  # noqa: PLC0415
    c, bad = _client(ctx, "lago", lago_from_env, "LAGO_API_URL and LAGO_API_KEY")
    if bad:
        return SourceResult(bad)
    now, project, rows = ctx.now(), ctx.config.source_project("lago"), []
    probe = c._get("/api/v1/subscriptions", {"per_page": 1})            # this client returns {} on a refused key
    if not isinstance(probe, Mapping) or "subscriptions" not in probe:
        return SourceResult(_status("lago", UNAVAILABLE, detail="Lago refused the API key or did not answer", remedy="check LAGO_API_KEY"))
    for s in c.subscriptions(status="active", max_pages=10):
        rows.append(Record(source="lago", kind="subscription", id=str(s.get("external_id") or s.get("lago_id")), title=str(s.get("name") or s.get("plan_code") or ""),
                           status=str(s.get("status") or ""), project=project, at=parse_ts(s.get("started_at") or s.get("created_at")),
                           fields={"plan": str(s.get("plan_code") or ""), "customer": str(s.get("external_customer_id") or "")},
                           ref=f"lago:subscription/{s.get('external_id') or s.get('lago_id')}"))
    for page in range(1, 6):
        data = c._get("/api/v1/invoices", {"page": page, "per_page": 100})
        inv = data.get("invoices") or []
        for i in inv:
            cents = i.get("total_amount_cents")
            rows.append(Record(source="lago", kind="invoice", id=str(i.get("lago_id")), title=str(i.get("number") or i.get("lago_id")), status=str(i.get("payment_status") or i.get("status") or ""),
                               project=project, at=parse_ts(i.get("issuing_date") or i.get("created_at")),
                               fields={"amount": float(cents) / 100.0 if isinstance(cents, (int, float)) else None, "currency": str(i.get("currency") or ""),
                                       "customer": str((i.get("customer") or {}).get("external_id") or ""), "overdue": bool(i.get("payment_overdue")),
                                       "invoice_status": str(i.get("status") or "")},
                               ref=f"lago:invoice/{i.get('lago_id')}"))
        if not inv or page >= int((data.get("meta") or {}).get("total_pages", page)):
            break
    return SourceResult(_status("lago", at=now, n=len(rows)), rows)


def umami(ctx: Context) -> SourceResult:
    from agentic_os.integrations.umami import umami_from_env  # noqa: PLC0415
    c, bad = _client(ctx, "umami", umami_from_env, "UMAMI_URL, UMAMI_USERNAME and UMAMI_PASSWORD")
    if bad:
        return SourceResult(bad)
    now, project, rows = ctx.now(), ctx.config.source_project("umami"), []
    if not c.token():
        return SourceResult(_status("umami", UNAVAILABLE, detail="Umami refused the sign-in", remedy="check UMAMI_USERNAME and UMAMI_PASSWORD"))
    sites = c.websites()
    days = 30
    for site in sites:
        wid = str(site.get("id") or "")
        one = type(c)(base_url=c.base_url, website_id=wid, username=c.username, password=c.password)
        one._token = c._token
        st = one.stats(days=days)

        def val(k: str) -> Optional[float]:
            v = st.get(k)
            v = v.get("value") if isinstance(v, Mapping) else v
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        rows.append(Record(source="umami", kind="site_traffic", id=wid, title=str(site.get("name") or site.get("domain") or wid), status="tracked", project=project, at=now,
                           fields={"domain": str(site.get("domain") or ""), "days": days, "pageviews": val("pageviews"), "visitors": val("visitors"),
                                   "visits": val("visits"), "bounces": val("bounces"), "totaltime": val("totaltime")},
                           ref=f"umami:{wid}", url=f"{c.base_url}/websites/{wid}"))
        for p in one.top_pages(days=days, limit=10):
            rows.append(Record(source="umami", kind="page", id=f"{wid}{p.get('x') or p.get('path')}", title=str(p.get("x") or p.get("path") or ""), status="page", project=project, at=now,
                               fields={"site": str(site.get("domain") or ""), "views": p.get("y") if "y" in p else p.get("views")}, ref=f"umami:{wid}"))
    return SourceResult(_status("umami", at=now, n=len(rows)), rows)


def _http_json(url: str, *, headers: Mapping[str, str], params: Optional[Mapping[str, Any]] = None, timeout: float = 10.0) -> Tuple[int, Any]:
    import httpx  # noqa: PLC0415
    r = httpx.get(url, headers=dict(headers), params=dict(params or {}), timeout=timeout, follow_redirects=False)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


def postiz(ctx: Context) -> SourceResult:
    """Scheduled and published posts through Postiz's public API (``POSTIZ_API_URL`` + ``POSTIZ_API_KEY``)."""
    import os  # noqa: PLC0415
    c = ctx.clients.get("postiz")
    url, key = (c or {}).get("url") or os.environ.get("POSTIZ_API_URL", "").rstrip("/"), (c or {}).get("key") or os.environ.get("POSTIZ_API_KEY", "")
    if not (url and key):
        return SourceResult(_status("postiz", NOT_CONNECTED, detail="no connection is configured for this application", remedy="set POSTIZ_API_URL and POSTIZ_API_KEY"))
    now, project, rows = ctx.now(), ctx.config.source_project("postiz"), []
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))  # noqa: E731
    get = (c or {}).get("get") or _http_json
    posts: List[Mapping[str, Any]] = []
    try:
        code, data = get(f"{url}/public/v1/posts", headers={"Authorization": key}, params={"startDate": iso(now - 60 * DAY), "endDate": iso(now + 30 * DAY)})
        if code in (301, 302, 307, 308, 404):
            # an older Postiz serves the public API under /api and pages it by calendar month instead of a date range
            seen: Dict[str, Mapping[str, Any]] = {}
            for back in (0, 1):                     # this API is rate-limited: the current and the previous month, no more
                t = time.gmtime(now - back * 30 * DAY)
                code, data = get(f"{url}/api/public/v1/posts", headers={"Authorization": key},
                                 params={"display": "month", "year": t.tm_year, "month": t.tm_mon, "week": max(1, min(52, int(time.strftime("%V", t)))), "day": 0})
                if code >= 400 or not isinstance(data, Mapping):
                    break
                for p in data.get("posts") or []:
                    if isinstance(p, Mapping):
                        seen[str(p.get("id"))] = p
            else:
                data = {"posts": list(seen.values())}
    except Exception as e:  # noqa: BLE001
        return SourceResult(_status("postiz", UNAVAILABLE, detail=f"Postiz did not answer ({type(e).__name__})"))
    if code >= 400 or not isinstance(data, Mapping):
        return SourceResult(_status("postiz", UNAVAILABLE, detail=f"Postiz answered {code}", remedy="check POSTIZ_API_KEY and POSTIZ_API_URL"))
    posts = [p for p in data.get("posts") or [] if isinstance(p, Mapping)]
    for p in posts:
        integ = p.get("integration") if isinstance(p.get("integration"), Mapping) else {}
        rows.append(Record(source="postiz", kind="post", id=str(p.get("id", "")), title=re.sub(r"<[^>]+>", "", str(p.get("content") or ""))[:120], status=str(p.get("status") or "").lower(),
                           project=project, at=parse_ts(p.get("publishDate")),
                           fields={"channel": str(integ.get("providerIdentifier") or ""), "account": str(integ.get("name") or ""), "release_url": str(p.get("releaseURL") or "")},
                           ref=f"postiz:{p.get('id', '')}", url=str(p.get("releaseURL") or "")))
    return SourceResult(_status("postiz", at=now, n=len(rows)), rows)


def listmonk(ctx: Context) -> SourceResult:
    """Email campaigns and lists (``LISTMONK_URL`` + ``LISTMONK_API_USER``/``LISTMONK_API_TOKEN``)."""
    import base64  # noqa: PLC0415
    import os  # noqa: PLC0415
    c = ctx.clients.get("listmonk")
    url = (c or {}).get("url") or (os.environ.get("LISTMONK_URL") or os.environ.get("LISTMONK_API_URL") or "").rstrip("/")
    user, token = (c or {}).get("user") or os.environ.get("LISTMONK_API_USER", ""), (c or {}).get("token") or os.environ.get("LISTMONK_API_TOKEN", "")
    if not (url and user and token):
        return SourceResult(_status("listmonk", NOT_CONNECTED, detail="no connection is configured for this application",
                                    remedy="set LISTMONK_URL, LISTMONK_API_USER and LISTMONK_API_TOKEN"))
    now, project, rows = ctx.now(), ctx.config.source_project("listmonk"), []
    hdr = {"Authorization": "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()}
    get = (c or {}).get("get") or _http_json
    try:
        code, camps = get(f"{url}/api/campaigns", headers=hdr, params={"per_page": 100, "order_by": "created_at", "order": "DESC"})
        code2, lists = get(f"{url}/api/lists", headers=hdr, params={"per_page": 100})
    except Exception as e:  # noqa: BLE001
        return SourceResult(_status("listmonk", UNAVAILABLE, detail=f"Listmonk did not answer ({type(e).__name__})"))
    if code >= 400 or code2 >= 400 or not isinstance(camps, Mapping) or not isinstance(lists, Mapping):
        return SourceResult(_status("listmonk", UNAVAILABLE, detail=f"Listmonk answered {max(code, code2)}", remedy="check the API user and token"))
    for cp in ((camps.get("data") or {}).get("results") or []):
        rows.append(Record(source="listmonk", kind="campaign", id=str(cp.get("id", "")), title=str(cp.get("name") or ""), status=str(cp.get("status") or ""), project=project,
                           at=parse_ts(cp.get("started_at") or cp.get("created_at")),
                           fields={"sent": cp.get("sent"), "to_send": cp.get("to_send"), "views": cp.get("views"), "clicks": cp.get("clicks"), "bounces": cp.get("bounces"),
                                   "channel": "email"},
                           ref=f"listmonk:campaign/{cp.get('id', '')}", url=f"{url}/admin/campaigns/{cp.get('id', '')}"))
    for li in ((lists.get("data") or {}).get("results") or []):
        rows.append(Record(source="listmonk", kind="list", id=str(li.get("id", "")), title=str(li.get("name") or ""), status=str(li.get("type") or ""), project=project,
                           at=parse_ts(li.get("created_at")), fields={"subscribers": li.get("subscriber_count")},
                           ref=f"listmonk:list/{li.get('id', '')}", url=f"{url}/admin/lists/{li.get('id', '')}"))
    return SourceResult(_status("listmonk", at=now, n=len(rows)), rows)


def metabase(ctx: Context) -> SourceResult:
    """Control Tower's saved questions, run by Metabase itself. The operator names the cards in the dashboards file
    (``sources.metabase.cards: {<series id>: <card id>}``); Projects never sends SQL of its own."""
    import os  # noqa: PLC0415
    opts = ctx.config.sources.get("metabase") or {}
    cards = opts.get("cards") if isinstance(opts.get("cards"), Mapping) else {}
    w = ctx.clients.get("metabase")
    if w is None:
        url, email, pw = os.environ.get("METABASE_API_URL", "").rstrip("/"), os.environ.get("METABASE_ADMIN_EMAIL", ""), os.environ.get("METABASE_ADMIN_PASSWORD", "")
        session = os.environ.get("METABASE_SESSION", "").strip()          # an existing session token, as Control Tower uses
        if not (url and ((email and pw) or session)):
            return SourceResult(_status("metabase", NOT_CONNECTED, detail="no Metabase connection is configured",
                                        remedy="set METABASE_API_URL, METABASE_ADMIN_EMAIL and METABASE_ADMIN_PASSWORD"))
        from agentic_os.visualization.client import HttpMetabaseWriter  # noqa: PLC0415
        w = HttpMetabaseWriter(base_url=url, email=email, password=pw)
        if session:
            w._token = session
    now, project, rows = ctx.now(), ctx.config.source_project("metabase"), []
    public = str(opts.get("public_url") or getattr(w, "base_url", "")).rstrip("/")
    try:
        with w._client() as c:
            try:
                who_code = w._req(c, "GET", "/api/user/current").status_code      # a source with no card configured must still prove it can sign in
            except Exception:  # noqa: BLE001  (an expired session with no password to sign in again)
                who_code = 401
            if who_code >= 400:
                return SourceResult(_status("metabase", UNAVAILABLE, detail=f"Metabase did not accept the sign-in ({who_code})",
                                            remedy="renew METABASE_SESSION, or set METABASE_ADMIN_EMAIL and METABASE_ADMIN_PASSWORD"))
            for series, card_id in cards.items():
                if isinstance(card_id, bool) or not isinstance(card_id, int):
                    continue
                r = w._req(c, "POST", f"/api/card/{card_id}/query")
                if r.status_code >= 400:
                    return SourceResult(_status("metabase", UNAVAILABLE, detail=f"Metabase answered {r.status_code} for card {card_id}",
                                                remedy="check the card id and the Metabase credential"))
                data = (r.json() or {}).get("data") or {}
                cols = [str(col.get("name") or col.get("display_name") or f"c{i}") for i, col in enumerate(data.get("cols") or [])]
                for i, vals in enumerate(data.get("rows") or []):
                    row = dict(zip(cols, vals))
                    first = vals[0] if vals else ""
                    rows.append(Record(source="metabase", kind="card_row", id=f"{card_id}:{i}", title=str(first), status=str(series), project=project,
                                       at=parse_ts(first) or parse_ts(str(first) + "-01") if isinstance(first, str) else 0.0,
                                       fields={"series": str(series), "card_id": card_id, "row": row, "columns": cols},
                                       ref=f"metabase:card/{card_id}", url=f"{public}/question/{card_id}" if public else ""))
    except Exception as e:  # noqa: BLE001
        return SourceResult(_status("metabase", UNAVAILABLE, detail=f"Metabase did not answer ({type(e).__name__})"))
    return SourceResult(_status("metabase", at=now, n=len(rows)), rows)


COLLECTORS: Dict[str, Callable[[Context], SourceResult]] = {
    "missions": missions, "attention": attention, "runtime": runtime, "context_sources": context_sources,
    "deployment": deployment, "ai_usage": ai_usage, "qa": qa, "qa_ingest": qa_ingest, "sentinel": sentinel,
    "twenty": twenty, "erpnext": erpnext, "chatwoot": chatwoot, "lago": lago, "umami": umami, "postiz": postiz,
    "listmonk": listmonk, "metabase": metabase,
}


class SourceCache:
    """One read of each source per ``CACHE_TTL_S``, shared by every dashboard and drill-down that needs it, so a number
    and the records behind it come from the same read."""

    def __init__(self, ctx: Context, ttl: float = CACHE_TTL_S) -> None:
        self.ctx, self.ttl = ctx, ttl
        self._lock = threading.Lock()
        self._hit: Dict[str, Tuple[float, SourceResult]] = {}

    def enabled(self, sid: str) -> bool:
        key = SOURCE_META[sid][2]
        return not key or key in self.ctx.config.sources

    def get(self, sid: str, *, fresh: bool = False) -> SourceResult:
        if not self.enabled(sid):
            return SourceResult(_status(sid, NOT_CONNECTED, detail="this source is not enabled on this deployment",
                                        remedy=f"list `{SOURCE_META[sid][2]}` under `sources` in the dashboards file"))
        now = time.monotonic()
        with self._lock:
            hit = self._hit.get(sid)
            if hit and not fresh and now - hit[0] < max(self.ttl, SOURCE_TTL_S.get(sid, 0.0)):
                return hit[1]
        try:
            res = COLLECTORS[sid](self.ctx)
        except Exception as e:  # noqa: BLE001  (a source must never take the dashboards down with it)
            res = SourceResult(_status(sid, UNAVAILABLE, detail=f"reading this source failed ({type(e).__name__})"))
        with self._lock:
            self._hit[sid] = (now, res)
        return res

    def warm(self, sids: Sequence[str]) -> None:
        """Read several sources at once. A person waits for the slowest application, not for the sum of them."""
        todo = [sid for sid in dict.fromkeys(sids) if sid in COLLECTORS and self.enabled(sid)]
        now = time.monotonic()
        with self._lock:
            todo = [sid for sid in todo if not (self._hit.get(sid) and now - self._hit[sid][0] < max(self.ttl, SOURCE_TTL_S.get(sid, 0.0)))]
        if len(todo) < 2:
            return
        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
        with ThreadPoolExecutor(max_workers=min(8, len(todo)), thread_name_prefix="dash-src") as pool:
            list(pool.map(self.get, todo))

    def invalidate(self) -> None:
        """Forget what was read, except from a rate-limited source: a forced refresh must not spend its quota."""
        now = time.monotonic()
        with self._lock:
            # kept whether it answered or refused: asking a rate-limited API again straight away only extends the refusal
            self._hit = {sid: h for sid, h in self._hit.items() if sid in SOURCE_TTL_S and now - h[0] < SOURCE_TTL_S[sid]}


__all__ = ["SOURCE_META", "COLLECTORS", "Context", "SourceResult", "SourceCache", "parse_ts", "qa_cell_status", "CACHE_TTL_S"]

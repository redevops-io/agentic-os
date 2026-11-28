"""Market Intelligence as governed work, on what already exists: Market Radar's operator does the watching, auditing,
recommending and checking; the Mission Runtime runs its research as a durable mission, from the existing
``market_intelligence_research`` template. This holds only what neither has:

* **which sites** an agent may name (``AGENTIC_OS_MARKET_SITES``, the operator's list): an agent cannot point the
  auditor or a watch at an address of its choosing;
* **which research and plans belong to which tenant** (``market.sqlite``; Market Radar has no tenants);
* a follow-up that a person authorizes: a **change plan** made of recommendations from a research mission this tenant ran
  (never of text the agent wrote), approved through Market Radar's own ``radar.approve_plan``, bound to the list's digest,
  and afterwards checked against the live site with ``radar.verify_change``.

A research step whose outcome no connected capability provides (the template's analytics readings, which belong to another
application) is left out, and the result says so.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.parse import urlsplit

from agentic_os.governed_writes.apps import NotApplied, OutcomeUnknown

OPERATOR = "market-radar"
ENV_SITES = "AGENTIC_OS_MARKET_SITES"
MAX_PAGES = 100


def allowed_sites() -> List[str]:
    """The origins (``https://host``) the deployment lets agents audit and watch."""
    out = []
    for raw in os.environ.get(ENV_SITES, "").split(","):
        u = urlsplit(raw.strip())
        if u.scheme in ("http", "https") and u.hostname and not u.username:
            out.append(f"{u.scheme}://{u.netloc}".lower())
    return sorted(set(out))


def is_listed(url: str) -> bool:
    """Whether the URL's whole origin is one the operator listed (never a prefix of the text: a look-alike host is not it)."""
    try:
        u = urlsplit(str(url).strip())
    except ValueError:
        return False
    return u.scheme in ("http", "https") and not u.username and not u.password and f"{u.scheme}://{u.netloc}".lower() in allowed_sites()


def allowed_url(url: str) -> str:
    """The URL when its origin is one the operator listed, else ``NotApplied``."""
    if not is_listed(url):
        raise NotApplied(f"that address is not one this deployment lets agents research ({ENV_SITES}); see market.sites")
    return str(url).strip()


@dataclasses.dataclass
class RadarClient:
    """Market Radar's operator app, with ITS control token (never this deployment's)."""
    base_url: str
    control_token: str
    timeout: float = 180.0
    post: Optional[Callable[..., Any]] = None            # test seam: a kernel Transport
    get: Optional[Callable[[str], dict]] = None          # test seam

    def _transport(self) -> Callable[..., Any]:
        from agentic_os.mission.util import post_json  # noqa: PLC0415

        def send(url: str, body: dict, headers: dict, timeout: float) -> Any:
            headers = {**headers, "Authorization": f"Bearer {self.control_token}"}
            return self.post(url, body, headers, timeout) if self.post is not None else post_json(url, body, headers, timeout, None)
        return send

    def operator_client(self) -> Any:
        from agentic_os.mission.operators import HTTPOperatorClient  # noqa: PLC0415
        return HTTPOperatorClient({OPERATOR: self.base_url}, timeout=self.timeout, transport=self._transport())

    def registry(self) -> Any:
        from agentic_os.mission.production import discover  # noqa: PLC0415
        try:
            registry, _ = discover({OPERATOR: self.base_url}, fetch=self.get)
        except Exception as e:  # noqa: BLE001
            raise NotApplied(f"Market Radar could not be reached ({type(e).__name__})") from None
        return registry

    def invoke(self, capability: str, inputs: dict, key: str) -> Dict[str, Any]:
        try:
            out = self.operator_client().invoke(OPERATOR, capability, dict(inputs), key)
        except Exception as e:  # noqa: BLE001
            refused = str(e)[:8] in ("http_401", "http_403")
            if refused:                                  # its gate refuses before the handler runs: nothing happened
                raise NotApplied("Market Radar refused the call: it did not accept this deployment's sign-in or authority") from None
            raise OutcomeUnknown(f"no usable answer from Market Radar ({type(e).__name__})") from None
        if not isinstance(out, dict):
            raise OutcomeUnknown("Market Radar's answer could not be read")
        return out


def operators_from_env() -> Dict[str, Any]:
    url, token = os.environ.get("MARKET_RADAR_URL", "").strip(), os.environ.get("MARKET_RADAR_CONTROL_TOKEN", "").strip()
    return {OPERATOR: RadarClient(url.rstrip("/"), token)} if url and token else {}


# ── what belongs to whom ─────────────────────────────────────────────────────────
class Ledger:
    def __init__(self, state_dir: "str | Path", clock: Callable[[], float] = time.time) -> None:
        db = Path(state_dir) / "market.sqlite"
        if not db.exists():
            os.close(os.open(db, os.O_CREAT | os.O_WRONLY, 0o600))
        self.clock, self._lock = clock, threading.RLock()
        self._db = sqlite3.connect(str(db), check_same_thread=False, isolation_level=None, timeout=30)
        self._db.execute("CREATE TABLE IF NOT EXISTS records(tenant TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,"
                         " created REAL NOT NULL, PRIMARY KEY(tenant, kind, id))")

    def put(self, tenant: str, kind: str, ident: str, data: Mapping[str, Any]) -> None:
        with self._lock:
            self._db.execute("INSERT INTO records VALUES(?,?,?,?,?) ON CONFLICT(tenant,kind,id) DO UPDATE SET data=excluded.data",
                             (tenant, kind, ident, json.dumps(dict(data), default=str), self.clock()))

    def get(self, tenant: str, kind: str, ident: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT data FROM records WHERE tenant=? AND kind=? AND id=?", (tenant, kind, str(ident))).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, tenant: str, kind: str, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM records WHERE tenant=? AND kind=? ORDER BY created DESC LIMIT ?", (tenant, kind, limit)).fetchall()
        return [json.loads(r[0]) for r in rows]


# ── research: a durable mission from the existing template ───────────────────────
class _TemplatePlanner:
    """The ``market_intelligence_research`` template, without the steps nothing connected here can do."""

    def __init__(self, provided: set, fields: dict) -> None:
        self.provided, self.fields, self.left_out = provided, fields, []

    def plan(self, mission_id: str, goal: str, context: Any) -> Any:
        from agentic_os.mission.templates import market_intelligence_research  # noqa: PLC0415
        intent = market_intelligence_research(mission_id)
        keep = [s for s in intent.steps if s.outcome in self.provided]
        self.left_out = [s.outcome for s in intent.steps if s.outcome not in self.provided]
        kept = {s.outcome for s in keep}
        for s in keep:
            s.inputs_from = [o for o in (s.inputs_from or []) if o in kept]
        return dataclasses.replace(intent, steps=keep, verified_fields=dict(self.fields))


#: all a research mission may ever do: read. Anything else the planner produced would be refused by the runtime.
RESEARCH_CAPABILITIES = ("radar.brief", "radar.site_audit", "radar.recommend")
RESEARCH_TTL_S = 3600.0


def _authorize_research(guard: Any, mission_id: str, *, tenant: str, requester: str, site: str) -> None:
    """This one mission's authority, issued after the gateway authorized the agent (its grant and its tenant's
    application): the three reading capabilities at the reading tier, no spend, no delegation, for an hour. It is stored
    beside every other envelope, so it can be audited and revoked."""
    from agentic_os.mission.authority import AuthorityEnvelope, AutonomyBudget, Grant  # noqa: PLC0415
    now = guard.clock()
    guard.envelopes.put(AuthorityEnvelope(
        tenant=tenant, principal=requester, mission_id=str(mission_id), goal=f"read-only market research of {site}",
        grants=tuple(Grant(capability=name, max_tier=0) for name in RESEARCH_CAPABILITIES),
        budget=AutonomyBudget(usd=0.0, max_actions=3 * len(RESEARCH_CAPABILITIES), max_children=0, max_depth=0, max_parallel=1),
        may_delegate=False, issued_at=now, expires_at=now + RESEARCH_TTL_S))


def run_research(radar: RadarClient, state_dir: Path, *, tenant: str, requester: str, site: str, max_pages: int,
                 authority: Any = None) -> Dict[str, Any]:
    from agentic_os.mission.executor import Executor  # noqa: PLC0415
    from agentic_os.mission.runtime import MissionRuntime  # noqa: PLC0415
    from agentic_os.mission.store import EventStore  # noqa: PLC0415
    registry = radar.registry()
    planner = _TemplatePlanner({o for spec in registry.all() for o in spec.provides}, {"site": site, "max_pages": max_pages})
    if authority is None:
        from agentic_os.mission.authority import authority_from_env  # noqa: PLC0415
        authority = authority_from_env()
    runtime = MissionRuntime(registry, Executor(radar.operator_client()), store=EventStore(str(Path(state_dir) / "market-events.jsonl")),
                             planner=planner, authority=authority)
    mission = runtime.create_mission(f"market research for {site}", principal=requester, tenant=tenant)
    if authority is not None:
        _authorize_research(authority, mission.id, tenant=tenant, requester=requester, site=site)
    runtime.run(mission.id)
    results: Dict[str, Any] = {}
    for event in runtime.store.for_mission(mission.id):
        if event.type == "NodeSucceeded":
            res = event.payload.get("result")
            if isinstance(res, dict):
                results[str(event.payload.get("capability") or "")] = res
    mission = runtime._missions[mission.id]
    audit = (results.get("radar.site_audit") or {}).get("site_audit")
    recs = (results.get("radar.recommend") or {}).get("recommendations")
    state = getattr(mission.state, "value", str(mission.state))
    return {"mission_id": mission.id, "site": site, "state": state, "steps_left_out": planner.left_out,
            "steps": [{"capability": c, "status": r.get("status"), "summary": str(r.get("summary", ""))[:400]} for c, r in results.items()],
            "audit": audit, "recommendations": recs, "watch": (results.get("radar.brief") or {}).get("market_brief"), "requested_by": requester}


def plan_digest(items: List[Mapping[str, Any]]) -> str:
    """The same digest Market Radar computes over a list of recommendations (``apps/market-radar/loop.plan_digest``)."""
    return hashlib.sha256(json.dumps([{"id": i["id"], "checks": i["checks"]} for i in items], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _ledger(ctx: Any) -> Ledger:
    return Ledger(ctx.state_dir, ctx.journal.clock)


def _chosen(ctx: Any, i: Mapping[str, Any]) -> Dict[str, Any]:
    research = _ledger(ctx).get(ctx.tenant, "research", str(i["research_id"]))
    if research is None:
        raise NotApplied("there is no such research of yours (see market.list_research)")
    recs = research.get("recommendations") or {}
    by_id = {r["id"]: r for r in recs.get("items", [])}
    wanted = list(dict.fromkeys(i["recommendation_ids"]))
    unknown = [w for w in wanted if w not in by_id]
    if unknown:
        raise NotApplied(f"that research has no recommendation {', '.join(unknown)}; it has: {', '.join(by_id) or 'none'}")
    items = [by_id[w] for w in wanted]
    if not any(x.get("checks") for x in items):
        raise NotApplied("none of these asks for a change that could be checked: there is nothing to approve")
    return {"site": recs.get("site"), "items": items, "digest": plan_digest(items)}


def prepare_plan(ctx: Any, radar: Any, i: Mapping[str, Any]) -> Dict[str, Any]:
    plan = _chosen(ctx, i)
    allowed_url(plan["site"])
    show = [["Site", plan["site"]], ["From research", str(i["research_id"])]]
    for item in plan["items"]:
        show.append([f"{item['id']} ({item.get('severity', '')})",
                     f"{item.get('title', '')}: {item.get('why', '')} Pages: {', '.join(item.get('pages', [])[:6]) or 'none'}. "
                     f"{len(item.get('checks', []))} machine check(s) must pass on the live site."])
    return {"digest": plan["digest"], "research_id": str(i["research_id"]), "show": show,
            "summary": f"Approve a change plan of {len(plan['items'])} recommendation(s) for {plan['site']}"}


def perform_plan(radar: Any, i: Mapping[str, Any], footer: str, rid: str, ctx: Any):
    from agentic_os.governed_writes.actions import Outcome  # noqa: PLC0415
    plan = _chosen(ctx, i)
    if plan["digest"] != (ctx.evidence or {}).get("digest"):
        raise NotApplied("the recommendations changed since they were approved: propose the plan again")
    marker = {"approved": True, "by": ctx.approver, "tenant": ctx.tenant, "plan_digest": plan["digest"]}
    out = radar.invoke("radar.approve_plan", {"recommendations": plan, "_mission": rid, "_node": "approve", "_approval": marker},
                       f"plan:{rid}")
    approved = out.get("change_plan_approved")
    if out.get("status") != "done" or not isinstance(approved, dict):
        why = str(out.get("error") or out.get("summary") or out.get("status"))[:300]
        raise NotApplied(f"Market Radar did not record the approval: {why}")
    verified = approved.get("digest") == plan["digest"] and approved.get("approved_by") == ctx.approver
    _ledger(ctx).put(ctx.tenant, "plan", rid, {"plan_id": rid, "research_id": str(i["research_id"]), "plan": approved})
    return Outcome(True, verified, rid, f"Market Radar recorded the plan of {len(approved.get('items', []))} item(s), approved by "
                   f"{approved.get('approved_by')}; check it against the live site with market.verify_plan")


def prepare_watch(ctx: Any, radar: Any, i: Mapping[str, Any]) -> Dict[str, Any]:
    url = allowed_url(i["url"])
    return {"show": [["Page to watch", url], ["What happens", "Market Radar fetches this public page from now on and reports changes"]],
            "summary": f"Start watching {url}"}


def perform_watch(radar: Any, i: Mapping[str, Any], footer: str, rid: str, ctx: Any):
    from agentic_os.governed_writes.actions import Outcome  # noqa: PLC0415
    url = allowed_url(i["url"])
    body = {"url": url, **({"title": str(i["title"])} if i.get("title") else {})}
    # Market Radar runs a change only with a person's approval: it verifies this signed, single-use token itself
    marker = {"approved": True, "by": ctx.approver, "tenant": ctx.tenant}
    out = radar.invoke("radar.add_watch", {**body, "_mission": rid, "_node": "watch", "_approval": marker}, f"watch:{rid}")
    watch = out.get("watch") if isinstance(out.get("watch"), (dict, str)) else None
    ident = str((watch.get("uuid") if isinstance(watch, dict) else watch) or out.get("uuid") or "")
    if out.get("status") == "error" or not ident:
        raise NotApplied(f"Market Radar did not add the watch: {str(out.get('error') or out.get('summary') or out.get('status'))[:300]}")
    try:
        listed = url in json.dumps(radar.invoke("radar.brief", {}, f"watch:{rid}:read:{ctx.journal.clock()}"))
    except (NotApplied, OutcomeUnknown):
        listed = False
    _ledger(ctx).put(ctx.tenant, "watch", ident, {"watch_id": ident, "url": url, "request_id": rid})
    return Outcome(True, listed, ident, "added, and listed by Market Radar" if listed else "added; it is not yet in Market Radar's brief")


__all__ = ["OPERATOR", "ENV_SITES", "MAX_PAGES", "allowed_sites", "allowed_url", "RadarClient", "operators_from_env", "Ledger",
           "run_research", "plan_digest", "prepare_plan", "perform_plan", "prepare_watch", "perform_watch"]

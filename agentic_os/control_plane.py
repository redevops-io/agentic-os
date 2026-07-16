"""FastAPI control plane — deploy, observe, and approve, from one place.

    uvicorn agentic_os.control_plane:app --port 8080
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import asdict
from pathlib import Path

import httpx
import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import channels, views
from .context import Context
from .fleet import Fleet
from .registry import Module, Registry
from .router import Router

CONFIG_PATH = os.environ.get("AGENTIC_OS_CONFIG", "config.yaml")

# Read-only demo posture: the per-app dashboards stay viewable, but their mutating surface
# (the /api/agent chat, whose tools include real core-mutating actions) is turned off — governed
# actions run through the Mission cockpit. v6: apps are orchestrated by the runtime, not driven
# ad-hoc per app. Set DEMO_READ_ONLY=1 for the public demo; unset (0) for local/dev.
DEMO_READ_ONLY = os.environ.get("DEMO_READ_ONLY", "0") == "1"
COCKPIT_URL = os.environ.get("COCKPIT_URL", "https://demo.redevops.io/cockpit")


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Auth dependency for mutating endpoints.

    If ``AGENTIC_OS_API_KEY`` is set in the environment, every POST route requires
    a matching ``X-API-Key`` header (401 otherwise). If it is unset, auth is
    disabled — in that case the control plane MUST be bound to localhost only
    (do not expose it on 0.0.0.0 without setting AGENTIC_OS_API_KEY).
    """
    expected = os.environ.get("AGENTIC_OS_API_KEY")
    if expected and x_api_key != expected:
        raise HTTPException(401, "invalid or missing X-API-Key")


def require_permissions_admin(x_api_key: str | None = Header(default=None)) -> None:
    """Auth for the permissions WRITE path — its own key so gating grant edits is independent of the
    fleet-ops key. Uses PERMISSIONS_ADMIN_KEY, falling back to AGENTIC_OS_API_KEY; open when neither
    is set (bind to localhost / put behind your gateway in that case)."""
    expected = os.environ.get("PERMISSIONS_ADMIN_KEY") or os.environ.get("AGENTIC_OS_API_KEY")
    if expected and x_api_key != expected:
        raise HTTPException(401, "invalid or missing X-API-Key")


def _build() -> Fleet:
    registry = Registry.load()
    cfg = {}
    if Path(CONFIG_PATH).exists():
        cfg = yaml.safe_load(Path(CONFIG_PATH).read_text(encoding="utf-8")) or {}
    router = Router.from_config(cfg.get("router", {"tiers": []}))
    # Hermes 0.17 chat notifier — no-op until a Telegram/Slack token is configured.
    notifier = channels.Notifier()
    ctx = Context(os.environ.get("AGENTIC_OS_HOME", ".agentic-os"), notifier=notifier)
    return Fleet(registry, router, ctx)


app = FastAPI(title="agentic-os control plane", version="0.1.0")
fleet = _build()

# Mission Runtime (Whitepaper v5) — mount /missions + /capabilities over the pilot operators.
# Guarded so the control plane still boots if the mission package is unavailable.
try:
    from .mission.service import mount as _mount_missions
    mission_runtime = _mount_missions(app)
except Exception:  # noqa: BLE001 - mission layer is additive; never block the control plane
    mission_runtime = None
# Hermes 0.17 inbound chatops gateway — daemon thread, started only if a channel
# is configured (closed by default; honors AGENTIC_OS_GATEWAY_ALLOW / _OPEN).
_gateway = channels.Gateway(fleet)


@app.on_event("startup")
def _start_gateway() -> None:
    _gateway.start()

# Serve the per-repo card images (deploy/assets/repos/<name>.png) at /assets/...
_ASSETS_DIR = Path(__file__).resolve().parent.parent / "deploy" / "assets"
if _ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_ASSETS_DIR)), name="assets")

# Functional grouping of modules for the dashboard (order matters).
GROUPS: dict[str, list[str]] = {
    "Money": ["agentic-billing", "agentic-books"],
    "Customers": ["agentic-support", "social-autopilot", "lifecycle"],
    "Security & Compliance": ["edge-sentinel", "agentic-compliance", "agentic-privacy"],
    "Growth & Intelligence": ["control-tower", "market-radar", "growth-engine",
                              "outreach-engine", "agentic-crm", "growth-assistant"],
    "Build & Platform": ["sidekick", "guide"],
}


def _group_for(name: str) -> str:
    for group, members in GROUPS.items():
        if name in members:
            return group
    return "Other"


# Cross-app missions shown on /overview — each is a REAL runnable Projects mission (a kernel
# template over live operators). ``template`` + ``goal`` deep-link into the cockpit
# (/cockpit?template=…&goal=…) so a visitor lands with the workflow pre-filled and runs it.
WORKFLOWS: list[dict] = [
    {"name": "Product launch across the fleet", "template": "product_launch",
     "goal": "Launch Context Runtime v5 across the fleet — market brief, announcement, blog, publish to social, email the list, brief support, track signups.",
     "desc": "One cross-app mission: market brief → announcement → blog → social publish (gated) → lifecycle email → support brief → BI tracking.",
     "steps": ["market-radar", "guide", "social-autopilot", "lifecycle", "agentic-support"]},
    {"name": "Deploy the stack as a governed mission", "template": "deploy_app",
     "goal": "Deploy the app to a Proxmox k3s node — supply-chain scan, terraform plan, approval gate, provision, configure, verify.",
     "desc": "Sidekick deploys through the runtime: scan → plan (the diff is evidence) → approval gate → provision → configure → verify, with saga rollback on failure.",
     "steps": ["edge-sentinel", "infra"]},
    {"name": "Revenue rescue", "template": "revenue_rescue",
     "goal": "Recover the failed payment and retain the at-risk customer.",
     "desc": "Detect the failed charge, decide the retry + outreach, and rescue the revenue — behind the money gate, with saga compensation.",
     "steps": ["finops", "agentic-support", "lifecycle"]},
    {"name": "New-customer onboarding", "template": "onboarding",
     "goal": "Onboard the new customer — set up the subscription, send welcome + onboarding, record the books entry, file the consent record.",
     "desc": "Set up the subscription, send welcome + onboarding, record the books entry, and file the consent record — the classic cross-app mission.",
     "steps": ["agentic-crm", "agentic-support", "lifecycle"]},
    {"name": "Multi-cloud deploy (SkyPilot)", "template": "sky_deploy",
     "goal": "Deploy to the cheapest available cloud/GPU — preflight the clouds, rank cost + availability, approve the chosen target, launch (failing over on capacity), and verify.",
     "desc": "SkyPilot picks the cloud/region/instance by live price + availability (the ranked table is the gate evidence), fails over on capacity, and learns which placement actually delivers — governed as a mission.",
     "steps": ["sky.check", "sky.optimize", "sky.launch", "verify"]},
]

# Static module → OSS core label (the live ✓/✕ still comes from each /health).
MODULE_CORES: dict[str, str] = {
    "agentic-billing": "Lago", "agentic-books": "ERPNext", "agentic-compliance": "OpenSCAP",
    "control-tower": "Metabase", "edge-sentinel": "CrowdSec", "market-radar": "changedetection",
    "growth-engine": "Umami", "social-autopilot": "Postiz", "agentic-support": "Chatwoot",
    "outreach-engine": "Twenty CRM",
    "guide": "redevops-rag",
    "agentic-crm": "ERPNext", "lifecycle": "Listmonk", "agentic-privacy": "ERPNext",
    "growth-assistant": "ERPNext",
}


# --- real agent-service wiring ----------------------------------------------
# Map each catalog module (modules.yaml name) to the REAL agentic-module service
# running in the integrated compose. Service names are the agent dir names
# (billing, support, …) on internal ports 8201-8209; the control plane proxies
# /m/<name> here and health-checks /health here. Modules NOT in this map have no
# real agent yet (sidekick -> tool/source-only) and keep their existing behavior.
# agentic-books -> http://books:8209 wraps the real ERPNext core.
MODULE_SERVICES: dict[str, str] = {
    "agentic-billing": "http://billing:8201",
    "control-tower": "http://control-tower:8202",
    "edge-sentinel": "http://edge-sentinel:8203",
    "market-radar": "http://market-radar:8204",
    "growth-engine": "http://growth-engine:8205",
    "social-autopilot": "http://social-autopilot:8206",
    "agentic-support": "http://support:8207",
    "agentic-compliance": "http://compliance:8208",
    "agentic-books": "http://books:8209",
    "agentic-crm": "http://agentic-crm:8210",
    "lifecycle": "http://lifecycle:8211",
    "agentic-privacy": "http://agentic-privacy:8212",
    "growth-assistant": "http://growth-assistant:8213",
    "outreach-engine": "http://outreach-engine:8214",
    "guide": "http://guide:8215",
}

# Allow overriding the whole map (or single entries) via env, e.g.
#   MODULE_SERVICE_agentic_billing=http://billing:8201
for _name in list(MODULE_SERVICES):
    _override = os.environ.get("MODULE_SERVICE_" + _name.replace("-", "_"))
    if _override:
        MODULE_SERVICES[_name] = _override.rstrip("/")

# Short service-name aliases so /m/billing and /m/agentic-billing both resolve
# (the compose service / agent-dir name is the short one: billing, support, …).
SERVICE_ALIASES: dict[str, str] = {
    url.split("//", 1)[1].split(":", 1)[0]: name
    for name, url in MODULE_SERVICES.items()
}


def _resolve_module_name(name: str) -> str:
    """Map a short service alias (billing) to its catalog name (agentic-billing)."""
    return SERVICE_ALIASES.get(name, name)


def _has_real_agent(m: Module) -> bool:
    return m.name in MODULE_SERVICES


class ModuleList(BaseModel):
    name: str
    repo: str
    pain: str
    agents: list[str]


# --- live fleet aggregation --------------------------------------------------
async def _probe_health(client: httpx.AsyncClient, m: Module) -> dict:
    """Probe a module's REAL agent service /health. Never raises.

    Returns {"health","core","connected"}: ``health`` is the agent service reachability
    ("up"/"down"); ``core`` (e.g. "lago") and ``connected`` come straight from the agent's
    own /health JSON, so a card can show "core: Lago ✓". Modules without a real agent
    (agentic-books, sidekick) report health="n/a".
    """
    base = MODULE_SERVICES.get(m.name)
    if base is None:
        # No real agent yet: books -> coming soon / on EC2; sidekick -> tool.
        coming = "coming soon · on EC2" if m.deploy == "compose" else None
        return {"health": "n/a", "core": coming, "connected": None}
    try:
        resp = await client.get(f"{base}/health", timeout=2.5)
        up = 200 <= resp.status_code < 300
        body = {}
        try:
            body = resp.json()
        except Exception:
            body = {}
        # Agents report their backend binding in one of three shapes:
        #   single-core:  {"core": "lago", "connected": true}
        #   multi (map):  {"cores": {"erpnext": true, "listmonk": true, ...}}
        #   multi (list): {"connectors_live": ["erpnext", "listmonk", ...]}
        # Normalize so the fleet/overview reports multi-connector apps honestly
        # instead of blank (agentic-privacy, growth-assistant).
        connected = body.get("connected")
        cores = body.get("cores")
        connectors = body.get("connectors_live")
        if connected is None and isinstance(cores, dict):
            connected = any(cores.values())
        if connected is None and isinstance(connectors, (list, tuple)):
            connected = len(connectors) > 0
        return {
            "health": "up" if up else "down",
            "core": body.get("core"),
            "connected": connected,
        }
    except Exception:
        return {"health": "down", "core": None, "connected": None}


@app.get("/api/fleet")
async def api_fleet() -> list[dict]:
    """Aggregate every module with a live health probe of its real agent service.

    Health checks run concurrently with a short timeout, so one slow or missing
    module can never block the response. ``core`` + ``connected`` are surfaced from
    each agent's /health so cards can show e.g. "core: Lago ✓".
    """
    mods = list(fleet.registry)
    async with httpx.AsyncClient() as client:
        probes = await asyncio.gather(*(_probe_health(client, m) for m in mods))
    return [
        {
            "name": m.name,
            "repo": m.repo,
            "pain": m.pain,
            "tagline": m.tagline,
            "agents": list(m.agents),
            "approval_required": list(m.approval_required),
            "deploy": m.deploy,
            "port": m.port,
            "health": p["health"],
            "core": p["core"],
            "connected": p["connected"],
            "has_agent": _has_real_agent(m),
            "group": _group_for(m.name),
        }
        for m, p in zip(mods, probes)
    ]


def _switcher_list() -> list[dict]:
    """Modules with a live dashboard, for the shell's jump-to-module dropdown."""
    return [
        {"name": m.name, "group": _group_for(m.name)}
        for m in fleet.registry
        if _has_real_agent(m)
    ]


def _module_meta() -> dict:
    """name -> {pain,tagline,core,repo,group} for the overview module map."""
    return {
        m.name: {
            "pain": m.pain, "tagline": m.tagline, "repo": m.repo,
            "core": MODULE_CORES.get(m.name), "group": _group_for(m.name),
        }
        for m in fleet.registry
    }


@app.get("/m/{name}/raw", response_class=HTMLResponse)
async def module_proxy_raw(name: str) -> HTMLResponse:
    """Reverse-proxy a module's REAL agent-service dashboard, same-origin on :8080.

    Proxies to the agent service's ``/`` (the live MD3 dashboard rendered from real
    OSS-core data). Agent pages are self-contained (inline CSS, no root-relative
    assets), so no URL rewriting is needed. This is the bare page; ``/m/<name>``
    wraps it in the nav shell (back / breadcrumb / switcher). Modules without a
    real agent (sidekick: deploy=tool) have no live dashboard.
    """
    name = _resolve_module_name(name)
    try:
        fleet.registry.get(name)
    except KeyError:
        raise HTTPException(404, f"no module {name}")
    base = MODULE_SERVICES.get(name)
    if base is None:
        raise HTTPException(404, f"module {name} has no live dashboard")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{base}/", timeout=8.0)
    except Exception:
        raise HTTPException(502, f"module {name} is not reachable")
    return HTMLResponse(content=resp.text, status_code=resp.status_code)


@app.api_route("/m/{name}/api/{path:path}", methods=["GET", "POST"])
async def module_api(name: str, path: str, request: Request) -> Response:
    """Forward /m/<name>/api/<path> to the module's /api/<path> (GET + POST).

    ONLY /api/* is forwarded — a module's powerful /agent/run lives OUTSIDE /api/
    and therefore stays private (never reachable through demo.redevops.io). This is
    the public surface for chat + read-only data. The real client IP is passed
    through (CF-Connecting-IP / X-Forwarded-For) so a module can rate-limit.
    """
    name = _resolve_module_name(name)
    base = MODULE_SERVICES.get(name)
    if base is None:
        raise HTTPException(404, f"no module {name}")
    # Read-only demo: the dashboards (GET) stay live, but the app's mutating surface — the
    # /api/agent chat, whose tools make real core calls — is gated off. Governed actions run in
    # the Mission cockpit. (Reads/SSE are GET and pass through.)
    if DEMO_READ_ONLY and request.method == "POST":
        return JSONResponse(status_code=403, content={
            "read_only": True,
            "message": "This is a read-only demo view. Governed actions run through the Mission cockpit.",
            "cockpit": COCKPIT_URL,
        })
    client_ip = (request.headers.get("cf-connecting-ip")
                 or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
                 or (request.client.host if request.client else ""))
    body = await request.body()
    url = f"{base}/api/{path}"
    fwd_headers = {"content-type": request.headers.get("content-type", "application/json"),
                   "x-forwarded-for": client_ip}
    # SSE endpoints (e.g. /api/stream, the live-decisions feed) are infinite text/event-stream
    # responses — a buffered request would hang until timeout, so stream them straight through.
    if request.method == "GET" and "text/event-stream" in request.headers.get("accept", ""):
        client = httpx.AsyncClient(timeout=None)
        try:
            req = client.build_request("GET", url, params=dict(request.query_params), headers=fwd_headers)
            resp = await client.send(req, stream=True)
        except Exception:
            await client.aclose()
            raise HTTPException(502, f"module {name} api unreachable")

        async def _pump():
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        return StreamingResponse(
            _pump(), status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "text/event-stream"),
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"})
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.request(
                request.method, url,
                params=dict(request.query_params), content=body,
                headers=fwd_headers, timeout=210.0)
    except Exception:
        raise HTTPException(502, f"module {name} api unreachable")
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json"))


@app.get("/m/{name}", response_class=HTMLResponse)
async def module_shell(name: str) -> HTMLResponse:
    """A module dashboard wrapped in persistent nav chrome.

    Fixes the dead-end problem: the proxied page (now at ``/m/<name>/raw``) renders
    in a same-origin iframe inside a top bar carrying back-to-OS, a breadcrumb, a
    live health dot, a jump-to-module switcher, and the source link.
    """
    name = _resolve_module_name(name)
    try:
        m = fleet.registry.get(name)
    except KeyError:
        raise HTTPException(404, f"no module {name}")
    if name not in MODULE_SERVICES:
        raise HTTPException(404, f"module {name} has no live dashboard")
    return HTMLResponse(views.module_shell(
        name=name, group=_group_for(name), repo=m.repo, switcher=_switcher_list(),
    ))


@app.get("/overview", response_class=HTMLResponse)
async def overview() -> HTMLResponse:
    """The 'how it works' page: kernel + grouped module map + cross-module workflows."""
    return HTMLResponse(views.overview_page(
        groups=GROUPS,
        has_agent=set(MODULE_SERVICES),
        module_meta=_module_meta(),
        workflows=WORKFLOWS,
    ))


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return DASHBOARD_HTML


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "modules": len(fleet.registry)}


@app.get("/modules", response_model=list[ModuleList])
def modules() -> list[ModuleList]:
    return [ModuleList(name=m.name, repo=m.repo, pain=m.pain, agents=list(m.agents))
            for m in fleet.registry]


@app.get("/status")
async def status() -> list[dict]:
    """Deployment status from a LIVE health probe of each module's agent service.

    The integrated stack brings every module up via one compose file, not the
    control-plane's own fleet.up() checkout, so the old workdir-existence heuristic
    reported everything as not-deployed. `deployed` now reflects real reachability:
    a module is deployed if its agent service answers (health "up") or it's a tool
    (health "n/a" — no service to probe).
    """
    mods = list(fleet.registry)
    async with httpx.AsyncClient() as client:
        probes = await asyncio.gather(*(_probe_health(client, m) for m in mods))
    out = []
    for m, p in zip(mods, probes):
        deployed = p["health"] in ("up", "n/a")
        out.append({
            "name": m.name,
            "deployed": deployed,
            "agents": list(m.agents),
            "detail": "context-runtime tenant" if deployed else "down",
        })
    return out


@app.post("/up", dependencies=[Depends(require_api_key)])
def up(names: list[str] | None = None) -> list[dict]:
    return [asdict(s) for s in fleet.up(*(names or []))]


@app.post("/down", dependencies=[Depends(require_api_key)])
def down(names: list[str] | None = None) -> list[dict]:
    return [asdict(s) for s in fleet.down(*(names or []))]


@app.get("/approvals")
def approvals() -> list[dict]:
    return [asdict(a) for a in fleet.context.pending()]


@app.post("/approvals/{approval_id}/{decision}", dependencies=[Depends(require_api_key)])
def resolve(approval_id: str, decision: str) -> dict:
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve|reject")
    ap = fleet.context.resolve(approval_id, approved=decision == "approve")
    if ap is None:
        raise HTTPException(404, f"no pending approval {approval_id}")
    return asdict(ap)


# ── Permissions plane (kernel) — grant admin + live preview ──────────────────
# The access-control plane's WRITE path: a client admin defines grants for their apps (subject →
# resource, row-scope + column-mask) and previews live what each subject can read. Grants persist to
# a JSONL store; a client app enforces the SAME store by installing permissions.make_authorizer over
# it. Deployable with just the core stack — no business apps required.
from . import permissions as _perm  # noqa: E402

_GRANTS = _perm.GrantStore()
_GRANTS.seed([
    {"subject_kind": "app", "subject": "support", "resource_kind": "table", "resource_name": "crm.customers",
     "actions": ["read"], "row_scope": "in", "row_column": "region", "row_values": ["us"],
     "masked_columns": ["email"], "created_by": "seed"},
    {"subject_kind": "role", "subject": "finance", "resource_kind": "table", "resource_name": "crm.customers",
     "actions": ["read"], "row_scope": "all", "masked_columns": ["notes"], "created_by": "seed"},
])


@app.get("/permissions", response_class=HTMLResponse)
def permissions_page() -> HTMLResponse:
    apps = sorted({m for members in GROUPS.values() for m in members})
    return HTMLResponse(views.permissions_page(
        subjects=apps, roles=["admin", "finance", "support", "analyst", "auditor"],
        resource=_perm.PREVIEW_RESOURCE, columns=_perm.PREVIEW_COLS,
        subject_kinds=list(_perm.SUBJECT_KINDS), resource_kinds=list(_perm.RESOURCE_KINDS),
        actions=list(_perm.ACTIONS), row_scopes=list(_perm.ROW_SCOPES),
    ))


@app.get("/api/permissions/status")
def permissions_status() -> dict:
    return _GRANTS.status()


@app.get("/api/permissions/grants")
def permissions_grants() -> list[dict]:
    return [g.to_dict() for g in _GRANTS.list()]


@app.post("/api/permissions/grants", dependencies=[Depends(require_permissions_admin)])
async def permissions_add(request: Request) -> dict:
    # Write path — gated by X-API-Key when AGENTIC_OS_API_KEY is set (the admin auth layer). This is
    # SEPARATE from at-rest protection: grants are AES-GCM encrypted + fail-closed in the GrantStore.
    b = await request.json()
    if not b.get("subject") or not b.get("resource_name"):
        raise HTTPException(400, "subject and resource_name are required")
    try:
        g = _GRANTS.add(
            b.get("subject_kind", "app"), b["subject"], b.get("resource_kind", "table"), b["resource_name"],
            actions=b.get("actions") or ["read"], row_scope=b.get("row_scope", "all"),
            row_column=b.get("row_column", "owner"), row_values=b.get("row_values") or [],
            masked_columns=b.get("masked_columns") or [], created_by="admin")
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return g.to_dict()


@app.delete("/api/permissions/grants/{gid}", dependencies=[Depends(require_permissions_admin)])
def permissions_remove(gid: str) -> dict:
    try:
        return {"removed": _GRANTS.remove(gid)}
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.post("/api/permissions/preview")
async def permissions_preview(request: Request) -> dict:
    b = await request.json()
    return _perm.preview(_GRANTS, b.get("subject_kind", "app"), b.get("subject", ""))


@app.post("/api/permissions/authorize")
async def permissions_authorize(request: Request) -> dict:
    b = await request.json()
    ident = _perm.Identity(app=b.get("app", ""), user=b.get("user", ""), roles=b.get("roles") or [])
    plane = _perm.PermissionsPlane(_GRANTS)
    return plane.authorize(ident, b.get("resource_kind", "table"), b.get("resource_name", ""),
                           b.get("action", "read")).to_dict()


class DispatchRequest(BaseModel):
    module: str
    agent: str
    action: str
    prompt: str = ""
    capability: str = "reason"
    background: bool = False


@app.post("/dispatch", dependencies=[Depends(require_api_key)])
def dispatch(req: DispatchRequest) -> dict:
    """Run one agent action through the fleet (router-picked model).

    Approval-gated actions return a pending Approval (and ping chat if a notifier
    is configured); ``background=true`` returns a job handle immediately (Hermes
    0.17 background subagents) — poll ``/jobs/<id>``."""
    try:
        out = fleet.dispatch(req.module, req.agent, req.action, req.prompt,
                             capability=req.capability, background=req.background)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))
    if hasattr(out, "id"):          # an Approval
        return {"kind": "approval", **asdict(out)}
    if isinstance(out, dict):       # a background job handle
        return {"kind": "job", **out}
    return {"kind": "result", "text": out}


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = fleet.job(job_id)
    if job is None:
        raise HTTPException(404, f"no job {job_id}")
    return {"job_id": job_id, **job}


@app.get("/notify/status")
def notify_status() -> dict:
    """What chat channels are wired + whether the inbound gateway is running."""
    n = fleet.context.notifier
    chans = [c.name for c in n.channels] if n is not None else []
    return {"channels": chans, "notifier_enabled": bool(chans),
            "gateway_running": bool(_gateway.channels)}


# --- single-page dashboard ---------------------------------------------------
DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>redevops.io — Context Runtime · Summit Roofing Co.</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500&family=Roboto+Mono:wght@400;500&display=swap">
<style>
  :root{
    --surface-dim:#0e0e11; --surface:#131316; --surface-bright:#393a3d;
    --surface-container-lowest:#0d0e10; --surface-container-low:#1b1b1f;
    --surface-container:#1f1f23; --surface-container-high:#2a2a2e; --surface-container-highest:#353539;
    --on-surface:#e4e2e6; --on-surface-variant:#c7c5ca; --on-surface-muted:#918f96;
    --outline:#938f99; --outline-variant:#2f2f33;
    --primary:#4fd1c5; --on-primary:#00201c; --primary-container:#00504a; --on-primary-container:#a8f0e6;
    --secondary:#f5b544; --on-secondary:#3d2e00; --secondary-container:#5c4500;
    --success:#5bd98a; --success-container:#0f3d22; --warning:#f5b544; --warning-container:#4a3500;
    --danger:#f2544f; --danger-container:#5c1512; --info:#5aa9f0; --info-container:#103a5c;
    --sp-1:4px;--sp-2:8px;--sp-3:12px;--sp-4:16px;--sp-5:24px;--sp-6:32px;--sp-7:40px;--sp-8:48px;
    --radius-sm:8px;--radius-md:12px;--radius-lg:16px;--radius-xl:28px;--radius-pill:999px;
    --shadow-1:0 1px 2px rgba(0,0,0,.45);--shadow-2:0 2px 6px rgba(0,0,0,.5);
    --font-sans:"Roboto",system-ui,-apple-system,"Segoe UI",sans-serif;
    --font-mono:"Roboto Mono",ui-monospace,"SF Mono",monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--surface);color:var(--on-surface);font-family:var(--font-sans);line-height:1.45;padding:var(--sp-5)}
  a{color:var(--primary);text-decoration:none}
  .shell{max-width:1440px;margin-inline:auto;display:flex;flex-direction:column;gap:var(--sp-5)}
  .grid{display:grid;gap:var(--sp-4)}
  .pill{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 10px;border-radius:var(--radius-pill);font:500 12px/1 var(--font-sans)}
  .pill--success{background:var(--success-container);color:var(--success)}
  .pill--warn{background:var(--warning-container);color:var(--warning)}
  .pill--danger{background:var(--danger-container);color:var(--danger)}
  .pill--neutral{background:var(--surface-container-highest);color:var(--on-surface-variant)}
  .pill__dot{width:6px;height:6px;border-radius:50%;background:currentColor}

  /* compact app bar */
  .appbar{background:var(--surface-container-low);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-5)}
  .appbar__row{display:flex;align-items:center;justify-content:space-between;gap:var(--sp-4);flex-wrap:wrap}
  .appbar h1{margin:0;font:400 24px/32px var(--font-sans);color:var(--on-surface)}
  .appbar h1 .accent{color:var(--primary)}
  .appbar__sub{margin-top:var(--sp-1);color:var(--on-surface-muted);font:400 14px/20px var(--font-sans)}
  .appbar__tenant{margin-top:var(--sp-2);color:var(--on-surface-variant);font:400 13px/18px var(--font-sans)}
  .appbar__tenant b{color:var(--on-surface)}
  .fleet-pill{font:500 13px/1 var(--font-mono);font-feature-settings:"tnum"}

  /* slim two-card band */
  .band{display:grid;gap:var(--sp-4);grid-template-columns:1fr 1fr}
  @media(max-width:839px){.band{grid-template-columns:1fr}}
  .card{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-5);display:flex;flex-direction:column;gap:var(--sp-3)}
  .card__title{margin:0;font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;color:var(--on-surface-muted)}
  .card ul{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:var(--sp-3)}
  .card li{font:400 14px/20px var(--font-sans);color:var(--on-surface)}
  .meta{color:var(--on-surface-muted);font:400 12px/16px var(--font-mono)}
  .wf-name{color:var(--primary);font:500 14px/20px var(--font-sans)}
  .wf-steps{color:var(--on-surface-muted);font:400 12px/16px var(--font-sans)}
  .empty{color:var(--on-surface-muted);font:400 14px/20px var(--font-sans)}

  /* functional groups */
  .group{display:flex;flex-direction:column;gap:var(--sp-4)}
  .group-label{font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;color:var(--primary);display:flex;align-items:center;gap:var(--sp-3);margin:0}
  .group-label::after{content:"";flex:1;height:1px;background:var(--outline-variant)}
  /* Cap card width so 2-item groups don't stretch to full width (which made the card
     thumbnails oversized); cards pack left at ~half the old width → images ~half size. */
  .module-grid{display:grid;gap:var(--sp-4);grid-template-columns:repeat(auto-fill,minmax(300px,360px));justify-content:start;align-items:stretch}

  /* equal-height module cards */
  .mcard{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);
    display:flex;flex-direction:column;overflow:hidden;transition:border-color .15s,background .15s}
  .mcard:hover{border-color:var(--primary);background:var(--surface-container-high)}
  .mcard .thumb{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;background:var(--surface-container-high);border-bottom:1px solid var(--outline-variant)}
  .mcard .body{padding:var(--sp-4) var(--sp-5);display:flex;flex-direction:column;gap:var(--sp-3);flex:1}
  .mcard .top{display:flex;align-items:center;justify-content:space-between;gap:var(--sp-3)}
  .mcard .name{font:500 16px/24px var(--font-sans);letter-spacing:.15px;color:var(--on-surface)}
  .mcard .pain{align-self:flex-start;font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;
    color:var(--primary);background:var(--primary-container);color:var(--on-primary-container);padding:2px 10px;border-radius:var(--radius-pill)}
  .mcard .tagline{color:var(--on-surface-variant);font:400 14px/20px var(--font-sans)}
  .chips{display:flex;flex-wrap:wrap;gap:var(--sp-2)}
  .chip{font:400 12px/16px var(--font-sans);color:var(--on-surface-variant);background:var(--surface-container-high);
    border:1px solid var(--outline-variant);padding:2px 8px;border-radius:var(--radius-sm)}
  .approval-note{align-self:flex-start;font:400 12px/16px var(--font-sans);color:var(--warning);
    background:var(--warning-container);border-radius:var(--radius-sm);padding:4px 10px}
  .status{display:flex;align-items:center;gap:var(--sp-2);font:400 13px/18px var(--font-sans);color:var(--on-surface-muted)}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--on-surface-muted);flex:none}
  .dot.up{background:var(--success);box-shadow:0 0 8px rgba(91,217,138,.7)}
  .dot.down{background:var(--danger)}
  .dot.na{background:var(--on-surface-muted)}
  .mcard .foot{margin-top:auto;padding:var(--sp-4) var(--sp-5);border-top:1px solid var(--outline-variant);
    display:flex;align-items:center;gap:var(--sp-4)}
  .btn-open{font:500 14px/1 var(--font-sans);color:var(--on-primary);background:var(--primary);
    padding:9px 16px;border-radius:var(--radius-pill)}
  .btn-open:hover{background:var(--primary-container);color:var(--on-primary-container)}
  .src{font:400 13px/18px var(--font-sans);color:var(--on-surface-muted)}
  .src:hover{color:var(--primary)}
  .tool-note{font:400 13px/18px var(--font-sans);color:var(--secondary)}
  .demo-note{display:flex;align-items:center;gap:var(--sp-3);background:var(--info-container);
    color:var(--info);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);
    padding:10px var(--sp-5);font:400 13px/18px var(--font-sans)}
  .demo-note b{color:var(--on-surface)} .demo-note a{color:inherit;text-decoration:underline}
  .demo-note button{margin-left:auto;background:none;border:1px solid currentColor;color:inherit;
    border-radius:var(--radius-pill);padding:2px 10px;font:500 12px/1 var(--font-sans);cursor:pointer}
  .card__link{margin-top:auto;font:500 13px/18px var(--font-sans);color:var(--primary)}
  footer{color:var(--on-surface-muted);font:400 12px/16px var(--font-sans);text-align:center;padding-top:var(--sp-2)}
  footer code{font-family:var(--font-mono)}
</style>
</head>
<body>
<div class="shell">
  <header class="appbar">
    <div class="appbar__row">
      <div>
        <h1><span class="accent">redevops.io</span> — Context Runtime <span style="display:inline-block;vertical-align:middle;margin-left:10px;font:600 11px/1 var(--font-sans);letter-spacing:.8px;text-transform:uppercase;color:var(--secondary);background:var(--secondary-container);border:1px solid var(--secondary);border-radius:var(--radius-pill);padding:5px 11px">Under development</span></h1>
        <div class="appbar__sub">The query planner for AI — <b>Context Runtime</b> decides what each agent sees before it acts. Here it runs billing, support, security &amp; growth on proven open-source cores, on hardware you own.</div>
        <div class="appbar__tenant">Demo tenant: <b>Summit Roofing Co.</b> — a fictional roofing contractor running entirely on agents. Everything below is live on demo data.</div>
      </div>
      <div style="display:flex;align-items:center;gap:var(--sp-3);flex-wrap:wrap">
        <a href="https://github.com/redevops-io/context-runtime" target="_blank" rel="noopener" style="font:500 14px/1 var(--font-sans);color:var(--on-primary);background:var(--primary);padding:9px 16px;border-radius:var(--radius-pill)">Get started &#8599;</a>
        <a href="https://chat.redevops.io" target="_blank" rel="noopener" style="font:500 14px/1 var(--font-sans);color:var(--primary);background:var(--surface-container-high);border:1px solid var(--outline-variant);padding:9px 16px;border-radius:var(--radius-pill)">Chat demo &#8599;</a>
        <a href="/overview" style="font:500 14px/1 var(--font-sans);color:var(--primary);background:var(--surface-container-high);border:1px solid var(--outline-variant);padding:9px 16px;border-radius:var(--radius-pill)">How it works</a>
        <span class="pill pill--success fleet-pill" id="summary"><span class="pill__dot"></span>loading…</span>
      </div>
    </div>
  </header>

  <div class="demo-note" id="demoNote">
    <span>Live demo on fictional <b>Summit Roofing Co.</b> data — real open-source cores, simulated business. Actions that move money or change infrastructure are gated and safe to explore.</span>
    <button onclick="document.getElementById('demoNote').remove()">dismiss</button>
  </div>

  <section class="band">
    <div class="card">
      <h2 class="card__title">Approvals — your one-click sign-offs</h2>
      <ul id="approvals"><li class="empty">checking…</li></ul>
      <a class="card__link" href="/overview#approvals">How approvals work &#8594;</a>
    </div>
    <div class="card">
      <h2 class="card__title">Cross-module workflows</h2>
      <ul id="workflows"></ul>
    </div>
  </section>

  <!-- align-self:stretch → #groups fills the shell width; without it, this flex item shrinks to
       the grid's intrinsic width (one capped track), collapsing the module grid to a lone column. -->
  <div id="groups" class="shell" style="gap:var(--sp-6);align-self:stretch;width:100%"></div>

  <footer>redevops.io — Context Runtime · self-hosted &amp; open-core · <a href="https://github.com/redevops-io/context-runtime" target="_blank" rel="noopener">source &#8599;</a></footer>
</div>

<script>
const WORKFLOWS = [
  { name: "New customer onboarding",
    steps: ["agentic-billing", "agentic-support", "agentic-books", "agentic-compliance"] },
  { name: "Storm-damage lead → booked job",
    steps: ["market-radar", "growth-engine", "agentic-support", "agentic-billing"] },
  { name: "Security incident",
    steps: ["edge-sentinel", "agentic-compliance"] },
];

// Functional groups, in display order (mirrors GROUPS in control_plane.py).
const GROUP_ORDER = [
  "Money",
  "Customers",
  "Security & Compliance",
  "Growth & Intelligence",
  "Build & Platform",
];

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function renderWorkflows() {
  const ul = document.getElementById("workflows");
  ul.innerHTML = "";
  WORKFLOWS.forEach(w => {
    const li = el("li");
    li.appendChild(el("span", "wf-name", w.name));
    li.appendChild(document.createElement("br"));
    li.appendChild(el("span", "wf-steps", w.steps.join(" → ")));
    ul.appendChild(li);
  });
}

function dotClass(health) {
  if (health === "up") return "dot up";
  if (health === "down") return "dot down";
  return "dot na";
}

function coreLabel(core) {
  // Pretty-print the OSS core name surfaced by each agent's /health.
  const map = {
    lago: "Lago", metabase: "Metabase", crowdsec: "CrowdSec",
    changedetection: "changedetection", umami: "Umami", postiz: "Postiz",
    chatwoot: "Chatwoot", openscap: "OpenSCAP", oscap: "OpenSCAP",
  };
  if (!core) return null;
  return map[String(core).toLowerCase()] || core;
}

function makeCard(m) {
  // Three kinds: real agent (has_agent), coming-soon (deploy=compose, no agent),
  // and tool (deploy=tool, e.g. sidekick).
  const hasAgent = !!m.has_agent;
  const isTool = m.deploy !== "compose";
  const card = el("div", "mcard");

  // Thumbnail (16:9 cover; links to live dashboard, or GitHub when there's none).
  const img = el("img", "thumb");
  img.src = "/assets/repos/" + m.name + ".png";
  img.alt = m.name;
  img.loading = "lazy";
  const imgLink = el("a");
  imgLink.href = hasAgent ? ("/m/" + m.name) : ("https://github.com/" + m.repo);
  if (!hasAgent) { imgLink.target = "_blank"; imgLink.rel = "noopener"; }
  imgLink.appendChild(img);
  card.appendChild(imgLink);

  const bodyEl = el("div", "body");
  const top = el("div", "top");
  top.appendChild(el("span", "name", m.name));
  const status = el("span", "status");
  status.appendChild(el("span", dotClass(hasAgent ? m.health : "na")));
  status.appendChild(el("span", null, isTool ? "tool" : (hasAgent ? m.health : "soon")));
  top.appendChild(status);
  bodyEl.appendChild(top);

  bodyEl.appendChild(el("span", "pain", m.pain));
  if (m.tagline) bodyEl.appendChild(el("div", "tagline", m.tagline));

  // Real OSS core badge: "core: Lago ✓" (connected) / "✕" (unreachable).
  if (hasAgent) {
    const core = coreLabel(m.core);
    if (core) {
      const cls = m.connected ? "pill pill--success" : "pill pill--danger";
      const mark = m.connected ? " ✓" : " ✕";
      const cp = el("span", cls);
      cp.appendChild(el("span", "pill__dot"));
      cp.appendChild(el("span", null, "core: " + core + mark));
      bodyEl.appendChild(cp);
    }
  } else if (m.deploy === "compose" && m.core) {
    const cp = el("span", "pill pill--neutral");
    cp.appendChild(el("span", "pill__dot"));
    cp.appendChild(el("span", null, m.core));
    bodyEl.appendChild(cp);
  }

  if (m.agents && m.agents.length) {
    const chips = el("div", "chips");
    m.agents.forEach(a => chips.appendChild(el("span", "chip", a)));
    bodyEl.appendChild(chips);
  }
  if (m.approval_required && m.approval_required.length) {
    bodyEl.appendChild(el("div", "approval-note", "approval-gated: " + m.approval_required.join(", ")));
  }
  card.appendChild(bodyEl);

  // Footer: primary filled-teal "Open dashboard" button; secondary "source" link.
  const foot = el("div", "foot");
  if (hasAgent) {
    const open = el("a", "btn-open", "Open dashboard");
    open.href = "/m/" + m.name;
    foot.appendChild(open);
  } else {
    foot.appendChild(el("span", "tool-note", isTool ? "CLI tool — no dashboard by design; see source" : "coming soon — on EC2"));
  }
  const src = el("a", "src", "source ↗");
  src.href = "https://github.com/" + m.repo;
  src.target = "_blank"; src.rel = "noopener";
  foot.appendChild(src);
  card.appendChild(foot);
  return card;
}

function renderFleet(mods) {
  const root = document.getElementById("groups");
  root.innerHTML = "";
  let up = 0, total = 0;

  const byGroup = {};
  mods.forEach(m => {
    if (m.has_agent) {
      total++;
      if (m.health === "up") up++;
    }
    const g = m.group || "Other";
    (byGroup[g] = byGroup[g] || []).push(m);
  });

  const order = GROUP_ORDER.slice();
  Object.keys(byGroup).forEach(g => { if (!order.includes(g)) order.push(g); });

  order.forEach(g => {
    const members = byGroup[g];
    if (!members || !members.length) return;
    const section = el("section", "group");
    section.appendChild(el("div", "group-label", g));
    const grid = el("div", "module-grid");
    members.forEach(m => grid.appendChild(makeCard(m)));
    section.appendChild(grid);
    root.appendChild(section);
  });

  const summary = document.getElementById("summary");
  summary.innerHTML = "";
  summary.appendChild(el("span", "pill__dot"));
  summary.appendChild(el("span", null, up + "/" + total + " modules up"));
}

async function pollFleet() {
  try {
    const r = await fetch("/api/fleet");
    if (r.ok) renderFleet(await r.json());
  } catch (e) { /* keep last render */ }
}

async function pollApprovals() {
  const ul = document.getElementById("approvals");
  try {
    const r = await fetch("/approvals");
    if (!r.ok) throw new Error("bad status");
    const list = await r.json();
    ul.innerHTML = "";
    if (!list.length) {
      ul.appendChild(el("li", "empty", "Nothing needs your sign-off right now — money, compliance, and infra actions pause here."));
      return;
    }
    list.forEach(a => {
      const li = el("li");
      li.appendChild(el("span", null, (a.module || "?") + ": " + (a.summary || a.action || "pending")));
      if (a.id) {
        li.appendChild(document.createElement("br"));
        li.appendChild(el("span", "meta", "id " + a.id));
      }
      // Hermes 0.17 safety scan: warn the approver before they click approve.
      if (a.findings && a.findings.length) {
        li.appendChild(document.createElement("br"));
        li.appendChild(el("span", "approval-note", "⚠ safety: " + a.findings.join("; ")));
      }
      ul.appendChild(li);
    });
  } catch (e) {
    ul.innerHTML = "";
    ul.appendChild(el("li", "empty", "nothing waiting on you"));
  }
}

renderWorkflows();
pollFleet();
pollApprovals();
setInterval(pollFleet, 5000);
setInterval(pollApprovals, 5000);
</script>
</body>
</html>
"""

"""Evidence Explorer core: the synthetic corpus, the scoped catalog and the HTTP surface shared by BOTH deployment profiles.

Profiles (separate entrypoints, never an env switch on one another):
* ``app.py``        - AUTHENTICATED profile. Fails closed: refuses to start without an owner token. Every query is
                      authorized through the permission plane and audited.
* ``public_app.py`` - PUBLIC SYNTHETIC profile. Anonymous read-only SELECT over the one built-in synthetic scope only, in the
                      sandboxed worker, rate limited. A different image target; it has no token and no way to name another scope.
Neither profile has a replay, resume, approval or operational route (the routes do not exist).

(Original description follows.)

Evidence Explorer — demo.redevops.io/evidence.

Shows the **Historical Evidence Plane** end to end: a real benchmark run is persisted through the open
``runtime_contracts.store.EvidenceStore`` seam (the AGPL file-level backend), then the *saved* canonical
evidence is queried with SQL.

The corpus is not mocked. Each scenario is driven through the **real** runtime plane
(``agentic_os.mission.security_monitor.SecurityMonitor`` + ``Executor`` + ``MissionTrace``) — the same
code path a governed mission uses. Every boundary ``RuntimeSecurityEvent``, every per-call
``SecurityDecision``, the correlated ``GovernanceDisposition`` and the Mission trace spans are projected
onto ``EvidenceEnvelope`` rows and appended to a ``FileEvidenceStore``. Then the JSONL that backend wrote
is loaded into DuckDB so it can be queried — demonstrating the pluggable-backend point: the runtime
persists through the open seam, and any SQL engine can read what it wrote.

Self-contained: one FastAPI app, an HTML page, and two JSON endpoints. No network, no secrets.
"""

import hashlib
import json
import os
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from runtime_contracts.protocol.security import SecurityDecision, SecurityVerdict
from runtime_contracts.store import EvidenceEnvelope, FileEvidenceStore, project

# --- capability surface (declared by the registry, not the agent) -------------------------------------
_D = lambda dc=(), ra=(), net=(): SimpleNamespace(data_classifications=dc, required_authority=ra, network=net)
DESCS = {
    "crm.read":      _D(("pii",), ("read:crm",)),
    "billing.report": _D((), ("read:billing",)),
    "db.query":      _D(("pii",), ("read:db",)),
    "vault.read":    _D(("secrets",), ("read:secrets",)),
    "model.infer":   _D((), ("infer:model",)),
    "storage.upload": _D((), ("write:storage",), ("s3.external.com",)),
    "report.export": _D((), ("export:report",), ("reports.public.com",)),
    "email.send":    _D((), ("send:email",), ("smtp.external.com",)),
}
# bulk-read capabilities return a records count -> a `records_read=` boundary side-effect; sensitive data
# + external egress + bulk read is the exfiltration shape the governance correlation denies.
RECORDS = {"crm.read": 2000, "db.query": 1500}
_HANDLERS = {c: (lambda i, n=RECORDS.get(c, 0): ({"records": n} if n else {})) for c in DESCS}

# --- scenario library: (name, tenant, leased authority, call sequence, planned capabilities) -----------
# planned != observed => plan-vs-observed divergence (REQUIRE_REVIEW); sensitive+egress+bulk => DENY.
SCENARIOS = [
    ("acme benign reads",       "acme",    {"read:crm", "read:billing"},            ["crm.read", "billing.report"],       ["crm.read", "billing.report"]),
    ("acme exfil shape",        "acme",    {"read:crm"},                            ["crm.read", "storage.upload"],       ["crm.read", "storage.upload"]),
    ("globex off-plan export",  "globex",  {"infer:model"},                         ["model.infer", "report.export"],     ["model.infer"]),
    ("globex benign infer",     "globex",  {"infer:model"},                         ["model.infer"],                      ["model.infer"]),
    ("initech email exfil",     "initech", {"read:db"},                             ["db.query", "email.send"],           ["db.query", "email.send"]),
    ("initech vault read",      "initech", {"read:secrets"},                        ["vault.read"],                       ["vault.read"]),
    ("acme multi read",         "acme",    {"read:crm", "read:billing", "read:db"}, ["crm.read", "db.query", "billing.report"], ["crm.read", "db.query", "billing.report"]),
    ("globex full exfil chain", "globex",  {"read:crm"},                            ["crm.read", "db.query", "storage.upload"], ["crm.read", "db.query", "storage.upload"]),
]

STORE_PATH = os.environ.get("EVIDENCE_PATH", "/tmp/evidence-explorer/evidence.jsonl")


def _seq_clock():
    n = {"t": 0}
    def tick() -> str:
        n["t"] += 1
        return f"2026-08-01T00:{n['t'] // 60:02d}:{n['t'] % 60:02d}+00:00"
    return tick


def build_corpus(store_path: Optional[str] = None) -> FileEvidenceStore:
    """Run every scenario through the real SecurityMonitor and persist the canonical evidence."""
    store_path = store_path or STORE_PATH
    from agentic_os.mission.executor import Executor, InMemoryOperatorClient  # noqa: PLC0415
    from agentic_os.mission.security_monitor import SecurityMonitor  # noqa: PLC0415
    from agentic_os.mission.tracing import MissionTrace  # noqa: PLC0415
    from agentic_os.mission.types import Node  # noqa: PLC0415

    if os.path.exists(store_path):
        os.remove(store_path)
    store = FileEvidenceStore(store_path, clock=_seq_clock())

    for i, (name, tenant, leased, sequence, planned) in enumerate(SCENARIOS):
        mid = f"m-{tenant}-{i:02d}"
        date = f"2026-0{(i % 6) + 1}-15T09:00:00Z"
        mon = SecurityMonitor(mission_id=mid, planned_capabilities=tuple(planned), descriptor_for=DESCS.get)
        ex = Executor(InMemoryOperatorClient(_HANDLERS), monitor=mon)
        for cap in sequence:
            ex.run(Node(capability=cap, operator="op"), {})
            desc = DESCS[cap]
            required = set(desc.required_authority or ())
            covered = required.issubset(leased)
            decision = SecurityDecision(
                verdict=SecurityVerdict.ALLOW if covered else SecurityVerdict.DENY,
                subject=tenant, resource=cap,
                reason=("covered by the leased authority" if covered
                        else f"requires {sorted(required)} beyond the leased chain {sorted(leased)}"),
                evidence=tuple(sorted(required)),
                decided_by="AuthorityContext",
            )
            env = project(decision, tenant_id=tenant, contract_version="rcv1")
            store.append(replace(env, mission_id=mid, event_time=date))

        # boundary telemetry — the canonical RuntimeSecurityEvent stream
        for ev in mon.trajectory.events:
            env = project(ev, tenant_id=tenant, contract_version="rcv1")
            store.append(replace(env, mission_id=mid, event_time=date))

        # correlated disposition + containment for the whole series
        disposition, reasons, containment = mon.enforce()
        store.append(EvidenceEnvelope(
            event_id="", event_type="governance_disposition", family="governance_dispositions",
            tenant_id=tenant, mission_id=mid, contract_version="rcv1", event_time=date,
            payload={"scenario": name, "disposition": disposition.value, "containment": containment.value,
                     "reasons": [str(r) for r in reasons], "mission_id": mid, "tenant": tenant},
        ))

        # Mission-native trace spans
        for sp in MissionTrace(mid).spans(mon.trajectory):
            store.append(EvidenceEnvelope(
                event_id=sp.get("span_id", ""), event_type="trace_span", family="trace_spans",
                tenant_id=tenant, mission_id=mid, trace_id=sp.get("trace_id", ""),
                parent_event_id=sp.get("parent_span_id", ""), contract_version="rcv1", event_time=date,
                payload=sp,
            ))
    return store


def _row(env: EvidenceEnvelope) -> Dict[str, Any]:
    """Flatten one envelope into a queryable DuckDB row (identity columns + a few lifted payload fields)."""
    p = env.payload or {}
    cap = p.get("capability") or p.get("resource") or (p.get("name") if env.family == "trace_spans" else "")
    verdict = p.get("verdict") or p.get("disposition") or p.get("decision") or ""
    net = p.get("network") or (p.get("attributes", {}) or {}).get("redevops.network") or []
    dcl = p.get("data_classifications") or (p.get("attributes", {}) or {}).get("redevops.data_classifications") or []
    return {
        "family": env.family, "event_type": env.event_type, "tenant_id": env.tenant_id,
        "mission_id": env.mission_id, "trace_id": env.trace_id, "event_id": env.event_id,
        "parent_event_id": env.parent_event_id, "capability": cap, "verdict": str(verdict),
        "network": ",".join(net) if isinstance(net, (list, tuple)) else str(net),
        "data_classifications": ",".join(dcl) if isinstance(dcl, (list, tuple)) else str(dcl),
        "event_time": env.event_time, "known_at": env.known_at, "content_hash": env.content_hash,
    }


# --- canned queries the buttons run --------------------------------------------------------------------
CANNED = {
    "Lifecycle by family": "SELECT family, count(*) AS rows FROM evidence GROUP BY family ORDER BY rows DESC",
    "Every DENY, by capability": (
        "SELECT capability, tenant_id, count(*) AS denies FROM evidence "
        "WHERE event_type='security_decision' AND verdict='DENY' GROUP BY capability, tenant_id ORDER BY denies DESC"),
    "Governed dispositions": (
        "SELECT tenant_id, mission_id, verdict AS disposition FROM evidence "
        "WHERE family='governance_dispositions' ORDER BY disposition DESC"),
    "Exfiltration shape (PII read, then external egress)": (
        "SELECT mission_id, capability, data_classifications, network FROM evidence "
        "WHERE family='security_events' AND (data_classifications LIKE '%pii%' OR network <> '') ORDER BY mission_id"),
    "Reconstruct a mission trace": (
        "SELECT mission_id, capability AS span, event_id, parent_event_id FROM evidence "
        "WHERE family='trace_spans' AND mission_id='m-acme-01' ORDER BY event_id"),
    "Has this capability ever been denied?": (
        "SELECT capability, count(*) FILTER (WHERE verdict='DENY') AS denied, "
        "count(*) FILTER (WHERE verdict='ALLOW') AS allowed FROM evidence "
        "WHERE event_type='security_decision' GROUP BY capability ORDER BY denied DESC"),
}

PAGE_TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Evidence Explorer · Agentic OS</title>
<style>
:root{--bg:#0f0f12;--card:#1f1f23;--line:#2f2f33;--fg:#e7e5ea;--muted:#9b99a1;--teal:#4fd1c5;--amber:#f5b544;--red:#f56565}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,sans-serif;line-height:1.55}
.wrap{max-width:1000px;margin:0 auto;padding:40px 24px 80px}
a{color:var(--teal)}h1{font-size:28px;margin:0 0 6px}.sub{color:var(--muted);margin:0 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 22px;margin:16px 0}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
.chip{background:#17171a;border:1px solid var(--line);color:var(--teal);border-radius:999px;padding:5px 12px;font-size:13px;cursor:pointer}
.chip:hover{border-color:var(--teal)}
.fam{display:inline-block;font-size:12px;color:var(--muted);margin-right:14px}.fam b{color:var(--fg)}
textarea{width:100%;min-height:70px;background:#141417;color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:10px;font-family:ui-monospace,monospace;font-size:13px}
button{background:var(--teal);color:#0f0f12;border:0;border-radius:8px;padding:9px 16px;font-weight:700;cursor:pointer}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:10px}th,td{border:1px solid var(--line);padding:7px 10px;text-align:left;white-space:nowrap}
thead th{color:var(--teal);background:#17171a}.mono{font-family:ui-monospace,monospace;font-size:12px;color:var(--muted)}
.scroll{overflow-x:auto}.err{color:var(--red)}code{color:var(--amber)}
</style></head><body><div class="wrap">
<h1>Evidence Explorer</h1>
__BANNER__
<p class="sub">Query the canonical runtime evidence a benchmark run <em>saved</em> — persisted through the open
<code>EvidenceStore</code> seam, read back with SQL.</p>
<div class="card"><p>A set of scenarios was driven through the <strong>real</strong> runtime security monitor; every
boundary <code>RuntimeSecurityEvent</code>, per-call <code>SecurityDecision</code>, correlated
<code>GovernanceDisposition</code> and Mission trace span was projected onto an <code>EvidenceEnvelope</code> and
appended to a <strong>file</strong> backend (<span class="mono">runtime_contracts.store.FileEvidenceStore</span>) — the
open, dependency-free floor. An enterprise deployment swaps that backend for a managed database or a lakehouse
behind the same interface. <span id="meta" class="mono"></span></p>
<div id="fams"></div></div>
<div class="card" id="tokrow" style="display:__TOKDISPLAY__"><label for="tok" class="mono">Access token (kept in this tab only)</label>
<input id="tok" type="password" autocomplete="off" style="width:100%;background:#141417;color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:8px"></div>
<div class="card"><div class="chips" id="canned"></div>
<textarea id="sql">SELECT family, count(*) AS rows FROM evidence GROUP BY family ORDER BY rows DESC</textarea>
<div style="margin-top:10px"><button onclick="run()">Run query</button> <span id="status" class="mono"></span></div>
<div class="scroll"><div id="out"></div></div></div>
<script>
function esc(v){return String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function hdrs(extra){const h=Object.assign({}, extra||{}); const t=(document.getElementById('tok')||{}).value||sessionStorage.getItem('evtok'); if(t){h['Authorization']='Bearer '+t; sessionStorage.setItem('evtok', t);} return h;}
async function meta(){
  const d = await (await fetch('/api/evidence/summary')).json();
  document.getElementById('meta').textContent = `backend=${d.backend} · ${d.rows} rows saved`;
  document.getElementById('fams').innerHTML = d.families.map(f=>`<span class="fam"><b>${esc(f.rows)}</b> ${esc(f.family)}</span>`).join('');
  document.getElementById('canned').innerHTML = d.canned.map(n=>`<span class="chip" data-n="${esc(n)}" onclick="canned(this.dataset.n)">${esc(n)}</span>`).join('');
}
function render(d){
  if(d.error){document.getElementById('out').innerHTML = `<p class="err">${esc(d.error)}</p>`; return;}
  const th = d.columns.map(c=>`<th>${esc(c)}</th>`).join('');
  const tr = d.rows.map(r=>`<tr>${r.map(c=>`<td>${c===null?'—':esc(c)}</td>`).join('')}</tr>`).join('');
  document.getElementById('out').innerHTML = `<table><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table>`;
  document.getElementById('status').textContent = `${d.rows.length} row(s)`;
}
async function run(){
  document.getElementById('status').textContent = 'running…';
  const sql = document.getElementById('sql').value;
  render(await (await fetch('/api/evidence/query',{method:'POST',headers:hdrs({'content-type':'application/json'}),body:JSON.stringify({sql})})).json());
}
async function canned(name){
  document.getElementById('status').textContent = 'running…';
  const d = await (await fetch('/api/evidence/canned?name='+encodeURIComponent(name),{headers:hdrs()})).json();
  if(d.sql) document.getElementById('sql').value = d.sql;
  render(d);
}
meta(); run();
</script></div></body></html>"""



# ═══════════════════════════ scoped catalog + HTTP surface (shared by both profiles) ═══════════════════════════
COLUMNS = tuple((c, "VARCHAR") for c in (
    "family", "event_type", "tenant_id", "mission_id", "trace_id", "event_id", "parent_event_id", "capability", "verdict",
    "network", "data_classifications", "event_time", "known_at", "content_hash"))
TENANTS = tuple(sorted({sc[1] for sc in SCENARIOS}))
SYNTHETIC_SCOPE = "synthetic"                       # the one scope the public profile can reach


def _parse_envelopes(data: bytes) -> list:
    """Rows from the file backend's JSONL; every envelope's payload hash is verified (evidence integrity stays checkable)."""
    from agentic_os.evidence_query import ArtifactError  # noqa: PLC0415
    rows = []
    for line in data.decode("utf-8").splitlines():
        if not line.strip():
            continue
        env = EvidenceEnvelope.from_json(json.loads(line))
        if not env.verify_payload():
            raise ArtifactError("an evidence envelope failed its payload hash check")
        rows.append(_row(env))
    return rows


def build_catalog(store_path: str, *, tenant_scopes: bool = True):
    """Register the synthetic corpus: ``synthetic`` (all rows) and, optionally, one scope per synthetic tenant. The dataset
    is the JSONL the file backend wrote, read through a confined root and checked against the sha256 recorded here."""
    from agentic_os.evidence_query import ArtifactFile, ArtifactRoot, DatasetSpec, ScopeCatalog, ViewSpec  # noqa: PLC0415
    root = ArtifactRoot(os.path.dirname(os.path.abspath(store_path)))
    rel = os.path.basename(store_path)
    with open(store_path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    art = ArtifactFile(root, rel, digest, parse=_parse_envelopes)
    cat = ScopeCatalog()
    cat.register_scope(SYNTHETIC_SCOPE, [DatasetSpec("evidence", COLUMNS, artifact=art)])
    if tenant_scopes:
        for t in TENANTS:
            tart = ArtifactFile(root, rel, digest, parse=(lambda b, _t=t: [r for r in _parse_envelopes(b) if r["tenant_id"] == _t]))
            cat.register_scope(f"{SYNTHETIC_SCOPE}-{t}", [DatasetSpec("evidence", COLUMNS, artifact=tart)])
    return cat


def banner(profile: str) -> str:
    if profile == "public-synthetic":
        return ('<p class="sub" role="note" style="border:1px solid var(--amber);padding:8px 12px;border-radius:8px">'
                '<strong>Public synthetic demo.</strong> Anonymous, read-only SQL over a generated synthetic corpus only, '
                'executed in a sandboxed worker with time, memory and row limits and rate limiting. No real data.</p>')
    return ('<p class="sub" role="note">Authenticated profile: queries need the owner token and are audited.</p>')


def render_page(profile: str) -> str:
    return (PAGE_TEMPLATE.replace("__BANNER__", banner(profile))
            .replace("__TOKDISPLAY__", "none" if profile == "public-synthetic" else "block"))


def make_app(*, profile: str, service, store_path: str, authenticator=None, limiter=None, audit_path: str = ""):
    """Build the FastAPI app for ``profile`` (``authenticated`` | ``public-synthetic``). The route table is explicit and tiny;
    there is no replay/resume/approval/operational route in either profile."""
    from fastapi import FastAPI, Request  # noqa: PLC0415
    from fastapi.responses import HTMLResponse, JSONResponse  # noqa: PLC0415
    from pydantic import BaseModel  # noqa: PLC0415
    from agentic_os.evidence_query import QueryRefused  # noqa: PLC0415
    from agentic_os.overlays import Principal  # noqa: PLC0415

    public = profile == "public-synthetic"
    if not public and authenticator is None:
        raise RuntimeError("the authenticated profile requires an authenticator")
    anon = Principal("public-demo", "user", ())
    catalog = service.catalog

    class Q(BaseModel):
        sql: str
        scope: str = SYNTHETIC_SCOPE

    app = FastAPI(title="ReDevOps Evidence Explorer", docs_url=None, redoc_url=None, openapi_url=None)
    sec = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
           "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"}

    @app.middleware("http")
    async def _headers(request: Request, call_next):
        resp = await call_next(request)
        for k, v in sec.items():
            resp.headers.setdefault(k, v)
        return resp

    def _principal(request: Request):
        if public:
            return anon
        h = request.headers.get("authorization", "")
        tok = h[7:].strip() if h.lower().startswith("bearer ") else ""
        return authenticator.authenticate(tok)

    def _client(request: Request) -> str:
        return request.client.host if request.client else "?"

    def _run(request: Request, sql: str, scope: str):
        p = _principal(request)
        if p is None:
            return JSONResponse({"error": "authentication required"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        if public:
            scope = SYNTHETIC_SCOPE                       # the public profile cannot name any other scope
            if limiter is not None and not limiter.allow(_client(request)):
                return JSONResponse({"error": "too many requests"}, status_code=429, headers={"Retry-After": "60"})
        try:
            r = service.query(p, scope, sql)
        except QueryRefused as e:
            status = {"unauthenticated": 401, "not_found": 404, "statement_rejected": 400, "timeout": 408, "limit": 413,
                      "integrity": 500, "audit_unavailable": 503}.get(e.code, 400)
            return JSONResponse({"error": str(e), "code": e.code}, status_code=status)
        return {"columns": r.columns, "rows": r.rows, "truncated": r.truncated, "receipt": r.receipt_id}

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "profile": profile}

    @app.get("/api/evidence/summary")
    def summary():
        """Aggregate counts of the synthetic corpus (no paths, no host detail). Computed once at start-up."""
        return SUMMARY(profile)

    @app.post("/api/evidence/query")
    def query(q: Q, request: Request):
        return _run(request, q.sql, q.scope)

    @app.get("/api/evidence/canned")
    def canned(name: str, request: Request):
        sql = CANNED.get(name)
        if not sql:
            return JSONResponse({"error": "unknown query"}, status_code=404)
        out = _run(request, sql, SYNTHETIC_SCOPE)
        if isinstance(out, JSONResponse):
            return out
        return {"sql": sql, **out}

    @app.get("/evidence", response_class=HTMLResponse)
    def page():
        return render_page(profile)

    if not public:
        @app.get("/api/evidence/scopes")
        def scopes(request: Request):
            p = _principal(request)
            if p is None:
                return JSONResponse({"error": "authentication required"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
            from agentic_os.evidence_query import perm  # noqa: PLC0415
            from agentic_os.evidence_query.authz import allowed  # noqa: PLC0415
            return {"scopes": [s for s in catalog.scope_ids() if allowed(service.ceiling, p, perm(s, "inspect"))]}

    app.state.profile = profile
    return app


_SUMMARY: Dict[str, Any] = {}


def SUMMARY(profile: str) -> Dict[str, Any]:
    return dict(_SUMMARY, profile=profile)


def set_summary(store) -> None:
    from collections import Counter  # noqa: PLC0415
    c = Counter(e.family for e in store.snapshot(""))
    _SUMMARY.clear()
    _SUMMARY.update({"backend": "file", "rows": len(store),
                     "families": [{"family": f, "rows": n} for f, n in c.most_common()], "canned": list(CANNED.keys())})

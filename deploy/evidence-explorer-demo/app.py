"""Evidence Explorer — demo.redevops.io/evidence.

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
from __future__ import annotations

import os
import re
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Dict, List

import duckdb
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

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


def build_corpus() -> FileEvidenceStore:
    """Run every scenario through the real SecurityMonitor and persist the canonical evidence."""
    from agentic_os.mission.executor import Executor, InMemoryOperatorClient  # noqa: PLC0415
    from agentic_os.mission.security_monitor import SecurityMonitor  # noqa: PLC0415
    from agentic_os.mission.tracing import MissionTrace  # noqa: PLC0415
    from agentic_os.mission.types import Node  # noqa: PLC0415

    if os.path.exists(STORE_PATH):
        os.remove(STORE_PATH)
    store = FileEvidenceStore(STORE_PATH, clock=_seq_clock())

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


def load_duckdb(store: FileEvidenceStore) -> "duckdb.DuckDBPyConnection":
    con = duckdb.connect(":memory:")
    rows = [_row(e) for e in store.snapshot("")]
    cols = list(rows[0].keys())
    con.execute(f"CREATE TABLE evidence ({', '.join(f'{c} VARCHAR' for c in cols)})")
    con.executemany(
        f"INSERT INTO evidence VALUES ({', '.join('?' for _ in cols)})",
        [[r[c] for c in cols] for r in rows],
    )
    return con


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

_STORE = build_corpus()
_CON = load_duckdb(_STORE)
_ROWCOUNT = len(_STORE)

app = FastAPI(title="ReDevOps Evidence Explorer")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/evidence/summary")
def summary():
    fams = _CON.execute("SELECT family, count(*) FROM evidence GROUP BY family ORDER BY 2 DESC").fetchall()
    return {"backend": "file", "path": STORE_PATH, "rows": _ROWCOUNT,
            "families": [{"family": f, "rows": n} for f, n in fams],
            "canned": list(CANNED.keys())}


class Q(BaseModel):
    sql: str


_ALLOWED = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_FORBIDDEN = re.compile(r"\b(attach|copy|install|load|pragma|create|insert|update|delete|drop|alter|export)\b", re.IGNORECASE)


@app.post("/api/evidence/query")
def query(q: Q):
    sql = q.sql.strip().rstrip(";")
    if not _ALLOWED.match(sql) or _FORBIDDEN.search(sql):
        return JSONResponse({"error": "read-only: only a single SELECT/WITH query is allowed"}, status_code=400)
    try:
        cur = _CON.execute(sql + " LIMIT 500")
        cols = [d[0] for d in cur.description]
        return {"columns": cols, "rows": [list(r) for r in cur.fetchall()]}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/evidence/canned")
def canned(name: str):
    sql = CANNED.get(name)
    if not sql:
        return JSONResponse({"error": "unknown query"}, status_code=404)
    return {"sql": sql, **query(Q(sql=sql))}


@app.get("/evidence", response_class=HTMLResponse)
def page():
    return _PAGE


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
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
<p class="sub">Query the canonical runtime evidence a benchmark run <em>saved</em> — persisted through the open
<code>EvidenceStore</code> seam, read back with SQL.</p>
<div class="card"><p>A set of scenarios was driven through the <strong>real</strong> runtime security monitor; every
boundary <code>RuntimeSecurityEvent</code>, per-call <code>SecurityDecision</code>, correlated
<code>GovernanceDisposition</code> and Mission trace span was projected onto an <code>EvidenceEnvelope</code> and
appended to a <strong>file</strong> backend (<span class="mono">runtime_contracts.store.FileEvidenceStore</span>) — the
open, dependency-free floor. An enterprise deployment swaps that backend for a managed database or a lakehouse
behind the same interface. <span id="meta" class="mono"></span></p>
<div id="fams"></div></div>
<div class="card"><div class="chips" id="canned"></div>
<textarea id="sql">SELECT family, count(*) AS rows FROM evidence GROUP BY family ORDER BY rows DESC</textarea>
<div style="margin-top:10px"><button onclick="run()">Run query</button> <span id="status" class="mono"></span></div>
<div class="scroll"><div id="out"></div></div></div>
<script>
async function meta(){
  const d = await (await fetch('/api/evidence/summary')).json();
  document.getElementById('meta').textContent = `backend=${d.backend} · ${d.rows} rows saved`;
  document.getElementById('fams').innerHTML = d.families.map(f=>`<span class="fam"><b>${f.rows}</b> ${f.family}</span>`).join('');
  document.getElementById('canned').innerHTML = d.canned.map(n=>`<span class="chip" onclick="canned('${n.replace(/'/g,"\\\\'")}')">${n}</span>`).join('');
}
function render(d){
  if(d.error){document.getElementById('out').innerHTML = `<p class="err">${d.error}</p>`; return;}
  const th = d.columns.map(c=>`<th>${c}</th>`).join('');
  const tr = d.rows.map(r=>`<tr>${r.map(c=>`<td>${c===null?'—':c}</td>`).join('')}</tr>`).join('');
  document.getElementById('out').innerHTML = `<table><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table>`;
  document.getElementById('status').textContent = `${d.rows.length} row(s)`;
}
async function run(){
  document.getElementById('status').textContent = 'running…';
  const sql = document.getElementById('sql').value;
  render(await (await fetch('/api/evidence/query',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({sql})})).json());
}
async function canned(name){
  document.getElementById('status').textContent = 'running…';
  const d = await (await fetch('/api/evidence/canned?name='+encodeURIComponent(name))).json();
  if(d.sql) document.getElementById('sql').value = d.sql;
  render(d);
}
meta(); run();
</script></div></body></html>"""

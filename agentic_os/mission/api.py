"""FastAPI router for Mission Runtime — the /missions control-plane surface.

Thin: it delegates to a MissionRuntime. Mount into the existing control_plane.py with
`app.include_router(build_router(runtime))`. FastAPI is imported lazily so the kernel stays
usable (and testable) without the web dependency.
"""

from typing import Any

from .runtime import MissionRuntime
from .simulator import simulate
from .types import Budget, to_jsonable


def build_router(runtime: MissionRuntime):
    from fastapi import APIRouter, HTTPException
    from pydantic import BaseModel

    router = APIRouter(prefix="/missions", tags=["missions"])

    class CreateMission(BaseModel):
        goal: str
        constraints: list[str] = []
        policy_refs: list[str] = ["*"]
        template: str | None = None
        budget_usd: float | None = None

    class Approve(BaseModel):
        node_id: str
        decision: str = "approve"
        edit: dict[str, Any] | None = None

    def _mission_view(mid: str) -> dict:
        m = runtime._missions.get(mid)
        if not m:
            raise HTTPException(404, "mission not found")
        plan = runtime._plans.get(mid)
        return {
            "id": m.id, "goal": m.goal, "state": m.state.value, "template": m.template,
            "outcome": m.outcome,
            "active_plan": to_jsonable(plan) if plan else None,
            "pending_human": runtime.repo.pending_human(mid),
        }

    @router.post("")
    def create(body: CreateMission):
        budget = Budget(usd=body.budget_usd) if body.budget_usd else None
        m = runtime.create_mission(body.goal, constraints=body.constraints,
                                   policy_refs=body.policy_refs, template=body.template, budget=budget)
        runtime.run(m.id)
        return _mission_view(m.id)

    @router.get("")
    def list_missions():
        return {"missions": runtime.missions()}

    @router.get("/{mid}")
    def get(mid: str):
        return _mission_view(mid)

    @router.get("/{mid}/plans")
    def plans(mid: str):
        plan = runtime._plans.get(mid)
        return {"plans": [to_jsonable(plan)] if plan else []}

    @router.post("/{mid}/simulate")
    def resimulate(mid: str):
        m = runtime._missions.get(mid)
        plan = runtime._plans.get(mid)
        if not m or not plan:
            raise HTTPException(404, "mission not found")
        plan.projection = simulate(plan, m, runtime.registry, runtime.policy)
        return to_jsonable(plan.projection)

    @router.post("/{mid}/approve")
    def approve(mid: str, body: Approve):
        runtime.approve(mid, body.node_id, body.decision, body.edit)
        return _mission_view(mid)

    @router.get("/{mid}/events")
    def events(mid: str):
        return {"timeline": runtime.repo.timeline(mid)}

    @router.get("/{mid}/explain")
    def explain(mid: str):
        return runtime.explain(mid)

    return router


def build_capabilities_router(runtime: MissionRuntime):
    from fastapi import APIRouter

    router = APIRouter(tags=["capabilities"])

    @router.get("/capabilities")
    def capabilities(need: str | None = None, k: int = 5):
        if need:
            return {"need": need,
                    "matches": [{"capability": c.name, "operator": c.operator, "score": round(s, 3)}
                                for c, s in runtime.registry.discover(need, k)]}
        return {"capabilities": [to_jsonable(c) for c in runtime.registry.all()]}

    @router.get("/learning")
    def learning():
        """What the four learning loops have learned so far (capability / planning / business)."""
        return runtime.learning.policy()

    return router


def build_cockpit_router(runtime: MissionRuntime):
    """P6 — the human-facing shell: an inbox of open human tasks + a one-page cockpit to launch,
    watch, approve/resolve and EXPLAIN a mission. No build step; vanilla JS over the JSON API."""
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse

    router = APIRouter(tags=["cockpit"])

    @router.get("/inbox")
    def inbox():
        return {"inbox": runtime.inbox()}

    @router.get("/cockpit", response_class=HTMLResponse)
    def cockpit():
        return _COCKPIT_HTML

    return router


_COCKPIT_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Mission Plane · redevops.io</title>
<style>
:root{
  --bg:#0e0f13;--panel:#16171d;--card:#1b1c23;--line:#2a2c36;--line2:#363945;
  --fg:#e9e8ee;--mut:#9a9aa6;--dim:#6f6f7c;
  --teal:#4fd1c5;--amber:#f5b544;--red:#ff6b6b;--blue:#7fb0ff;--violet:#b39ddb;--grey:#6b7280;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
h2{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--dim);margin:0 0 10px;font-weight:700}
.mut{color:var(--mut)}.dim{color:var(--dim)}code{color:var(--teal);font-size:12px}
a{color:var(--teal);cursor:pointer;text-decoration:none}
input,select,button{font:inherit}
input,select{background:#0d0e12;border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:7px 10px}
button{background:var(--teal);color:#08110f;border:0;border-radius:8px;padding:7px 12px;font-weight:650;cursor:pointer}
button:hover{filter:brightness(1.08)}button.sec{background:#282a33;color:var(--fg)}button.red{background:var(--red);color:#1a0808}
button.sm{padding:4px 9px;font-size:12px}
/* shell */
.top{display:flex;gap:12px;align-items:center;justify-content:space-between;
  padding:12px 20px;border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:5}
.top h1{font-size:15px;margin:0;font-weight:700;letter-spacing:.2px}
.shell{display:grid;grid-template-columns:340px 1fr;min-height:calc(100vh - 55px)}
.side{border-right:1px solid var(--line);background:var(--panel);overflow:auto;max-height:calc(100vh - 55px)}
.side section{padding:14px 16px;border-bottom:1px solid var(--line)}
.main{overflow:auto;max-height:calc(100vh - 55px);padding:20px 24px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
/* pills — mission + node status share one scale */
.pill{font-size:10.5px;padding:2px 8px;border-radius:999px;font-weight:700;letter-spacing:.02em;white-space:nowrap}
.st-done,.st-succeeded{background:#0e211d;color:var(--teal)}
.st-failed{background:#271417;color:var(--red)}
.st-waiting,.st-waiting_human{background:#271f13;color:var(--amber)}
.st-running,.st-ready,.st-planning{background:#13202e;color:var(--blue)}
.st-pending{background:#1d1e26;color:var(--dim)}
.st-skipped{background:#1d1e26;color:var(--mut)}
.st-compensated{background:#211a2b;color:var(--violet)}
/* mission rows */
.mrow{display:block;padding:9px 11px;border:1px solid transparent;border-radius:9px;margin:3px 0;cursor:pointer}
.mrow:hover{background:#1c1d25}.mrow.sel{background:#1c1e29;border-color:var(--line2)}
.mrow .g{font-weight:600;color:var(--fg)}
/* cards */
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
.tabs{display:flex;gap:4px;margin:2px 0 16px;flex-wrap:wrap}
.tab{padding:6px 12px;border-radius:8px;color:var(--mut);cursor:pointer;font-weight:600;font-size:13px;border:1px solid transparent}
.tab:hover{color:var(--fg)}.tab.on{background:var(--card);color:var(--fg);border-color:var(--line)}
/* projection strip */
.proj{display:flex;gap:10px;flex-wrap:wrap}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:9px 13px;min-width:96px}
.stat b{display:block;font-size:17px;font-weight:700}.stat span{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim)}
/* task / graph node */
.node{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border:1px solid var(--line);
  border-left:3px solid var(--line2);border-radius:9px;margin:7px 0;background:#191a21}
.node.n-done{border-left-color:var(--teal)}.node.n-failed{border-left-color:var(--red)}
.node.n-waiting{border-left-color:var(--amber)}.node.n-running,.node.n-ready{border-left-color:var(--blue)}
.node.n-compensated{border-left-color:var(--violet)}.node.n-pending,.node.n-skipped{border-left-color:var(--line2)}
.node .cap{font-weight:600}.node .meta{font-size:12px;color:var(--mut);margin-top:2px}
.node{cursor:pointer}.node:hover{border-color:var(--line2)}
.rchips{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px}
.rchip{font-size:11px;background:#12291f;color:#7fe0a8;border-radius:6px;padding:2px 8px}
.rchip.k{background:#1e2028;color:var(--mut)}.rchip.k b{color:var(--fg);font-weight:600}
.rfail{color:var(--red);font-size:12px;margin-top:6px}
.drawer{position:fixed;top:0;right:0;width:min(460px,94vw);height:100vh;background:var(--panel);border-left:1px solid var(--line);box-shadow:-24px 0 60px rgba(0,0,0,.45);z-index:60;overflow:auto;transform:translateX(100%);transition:transform .2s}
.drawer.open{transform:translateX(0)}
.drawer .dh{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:15px 18px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--panel)}
.drawer .db{padding:16px 18px}.drawer h3{font-size:14px;margin:0;font-family:ui-monospace,monospace;color:var(--teal)}
.drawer h4{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);margin:16px 0 6px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:5px 12px;font-size:13px}.kv .kk{color:var(--dim)}
.backdrop{position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:59;display:none}.backdrop.open{display:block}
.flag{font-size:10px;padding:1px 6px;border-radius:5px;background:#242630;color:var(--mut);margin-left:6px}
.flag.gate{background:#271f13;color:var(--amber)}.flag.undo{background:#211a2b;color:var(--violet)}
/* belief chips */
.chip{display:inline-flex;flex-direction:column;gap:3px;border:1px solid var(--line);border-radius:9px;
  padding:8px 11px;margin:5px 6px 0 0;min-width:150px;background:#191a21;vertical-align:top}
.chip .k{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.05em}
.chip .v{font-weight:600;word-break:break-word}
.bar{height:4px;border-radius:3px;background:#2a2c36;overflow:hidden;margin-top:4px}
.bar>i{display:block;height:100%;background:var(--teal)}
/* timeline */
.tl{border-left:2px solid var(--line2);margin-left:6px;padding-left:14px}
.tev{position:relative;padding:5px 0}
.tev:before{content:"";position:absolute;left:-19px;top:11px;width:7px;height:7px;border-radius:50%;background:var(--dim)}
.tev.ok:before{background:var(--teal)}.tev.bad:before{background:var(--red)}.tev.wait:before{background:var(--amber)}
/* inbox packet */
.packet{border:1px solid var(--line);border-left:3px solid var(--amber);border-radius:9px;padding:11px 12px;margin:8px 0;background:#191a21}
.packet .ev{font-size:12px;margin:3px 0}
pre{white-space:pre-wrap;font-size:12px;color:#cfd0d8;margin:6px 0 0;background:#131319;padding:9px;border-radius:8px}
.empty{color:var(--dim);padding:40px 0;text-align:center}
</style></head><body>
<div class=top>
  <h1>🛰 Mission Plane <span class=dim style="font-weight:500">· redevops.io Agentic OS v5</span></h1>
  <div class=row>
    <input id=goal placeholder="New mission goal…" style="width:230px">
    <select id=tpl><option value="">no template</option><option value=product_launch>product_launch</option>
      <option value=revenue_rescue>revenue_rescue</option><option value=onboarding>onboarding</option>
      <option value=invoice_recovery>invoice_recovery</option></select>
    <button onclick=launch()>Launch</button>
    <button class="sec" onclick=refreshAll()>↻</button>
  </div>
</div>

<div class=shell>
  <aside class=side>
    <section><h2>Inbox — awaiting a human <span id=ibx_n class=dim></span></h2><div id=inbox></div></section>
    <section><h2>Missions <span id=ms_n class=dim></span></h2><div id=missions></div></section>
  </aside>
  <main class=main id=main><div class=empty>Select a mission, or launch one above.</div></main>
</div>
<div class=backdrop id=backdrop onclick=closeNode()></div>
<aside class=drawer id=drawer></aside>

<script>
const J=r=>r.json(), api=(u,o)=>fetch(u,o).then(J);
const esc=s=>String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const short=id=>String(id||'').replace(/^node[_-]?/,'').slice(0,6);
function pill(s){return `<span class="pill st-${s}">${esc(s)}</span>`}
let CUR=null, TAB='graph';

async function launch(){
  const g=document.getElementById('goal').value.trim(); if(!g)return;
  const t=document.getElementById('tpl').value||null;
  const m=await api('/missions',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({goal:g,template:t,policy_refs:['*']})});
  document.getElementById('goal').value=''; CUR=m.id; await refreshAll(); openMission(m.id);
}
async function decide(mid,node,decision,kind){
  let edit=null;
  if(kind==='disambiguation'){const v=(document.getElementById('v_'+node)||{}).value; edit={value:v};}
  await api(`/missions/${mid}/approve`,{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({node_id:node,decision,edit})});
  await refreshAll(); if(CUR)openMission(CUR);
}
async function simulate(mid){await api(`/missions/${mid}/simulate`,{method:'POST'}); openMission(mid);}

/* ── left rail ── */
async function refreshAll(){
  const [ib,ml]=await Promise.all([api('/inbox'),api('/missions')]);
  const inb=ib.inbox||[];
  document.getElementById('ibx_n').textContent=inb.length?`(${inb.length})`:'';
  document.getElementById('inbox').innerHTML = inb.length? inb.map(t=>{
    const ev=(t.evidence||[]).map(e=>`<div class="ev mut">· <code>${esc(e)}</code></div>`).join('');
    const ctl = t.kind==='disambiguation'
      ? `<input id="v_${t.node_id}" placeholder="authoritative value" style="width:150px">
         <button class=sm onclick="decide('${t.mission_id}','${t.node_id}','resolve','disambiguation')">Resolve</button>`
      : `<button class=sm onclick="decide('${t.mission_id}','${t.node_id}','approve')">Approve</button>
         <button class="sm red" onclick="decide('${t.mission_id}','${t.node_id}','reject')">Reject</button>`;
    return `<div class=packet><div class=row style="justify-content:space-between">
        <b>${esc(t.kind)}</b><a onclick="openMission('${t.mission_id}')">open ↗</a></div>
      <div class="mut" style="font-size:12px">${esc(t.goal||'')}</div>
      <div style="margin:5px 0">${esc(t.prompt||'')}</div>${ev}
      <div class=row style="margin-top:8px">${ctl}</div></div>`;
  }).join('') : '<div class=dim>Nothing waiting. 🎉</div>';

  const ms=ml.missions||[];
  document.getElementById('ms_n').textContent=ms.length?`(${ms.length})`:'';
  document.getElementById('missions').innerHTML = ms.length? ms.slice().reverse().map(m=>
    `<div class="mrow${m.id===CUR?' sel':''}" onclick="openMission('${m.id}')">
      <div class=row style="justify-content:space-between">
        <span class=g>${esc(m.goal||m.id)}</span>${pill(m.state)}</div>
      <div class=dim style="font-size:12px">${esc(m.template||'—')}${m.awaiting_human?' · ⏳ needs you':''}</div></div>`
  ).join('') : '<div class=dim>No missions yet.</div>';
}

/* ── detail ── */
async function openMission(mid){
  CUR=mid;
  document.querySelectorAll('.mrow').forEach(e=>e.classList.remove('sel'));
  const [m,ev,x]=await Promise.all([
    api(`/missions/${mid}`), api(`/missions/${mid}/events`), api(`/missions/${mid}/explain`)]);
  window._M={m,ev:ev.timeline||[],x}; renderDetail();
  refreshAll();
}
// ── per-node results + a click-through detail drawer (observability, not a picture) ──
function resultChips(r){
  if(!r||typeof r!=='object') return '';
  const parts=[];
  if(r.summary) parts.push(`<span class=rchip>✓ ${esc(String(r.summary).slice(0,90))}</span>`);
  for(const [k,v] of Object.entries(r)){
    if(['summary','status','action'].includes(k)||v==null||typeof v==='object') continue;
    parts.push(`<span class="rchip k">${esc(k)} <b>${esc(String(v).slice(0,44))}</b></span>`);
  }
  return parts.length?`<div class=rchips>${parts.join('')}</div>`:'';
}
function failReason(id){
  const ev=(window._M&&window._M.ev)||[];
  const f=ev.find(e=>(e.payload||{}).node_id===id && /fail|reject/i.test(e.type));
  return f?(f.payload.reason||f.payload.error||f.payload.detail||f.type):'failed';
}
function openNode(id){
  const {m,ev}=window._M; const nodes=((m.active_plan||{}).graph||{}).nodes||[];
  const n=nodes.find(z=>z.id===id); if(!n) return;
  const trail=ev.filter(e=>(e.payload||{}).node_id===id).map(e=>{
    const cls=/fail|reject/i.test(e.type)?'bad':/succeed|done|commit/i.test(e.type)?'ok':/wait|park|approv/i.test(e.type)?'wait':'';
    return `<div class="tev ${cls}"><code>${esc(e.type)}</code></div>`;}).join('')||'<span class=dim>—</span>';
  const resBlock=n.result?`${resultChips(n.result)}<pre>${esc(JSON.stringify(n.result,null,2))}</pre>`
    :(n.status==='pending'||n.status==='ready'?'<div class=dim>Not run yet.</div>':'<div class=dim>No result recorded.</div>');
  const isInfra=/^infra\.|supply_chain/.test(n.capability);
  const where=isInfra
    ? `<h4>Where to see the details</h4><div class=mut style="font-size:12.5px;line-height:1.7">
        · <b>Logs</b> — the service's stdout → journald / Loki, tagged app · env · git SHA.<br>
        · <b>Metrics</b> — fleet <code>/health</code> + mission SLOs → control-tower / Metabase.<br>
        · <b>Traces</b> — this node's span → OTEL / Langfuse.<br>
        · <b>Record</b> — the result + status trail here are the authoritative, replayable audit record.</div>`
    : `<h4>Where to see the details</h4><div class=mut style="font-size:12.5px;line-height:1.7">
        · <b>Result</b> — exactly what this app returned, above.<br>
        · <b>Evidence & trail</b> — every state transition is event-sourced (replayable/auditable).<br>
        · <b>Metrics</b> — this app's <code>/health</code> + the mission SLOs (control-tower).</div>`;
  const dec=((window._M.x||{}).decisions||{})[id];
  const explain=dec?`<h4>Why this step ran this way — EXPLAIN</h4>
      <div class=kv>
        <span class=kk>chosen</span><span><code>${esc(dec.capability)}</code> · ${esc(dec.reason||'')}</span>
        <span class=kk>confidence</span><span>${dec.confidence!=null?Math.round(dec.confidence*100)+'%':'—'} <span class=dim>calibrated success rate</span></span>
        <span class=kk>cost</span><span>$${(dec.cost_usd||0).toFixed(4)} · ${dec.latency_ms||0}ms · value ${esc(dec.estimated_value||'—')}</span></div>
      ${dec.confidence!=null?`<div class=bar style="margin-top:7px"><i style="width:${Math.round(dec.confidence*100)}%"></i></div>`:''}
      ${dec.alternatives&&dec.alternatives.length
        ?`<div class=dim style="font-size:12px;margin-top:10px">Candidates considered (ranked by confidence):</div>`
          +dec.alternatives.map(a=>`<div class=meta style="margin-top:3px">↳ <code>${esc(a.capability)}</code> <span class=dim>${esc(a.operator)}</span> · ${Math.round((a.confidence||0)*100)}%</div>`).join('')
        :`<div class=dim style="font-size:12px;margin-top:8px">Sole provider — no alternative capability could satisfy this step.</div>`}`:'';
  document.getElementById('drawer').innerHTML=`
    <div class=dh><h3>${esc(n.capability)}</h3><button class="sec sm" onclick="closeNode()">✕ close</button></div>
    <div class=db>
      <div class=kv>
        <span class=kk>app</span><span>${esc(n.operator||'')}</span>
        <span class=kk>status</span><span>${pill(n.status)}</span>
        ${n.produces?`<span class=kk>produces</span><code>${esc(n.produces)}</code>`:''}
        ${n.approval_required?'<span class=kk>gate</span><span>human approval</span>':''}
        ${n.side_effecting?'<span class=kk>effect</span><span>side-effecting</span>':''}
        ${n.undo?`<span class=kk>undo</span><code>${esc(n.undo)}</code>`:''}</div>
      <h4>${n.status==='failed'?'✕ What failed & why':(n.status==='done'?'✓ What it produced':'Result')}</h4>
      ${n.status==='failed'?`<div class=rfail>${esc(failReason(id))}</div>`:''}
      ${resBlock}
      ${explain}
      <h4>Status trail</h4><div class=tl>${trail}</div>
      ${where}
    </div>`;
  document.getElementById('drawer').classList.add('open');
  document.getElementById('backdrop').classList.add('open');
}
function closeNode(){ document.getElementById('drawer').classList.remove('open'); document.getElementById('backdrop').classList.remove('open'); }

function renderDetail(){
  const {m,ev,x}=window._M; const p=m.active_plan;
  const proj=p&&p.projection?p.projection:null;
  const nodes=(p&&p.graph&&p.graph.nodes)||[];
  const capOf={}; nodes.forEach(n=>capOf[n.id]=n.capability);
  const projStrip = proj? `<div class=proj>
     <div class=stat><b>$${(proj.expected_usd||0).toFixed(2)}</b><span>est cost</span></div>
     <div class=stat><b>${Math.round((proj.expected_success||0)*100)}%</b><span>success</span></div>
     <div class=stat><b>${Math.round((proj.expected_latency_ms||0)/1000)}s</b><span>latency</span></div>
     <div class=stat><b>${proj.expected_approvals||0}</b><span>approvals</span></div></div>` : '';

  const tabs=['graph','world','timeline','explain'];
  const tabbar=tabs.map(t=>`<div class="tab${t===TAB?' on':''}" onclick="TAB='${t}';renderDetail()">${t==='graph'?'Graph · Tasks':t[0].toUpperCase()+t.slice(1)}</div>`).join('');

  let body='';
  if(TAB==='graph'){
    const counts={}; nodes.forEach(n=>counts[n.status]=(counts[n.status]||0)+1);
    const legend=Object.entries(counts).map(([s,c])=>`${pill(s)} ${c}`).join(' &nbsp; ')||'<span class=dim>no plan compiled</span>';
    body=`<div class=card><h2>Execution graph — ${nodes.length} node${nodes.length===1?'':'s'} ·
        enforced depends_on <span class=dim>(rev ${p?p.revision:'—'}${p&&p.reason?' · '+esc(p.reason):''})</span></h2>
      <div class=row style="margin-bottom:6px">${legend}</div>
      ${nodes.map(n=>{
        const deps=(n.depends_on||[]).map(d=>`<code>${esc(capOf[d]||short(d))}</code>`).join(', ');
        const flags=[n.approval_required?'<span class="flag gate">⛉ approval</span>':'',
          n.side_effecting?'<span class=flag>side-effect</span>':'',
          n.undo?`<span class="flag undo">↩ ${esc(n.undo)}</span>`:'',
          n.cost&&n.cost.usd?`<span class=flag>$${n.cost.usd.toFixed(2)}</span>`:''].join('');
        const rchips = n.status==='done' ? resultChips(n.result) : '';
        const failLine = n.status==='failed' ? `<div class=rfail>✕ ${esc(failReason(n.id))}</div>` : '';
        return `<div class="node n-${n.status}" onclick="openNode('${n.id}')" title="click for details, results and where to look">
          <div style="flex:1">
            <div class=row style="justify-content:space-between"><span class=cap><code>${esc(n.capability)}</code></span>${pill(n.status)}</div>
            <div class=meta>${esc(n.operator||'')}${deps?' &nbsp;⇠ '+deps:''}${n.produces?' &nbsp;→ writes <code>'+esc(n.produces)+'</code>':''}</div>
            <div style="margin-top:4px">${flags}</div>
            ${rchips}${failLine}
          </div></div>`;
      }).join('')||'<div class=dim>No nodes yet — mission still planning.</div>'}
      <div class=row style="margin-top:10px"><button class="sec sm" onclick="simulate('${m.id}')">Re-simulate</button></div></div>`;
  } else if(TAB==='world'){
    const ws=x.world_state||{};
    const chips=Object.entries(ws).map(([k,v])=>{
      let val=v, conf=null, conflict=false, state=null;
      if(v&&typeof v==='object'&&!Array.isArray(v)){
        if('value'in v)val=v.value; if('confidence'in v)conf=v.confidence;
        conflict=!!v.conflict; state=v.state||null;
      }
      const vs=typeof val==='object'?JSON.stringify(val):esc(val);
      const bar=conf!=null?`<div class=bar><i style="width:${Math.round(conf*100)}%"></i></div>
         <div class=dim style="font-size:11px">${Math.round(conf*100)}% conf${state?' · '+esc(state):''}${conflict?' · ⚠ conflict':''}</div>`:'';
      return `<div class=chip><div class=k>${esc(k)}</div><div class=v>${vs}</div>${bar}</div>`;
    }).join('')||'<div class=dim>World state is empty.</div>';
    body=`<div class=card><h2>World / belief state <span class=dim>(Observed → Verified → Believed → Committed)</span></h2>${chips}</div>`;
  } else if(TAB==='timeline'){
    const items=(ev.length?ev:(x.steps||[])).map(e=>{
      const t=e.type||e.kind||e.event||'event';
      const pl=e.payload||e; const label=pl.capability||pl.key||pl.node||pl.detail||'';
      const cls=/fail|reject|error/i.test(t)?'bad':/wait|approv/i.test(t)?'wait':/done|commit|succeed|verified/i.test(t)?'ok':'';
      return `<div class="tev ${cls}"><code>${esc(t)}</code> <span class=mut>${esc(typeof label==='object'?JSON.stringify(label):label)}</span></div>`;
    }).join('')||'<div class=dim>No events yet.</div>';
    body=`<div class=card><h2>Timeline — event-sourced activity</h2><div class=tl>${items}</div></div>`;
  } else {
    const steps=(x.steps||[]).map(s=>`<div class=mut><code>${esc(s.type)}</code> ${esc((s.payload||{}).capability||(s.payload||{}).key||'')}</div>`).join('')||'<div class=dim>—</div>';
    body=`<div class=card><h2>EXPLAIN — decision trace</h2>${steps}
      <h2 style="margin-top:14px">What the learners updated</h2>
      <pre>${esc(JSON.stringify(x.learning||{},null,2))}</pre></div>`;
  }

  document.getElementById('main').innerHTML = `
    <div class=row style="justify-content:space-between;align-items:flex-start;margin-bottom:6px">
      <div><div style="font-size:18px;font-weight:700">${esc(m.goal||m.id)}</div>
        <div class=dim style="font-size:12px">${esc(m.template||'no template')}
          ${m.outcome?' · outcome: <code>'+esc(m.outcome)+'</code>':''}</div></div>
      ${pill(m.state)}</div>
    ${projStrip}
    <div class=tabs style="margin-top:14px">${tabbar}</div>
    ${body}`;
}

refreshAll();
setInterval(()=>{ refreshAll(); }, 5000);
// Deep-link from /overview: /cockpit?template=<t>&goal=<g>[&run=1] pre-fills the launch form so a
// customer lands with the workflow ready to run (run=1 launches it immediately).
(function(){
  const p=new URLSearchParams(location.search), g=p.get('goal'), t=p.get('template');
  if(!g && !t) return;
  if(g) document.getElementById('goal').value=g;
  if(t){const s=document.getElementById('tpl');
    if(![...s.options].some(o=>o.value===t)){const o=document.createElement('option');o.value=t;o.textContent=t;s.appendChild(o);}
    s.value=t;}
  if(p.get('run')==='1'){ launch(); return; }
  const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='Launch');
  if(b){ b.scrollIntoView({block:'center'}); b.style.boxShadow='0 0 0 3px var(--teal)';
    setTimeout(()=>{ b.style.boxShadow=''; }, 4500); }
})();
</script></body></html>"""

"""Animated execution canvas — demo.redevops.io/worlds (docx P6 / v2 §33).

Runs the REAL world-runtime engine (agentic_os.world) over a dataset world and streams the resulting
VisualTrace to an animated canvas: a business event on the left, the Runtime spine in the centre, the four
business blocks on the right, with app nodes lighting up as the mission crosses them and a context/evidence/
policy capsule moving with the mission — not copied between apps. A counterfactual scorecard shows why the
Runtime is necessary. Nothing is mocked at the engine; writes are SIMULATE and labelled.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from runtime_contracts import AuthorityContext, PrincipalRef
from agentic_os.world import ALL_WORLDS, BenchmarkRunner, ScenarioOrchestrator

_ANSWERS = {"What is the roof pitch?": "6/12", "Approve invoice correction?": "yes",
            "Approve outreach to acme-ai-platform?": "yes",
            "Approve sponsorship portfolio?": "yes",
            "POLICY_APPROVAL": "approve within ceiling"}     # governed sponsorship booking approval
_SCOPES = ("read:crm", "read:geo", "write:quote", "write:crm", "read:secrets", "write:vendor", "write:billing")
_SEEDS = {"after-hours-lead": "8842", "kyc-ownership": "clean", "finance-leakage": "4471",
          "gtm-pilot-discovery": "c1", "creator-sponsorship": "s1", "sponsorship-booking": "s1",
          "paid-acquisition": "s1", "contact-center": "c1"}


def _authority():
    return AuthorityContext(authority_id="ctx", principal=PrincipalRef(id="operator", tenant="demo"),
                            purpose="run world", scope=_SCOPES)


def run_world(world_id: str) -> dict:
    world = ALL_WORLDS[world_id]
    auth = _authority()
    seed = _SEEDS.get(world_id, "seed-0")
    offline = world_id in ("gtm-pilot-discovery", "creator-sponsorship", "sponsorship-booking",
                           "paid-acquisition")  # deterministic + fast; fixture-labelled SYNTHETIC/SEEDED
    orch = ScenarioOrchestrator()
    run = orch.run(world, seed=seed, authority=auth, answers=_ANSWERS, offline=offline)
    card = BenchmarkRunner().run(world, seed=seed, authority=auth, answers=_ANSWERS, offline=offline)
    d = run.to_dict()
    d["descriptor"] = {"title": world.descriptor().title, "realism": world.descriptor().realism,
                       "datasources": list(world.descriptor().datasources)}
    d["scorecard"] = card.to_dict()
    # which OSS-core apps this world's entities projected into, via which World Adapter + realism
    seen, apps = set(), []
    for p in orch._seeder.projections:
        if p["app"] not in seen:
            seen.add(p["app"]); apps.append({"app": p["app"], "adapter": p["adapter"], "realism": p["realism"]})
    d["projections"] = apps
    return d


app = FastAPI(title="ReDevOps execution canvas")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/worlds")
def worlds():
    return {"worlds": [{"world_id": w.world_id, "title": w.descriptor().title,
                        "realism": w.descriptor().realism} for w in ALL_WORLDS.values()]}


@app.get("/api/worlds/run")
def api_run(world: str = "after-hours-lead"):
    if world not in ALL_WORLDS:
        return JSONResponse({"error": "unknown world"}, status_code=404)
    return run_world(world)


@app.get("/worlds", response_class=HTMLResponse)
def page():
    return _PAGE


_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Execution Canvas · Agentic OS</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root{--bg:#0d1214;--panel:#141b1e;--line:#243033;--ink:#e7eef0;--muted:#93a3a8;--faint:#6d7f85;
  --spine:#39c9b8;--rev:#f5b544;--cs:#4fd1c5;--fin:#7bb4f0;--sec:#f56565;--ops:#9b8cf0;
  --live:#5bd67f;--sim:#f0a35a;--good:#5bd67f;--bad:#ef6b6b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
  font-family:"IBM Plex Sans",system-ui,sans-serif;line-height:1.5}
.mono{font-family:"IBM Plex Mono",monospace}
.wrap{max-width:1240px;margin:0 auto;padding:26px 22px 70px}
h1{font-family:"Archivo",sans-serif;font-weight:800;font-size:26px;letter-spacing:-.02em;margin:0}
.sub{color:var(--muted);margin:4px 0 0;font-size:14px}
.bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:18px 0}
select,button{font-family:inherit;font-size:14px;border-radius:8px;border:1px solid var(--line);
  background:var(--panel);color:var(--ink);padding:9px 13px}
button.play{background:var(--spine);color:#0d1214;border:0;font-weight:700;cursor:pointer}
button.play:disabled{opacity:.5;cursor:default}
.badge{font-family:"IBM Plex Mono",monospace;font-size:11px;padding:2px 8px;border-radius:5px;letter-spacing:.04em}
.b-live{color:#0d1214;background:var(--live)}.b-sim{color:#0d1214;background:var(--sim)}
.b-snap{color:#0d1214;background:#4fc7dd}.b-seed{color:#0d1214;background:var(--rev)}.b-syn{color:#0d1214;background:#b79bf3}
#canvas{position:relative;display:grid;grid-template-columns:1fr 1.15fr 1.3fr;gap:18px;
  background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px;min-height:360px;overflow:hidden}
.col h2{font-family:"IBM Plex Mono",monospace;font-size:11px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--faint);margin:0 0 12px}
.node{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:9px;background:#0f1719;
  transition:border-color .25s,background .25s,box-shadow .25s}
.node .t{font-weight:600;font-size:13.5px}.node .d{font-size:11.5px;color:var(--faint)}
.node.on{border-color:var(--accent,#39c9b8);background:#12211f;box-shadow:0 0 0 1px var(--accent,#39c9b8)}
.spine .node.on{--accent:var(--spine)}
.block{border:1px solid var(--line);border-radius:10px;padding:8px 10px;margin-bottom:9px}
.block .bt{font-size:12px;font-weight:600;color:var(--muted);margin-bottom:6px}
.chip{display:inline-block;font-size:11px;border:1px solid var(--line);border-radius:6px;padding:3px 8px;
  margin:2px 3px 0 0;color:var(--muted);transition:all .25s}
.chip.on{color:#0d1214;font-weight:600}
.rev .chip.on{background:var(--rev)}.cs .chip.on{background:var(--cs)}.fin .chip.on{background:var(--fin)}
.sec .chip.on{background:var(--sec);color:#fff}.ops .chip.on{background:var(--ops)}
.event{border:1px solid var(--line);border-radius:10px;padding:12px;background:#0f1719}
.timer{font-family:"IBM Plex Mono",monospace;font-size:22px;color:var(--rev);margin-top:6px}
.timer.done{color:var(--good)}
#capsule{position:absolute;z-index:5;padding:5px 9px;border-radius:8px;background:var(--spine);color:#0d1214;
  font-family:"IBM Plex Mono",monospace;font-size:11px;font-weight:600;pointer-events:none;opacity:0;
  transition:transform .6s cubic-bezier(.4,0,.2,1),opacity .3s;box-shadow:0 4px 16px rgba(0,0,0,.4)}
.now{margin:14px 0 0;min-height:24px;font-size:14px}.now .k{color:var(--faint)}
.needs{display:none;margin-top:12px;border:1px solid var(--rev);border-radius:10px;padding:12px 14px;background:#1c1708}
.needs.show{display:block}.needs .h{color:var(--rev);font-weight:700;font-size:13px}
.panels{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
.card h3{margin:0 0 10px;font-family:"Archivo";font-size:15px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
td,th{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
th{color:var(--faint);font-family:"IBM Plex Mono",monospace;font-size:10.5px;letter-spacing:.06em;text-transform:uppercase}
td.ok{color:var(--good)}td.no{color:var(--bad)}.mono.small{font-size:12px;color:var(--muted)}
.kpi{display:flex;gap:18px;flex-wrap:wrap}.kpi .v{font-family:"Archivo";font-size:22px;font-weight:800}
.kpi .l{font-size:11px;color:var(--faint)}
@media(max-width:820px){#canvas{grid-template-columns:1fr}.panels{grid-template-columns:1fr}}
</style></head><body><div class="wrap">
<h1>Execution Canvas</h1>
<p class="sub">One business event → the Runtime spine → your systems. The mission crosses apps carrying one
identity/evidence/policy capsule — never copied. Writes are <span class="badge b-sim">SIMULATE</span>. The real engine, not a mock.</p>
<div class="bar">
  <select id="world"></select>
  <button class="play" id="play" onclick="play()">▶ Watch the mission</button>
  <span id="realism"></span>
  <span id="sources" class="mono" style="color:var(--faint);font-size:12px"></span>
</div>

<div id="canvas">
  <div class="col" id="left"><h2>Business event</h2>
    <div class="event"><div id="ev-title" style="font-weight:600">—</div>
      <div id="ev-desc" class="d" style="color:var(--faint);font-size:12.5px;margin-top:4px"></div>
      <div class="timer" id="timer">00:00</div>
      <div style="font-size:11px;color:var(--faint)">value decaying while a human would still be waiting</div>
    </div>
    <div class="now" id="now"><span class="k">idle —</span> press play</div>
    <div class="needs" id="needs"><div class="h">⚠ Needs you</div><div id="needs-b" style="font-size:13px;margin-top:4px"></div></div>
  </div>
  <div class="col spine" id="spine"><h2>Runtime spine</h2>
    <div class="node" data-node="discovery"><div class="t">Discovery</div><div class="d">intent · entities · missing evidence</div></div>
    <div class="node" data-node="planner"><div class="t">Execution Planner</div><div class="d">one governed mission</div></div>
    <div class="node" data-node="mission"><div class="t">Mission Runtime</div><div class="d">state · approvals · saga</div></div>
    <div class="node" data-node="context"><div class="t">Context Runtime</div><div class="d">evidence path · cost/quality</div></div>
    <div class="node" data-node="verify"><div class="t">Verification</div><div class="d">verified state transition</div></div>
  </div>
  <div class="col" id="blocks"><h2>Business systems</h2>
    <div class="block rev"><div class="bt">Revenue &amp; Intelligence</div>
      <span class="chip" data-node="intake">intake</span><span class="chip" data-node="crm">CRM</span><span class="chip" data-node="pricing">outreach</span></div>
    <div class="block cs"><div class="bt">Customer Success</div>
      <span class="chip" data-node="crm">support</span><span class="chip" data-node="lifecycle">lifecycle</span></div>
    <div class="block fin"><div class="bt">Finance</div>
      <span class="chip" data-node="pricing">pricing</span><span class="chip" data-node="lago">billing</span><span class="chip" data-node="erpnext">books</span></div>
    <div class="block sec"><div class="bt">Security &amp; Compliance</div>
      <span class="chip" data-node="gleif">GLEIF</span><span class="chip" data-node="opensanctions">sanctions</span></div>
  </div>
  <div id="capsule">◆ capsule</div>
</div>

<div class="panels">
  <div class="card"><h3>Outcome</h3>
    <div class="kpi" id="kpi"></div>
    <div id="outcome" class="mono small" style="margin-top:10px"></div>
    <div id="projections" class="mono small" style="margin-top:8px;color:var(--faint)"></div></div>
  <div class="card"><h3>Why the Runtime — counterfactual</h3>
    <div class="scroll" style="overflow-x:auto"><table id="score"><thead><tr><th>Arm</th><th>Outcome</th><th>To outcome</th><th>Guesses</th><th></th></tr></thead><tbody></tbody></table></div></div>
</div>

<script>
const blockOf={intake:'rev',crm:'rev',pricing:'fin',lago:'fin',erpnext:'fin',lifecycle:'cs',
  gleif:'sec',opensanctions:'sec',discovery:'spine',planner:'spine',mission:'spine',context:'spine',verify:'spine'};
const RB={'REAL-LIVE':'b-live','REAL-SNAPSHOT':'b-snap','SEEDED-DEMO':'b-seed','SYNTHETIC':'b-syn','SIMULATED':'b-sim','CANNED':'b-sim','STUB':'b-sim'};
let DATA=null, timer=null;

function badge(r){const c=RB[r]||'b-sim';return `<span class="badge ${c}">${r}</span>`;}
function clearHi(){document.querySelectorAll('.node.on,.chip.on').forEach(e=>e.classList.remove('on'));}
function moveCapsule(node){
  const el=document.querySelector(`[data-node="${node}"]`); const cap=document.getElementById('capsule');
  const cv=document.getElementById('canvas').getBoundingClientRect();
  if(!el){cap.style.opacity=0;return;} const r=el.getBoundingClientRect();
  cap.style.opacity=1;
  cap.style.transform=`translate(${r.left-cv.left+r.width/2-40}px,${r.top-cv.top+8}px)`;
}
function lite(node){document.querySelectorAll(`[data-node="${node}"]`).forEach(e=>e.classList.add('on'));}

async function load(){
  const w=document.getElementById('world').value;
  DATA=await (await fetch('/api/worlds/run?world='+encodeURIComponent(w))).json();
  const d=DATA.descriptor;
  document.getElementById('realism').innerHTML=badge(d.realism);
  document.getElementById('sources').textContent=d.datasources.join(' · ');
  const first=DATA.trace.milestones[0];
  document.getElementById('ev-title').textContent=first?first.label:'—';
  document.getElementById('ev-desc').textContent=d.title;
  document.getElementById('timer').textContent='00:00';document.getElementById('timer').classList.remove('done');
  clearHi();document.getElementById('capsule').style.opacity=0;
  document.getElementById('now').innerHTML='<span class="k">ready —</span> press play';
  document.getElementById('needs').classList.remove('show');
  renderPanels();
}
function play(){
  if(!DATA)return; clearTimeout(timer); clearHi();
  const ms=DATA.trace.milestones, btn=document.getElementById('play'); btn.disabled=true;
  document.getElementById('needs').classList.remove('show');
  let i=0;
  const step=()=>{
    if(i>=ms.length){btn.disabled=false;const t=document.getElementById('timer');t.classList.add('done');return;}
    const m=ms[i]; clearHi();
    const secs=String(Math.floor(m.t_offset_s)).padStart(2,'0');
    document.getElementById('timer').textContent='00:'+secs;
    lite(m.node||({event:'intake'}[m.kind]||'discovery'));
    ['discovery','plan','event','needs_you','verify'].includes(m.kind)&&m.block==='runtime'&&lite({discovery:'discovery',plan:'planner',event:'discovery',needs_you:'mission',verify:'verify'}[m.kind]);
    moveCapsule(m.node|| (m.block==='runtime'?({discovery:'discovery',plan:'planner',needs_you:'mission',verify:'verify',policy:'mission'}[m.kind]||'mission'):'crm'));
    const nowEl=document.getElementById('now');
    nowEl.innerHTML=`<span class="k">${m.kind}</span> · ${m.label} ${m.realism?badge(m.realism):''}`;
    const nd=document.getElementById('needs');
    if(m.needs_you){nd.classList.add('show');document.getElementById('needs-b').textContent=m.label+'  ('+m.needs_you+')';}
    else nd.classList.remove('show');
    i++;
    const dt=i<ms.length?(ms[i].t_offset_s-m.t_offset_s):1;
    timer=setTimeout(step, Math.max(300, dt*220));
  };
  step();
}
function renderPanels(){
  const mt=DATA.metrics;
  document.getElementById('kpi').innerHTML=[
    ['verified', mt.verified?'✓':'✗'],['to outcome', mt.time_to_outcome_s+'s'],
    ['revenue', '$'+(mt.revenue_value||0).toLocaleString()],['guesses', mt.unsupported_guesses],
    ['human decisions', mt.human_decisions]
  ].map(([l,v])=>`<div><div class="v">${v}</div><div class="l">${l}</div></div>`).join('');
  document.getElementById('outcome').textContent=JSON.stringify(DATA.outcome);
  const pr=DATA.projections||[];
  document.getElementById('projections').innerHTML=pr.length
    ? 'projected into '+pr.map(p=>p.app+' <span style="opacity:.65">['+p.realism+']</span>').join(' · ')
    : '';
  const tb=document.querySelector('#score tbody');
  tb.innerHTML=DATA.scorecard.arms.map(a=>{
    const t=a.metrics.time_to_outcome_s; const tt=t>=3600?(Math.round(t/3600)+'h'):(t+'s');
    return `<tr><td>${a.arm.replace('_',' ')}</td><td class="${a.outcome_reached?'ok':'no'}">${a.outcome_reached?'reached':'—'}</td>
      <td class="mono">${tt}</td><td class="mono">${a.metrics.unsupported_guesses}</td>
      <td class="mono small" style="color:var(--faint)">${a.notes}</td></tr>`;}).join('');
}
(async()=>{
  const ws=(await (await fetch('/api/worlds')).json()).worlds;
  const sel=document.getElementById('world');
  sel.innerHTML=ws.map(w=>`<option value="${w.world_id}">${w.title}</option>`).join('');
  sel.onchange=load;
  // deep-link: /worlds?world=<id> (e.g. "see it run" from an Attention item) pre-selects + auto-plays it
  const want=new URLSearchParams(location.search).get('world');
  if(want && ws.some(w=>w.world_id===want)) sel.value=want;
  await load();
  if(want) play();
})();
</script></div></body></html>"""

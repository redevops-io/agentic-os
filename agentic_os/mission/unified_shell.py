"""The minimal unified-desktop shell (P2) — a single self-contained React page.

Intentionally only FIVE surfaces: Sidekick, Needs You, Mission progress, Evidence, Action receipt. No
navigation, no CRM screens, no settings, no per-app dashboards, and no provider vocabulary — the shell reads
only ReDevOps mission/attention/receipt contracts from unified_server. It never sees or requests a
credential; connection status arrives as "Connected", the key stays with the broker.

React is loaded (UMD, no build step) from cdnjs; the page degrades to a clear message if offline.
"""

SHELL_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ReDevOps Sidekick</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--panel2:#1d222b;--line:#272d38;--fg:#e6e9ef;--mut:#9aa3b2;
--accent:#5eead4;--accent-ink:#04201c;--ok:#5bd98a;--warn:#f5b544;--danger:#f87171;
--font:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;--mono:ui-monospace,"SF Mono",Menlo,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--font)}
.wrap{max-width:1120px;margin:0 auto;padding:20px;display:flex;flex-direction:column;gap:16px}
header{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:10px;font-weight:600;font-size:17px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--accent)}
.conns{display:flex;gap:8px;flex-wrap:wrap}
.chip{display:flex;align-items:center;gap:7px;background:var(--panel);border:1px solid var(--line);
border-radius:999px;padding:6px 12px;font-size:12.5px;color:var(--mut)}
.chip .s{width:7px;height:7px;border-radius:50%;background:var(--ok)}
.chip .s.off{background:var(--warn)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:820px){.grid{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px}
.card h2{margin:0 0 4px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:var(--mut)}
.card .hint{color:var(--mut);font-size:12px;margin:0 0 12px}
.sk-log{display:flex;flex-direction:column;gap:10px;margin-bottom:12px;max-height:220px;overflow:auto}
.msg{padding:10px 12px;border-radius:10px;font-size:13.5px;line-height:1.45}
.msg.you{background:var(--panel2);align-self:flex-end;max-width:85%}
.msg.sk{background:#12302b;color:#c8f7ee;align-self:flex-start;max-width:90%;border:1px solid #1c463f}
.row{display:flex;gap:8px}
input[type=text]{flex:1;background:var(--panel2);border:1px solid var(--line);color:var(--fg);
border-radius:10px;padding:11px 13px;font:inherit;font-size:14px}
button{font:inherit;font-weight:600;border:0;border-radius:10px;padding:10px 15px;cursor:pointer}
.primary{background:var(--accent);color:var(--accent-ink)}
.approve{background:var(--ok);color:#053018}.reject{background:transparent;color:var(--danger);
border:1px solid var(--danger)}
.empty{color:var(--mut);font-size:13px;padding:8px 0}
.att{border:1px solid var(--warn);border-radius:12px;padding:13px;background:#241d07;margin-bottom:10px}
.att .t{font-weight:600;font-size:14px}.att .w{color:#e8d9ac;font-size:12.5px;margin:6px 0 11px}
.att .acts{display:flex;gap:8px}
.steps{display:flex;flex-direction:column;gap:8px}
.step{display:flex;align-items:center;gap:10px;font-size:13.5px}
.badge{font:600 11px/1 var(--mono);padding:4px 8px;border-radius:6px;background:var(--panel2);color:var(--mut)}
.badge.done{background:#0f3d22;color:var(--ok)}.badge.wait{background:#4a3500;color:var(--warn)}
.badge.run{background:#0e2a4a;color:#7cc4ff}
.mono{font-family:var(--mono);font-size:12px;color:var(--mut);white-space:pre-wrap;word-break:break-word}
.state{display:inline-block;padding:5px 11px;border-radius:999px;font-size:12.5px;font-weight:600}
.state.SUCCEEDED{background:#0f3d22;color:var(--ok)}.state.WAITING_HUMAN{background:#4a3500;color:var(--warn)}
.state.RUNNING,.state.PLANNING{background:#0e2a4a;color:#7cc4ff}.state.FAILED{background:#3a1414;color:var(--danger)}
.ev{border-left:2px solid var(--line);padding:6px 0 6px 12px;margin-bottom:6px;font-size:12.5px}
.ev b{color:var(--fg)}
.rc-line{display:flex;justify-content:space-between;gap:10px;font-size:13px;padding:5px 0;border-top:1px solid var(--line)}
.rc-line:first-child{border-top:0}
.note{color:var(--mut);font-size:11.5px;margin-top:10px}
.offline{padding:40px;text-align:center;color:var(--mut)}
</style></head>
<body><div id="root"><div class="offline">Loading the ReDevOps shell…</div></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/react/18.3.1/umd/react.production.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.3.1/umd/react-dom.production.min.js"></script>
<script>
const {useState,useEffect,useCallback}=React, h=React.createElement;
const api=(p,o)=>fetch(p,o).then(r=>r.json());

function Chip({c}){return h('div',{className:'chip'},h('span',{className:'s'+(c.connected?'':' off')}),
  c.domain+' — '+(c.connected?'Connected':'Not connected'));}

function Sidekick({onMission}){
  const [log,setLog]=useState([{who:'sk',text:'Ask me to run cross-domain work. Try: “sync the pilot account from revenue into support”.'}]);
  const [text,setText]=useState('');
  const send=async()=>{ if(!text.trim())return; const you=text; setText('');
    setLog(l=>[...l,{who:'you',text:you}]);
    const r=await api('/api/sidekick',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:you})});
    setLog(l=>[...l,{who:'sk',text:r.reply}]); if(r.mission_id)onMission(r.mission_id); };
  return h('div',{className:'card'},
    h('h2',null,'Sidekick'),h('p',{className:'hint'},'Natural-language intent → a governed Mission.'),
    h('div',{className:'sk-log'},log.map((m,i)=>h('div',{key:i,className:'msg '+m.who},m.text))),
    h('div',{className:'row'},
      h('input',{type:'text',value:text,placeholder:'Describe the work…',
        onChange:e=>setText(e.target.value),onKeyDown:e=>e.key==='Enter'&&send()}),
      h('button',{className:'primary',onClick:send},'Send')));
}

function NeedsYou({items,onApprove}){
  return h('div',{className:'card'},
    h('h2',null,'Needs You'),h('p',{className:'hint'},'Approvals, exceptions and high-risk decisions — one inbox.'),
    items.length===0?h('div',{className:'empty'},'Nothing needs you right now.'):
    items.map((it,i)=>h('div',{className:'att',key:i},
      h('div',{className:'t'},it.title),
      h('div',{className:'w'},it.why),
      h('div',{className:'acts'},
        h('button',{className:'approve',onClick:()=>onApprove(it,'approve')},'Approve'),
        h('button',{className:'reject',onClick:()=>onApprove(it,'reject')},'Reject')))));
}

function Mission({m}){
  if(!m)return h('div',{className:'card'},h('h2',null,'Mission'),h('div',{className:'empty'},'No active mission yet.'));
  const bcls=s=>s==='DONE'?'badge done':s==='WAITING'?'badge wait':(s==='RUNNING'||s==='READY')?'badge run':'badge';
  const steps=Object.entries(m.node_status||{});
  return h('div',{className:'card'},
    h('h2',null,'Mission'),
    h('div',{style:{marginBottom:'6px'}},h('span',{className:'state '+(m.state||'').toUpperCase()},m.state)),
    h('div',{className:'hint',style:{margin:'6px 0 12px'}},m.goal),
    h('div',{className:'steps'},steps.map(([nid,st],i)=>h('div',{className:'step',key:i},
      h('span',{className:bcls(st)},st),h('span',{className:'mono'},nid.slice(0,18))))));
}

function Evidence({ev}){
  return h('div',{className:'card'},h('h2',null,'Evidence'),
    (!ev||ev.length===0)?h('div',{className:'empty'},'No evidence recorded yet.'):
    ev.map((e,i)=>h('div',{className:'ev',key:i},h('b',null,(e.type||'record')+' '),
      h('span',{className:'mono'},JSON.stringify(Object.fromEntries(Object.entries(e).filter(([k])=>k!=='type'))).slice(0,160)))));
}

function Receipt({rc}){
  if(!rc||!rc.outcome)return h('div',{className:'card'},h('h2',null,'Action Receipt'),
    h('div',{className:'empty'},'The receipt appears once the Mission completes.'));
  return h('div',{className:'card'},h('h2',null,'Action Receipt'),
    h('div',{className:'rc-line'},h('span',null,'Outcome'),h('b',null,rc.outcome.success?'Success':'Failed')),
    (rc.steps||[]).map((s,i)=>h('div',{className:'rc-line',key:i},h('span',null,s.capability),
      h('span',{className:'mono'},JSON.stringify(s.result).slice(0,60)))),
    (rc.approvals||[]).map((a,i)=>h('div',{className:'rc-line',key:'a'+i},h('span',null,'Approved'),
      h('b',null,a.capability))),
    h('div',{className:'note'},rc.credential));
}

function App(){
  const [conns,setConns]=useState([]);
  const [mid,setMid]=useState(null);
  const [mission,setMission]=useState(null);
  const [needs,setNeeds]=useState([]);
  const [ev,setEv]=useState([]);
  const [rc,setRc]=useState(null);

  useEffect(()=>{api('/api/connections').then(d=>setConns(d.connections||[]));},[]);
  const refresh=useCallback(async()=>{
    const nu=await api('/api/needs-you'); setNeeds(nu.items||[]);
    if(mid){ setMission(await api('/api/missions/'+mid));
      setEv((await api('/api/missions/'+mid+'/evidence')).evidence||[]);
      setRc(await api('/api/missions/'+mid+'/receipt')); }
  },[mid]);
  useEffect(()=>{refresh(); const t=setInterval(refresh,1500); return ()=>clearInterval(t);},[refresh]);

  const approve=async(it,decision)=>{
    await api('/api/missions/'+it.mission_id+'/approve',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({node_id:it.node_id,decision})});
    if(!mid)setMid(it.mission_id); refresh();
  };

  return h('div',{className:'wrap'},
    h('header',null,
      h('div',{className:'brand'},h('span',{className:'dot'}),'ReDevOps Sidekick'),
      h('div',{className:'conns'},conns.map((c,i)=>h(Chip,{c,key:i})))),
    h('div',{className:'note',style:{marginTop:'-6px'}},
      'Connections show status only — credentials are broker-managed and never reach this screen.'),
    h('div',{className:'grid'},
      h('div',{style:{display:'flex',flexDirection:'column',gap:'16px'}},
        h(Sidekick,{onMission:setMid}), h(NeedsYou,{items:needs,onApprove:approve})),
      h('div',{style:{display:'flex',flexDirection:'column',gap:'16px'}},
        h(Mission,{m:mission}), h(Evidence,{ev}), h(Receipt,{rc}))));
}

if(window.React&&window.ReactDOM){ReactDOM.createRoot(document.getElementById('root')).render(h(App));}
else{document.getElementById('root').innerHTML='<div class=offline>Could not load React (offline?). The API still works at /api/*.</div>';}
</script></body></html>
"""

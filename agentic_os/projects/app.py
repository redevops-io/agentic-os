"""A running Projects UI over canonical Missions/Artifacts — the plan's §20 slice, served.

Generic workspace (not app-specific): lists missions, renders a mission's artifacts with INDEPENDENT
status, shows the pending HumanRequest with its consequence BEFORE approval, and posts a Decision over the
EXACT selected artifacts → governed publish → ActionReceipts. Single self-contained page (no external
build), backed by the same canonical objects Sidekick would reference.
"""
from __future__ import annotations

from typing import Optional

from .content_service import ProjectsContentService
from .contracts import WorkflowDefinition


class ProjectsServer:
    def __init__(self, *, project_id: str = "content-studio", project_name: str = "Content Studio"):
        self.project_id = project_id
        self.project_name = project_name
        self.services: dict[str, ProjectsContentService] = {}
        # learned/authored workflows for this project (Workflow-Teaching plan §11)
        self.workflows: dict[str, WorkflowDefinition] = {}

    def add(self, svc: ProjectsContentService) -> ProjectsContentService:
        self.services[svc.mission_id] = svc
        return svc

    def add_workflow(self, wf: WorkflowDefinition) -> WorkflowDefinition:
        self.workflows[wf.workflow_id] = wf
        return wf

    def missions(self) -> list[dict]:
        out = []
        for s in self.services.values():
            v = s.mission_view()
            out.append({**v["mission"], "counts": v["mission"]["summary"]})
        return out

    def workflow_rows(self) -> list[dict]:
        """§11 workflow list: title, status, trigger, versions, gate/unresolved counts."""
        rows = []
        for w in self.workflows.values():
            rows.append({"id": w.workflow_id, "title": w.title, "status": w.status.value,
                         "trigger": w.trigger, "version": w.version, "rules": len(w.rules),
                         "gates": len(w.human_gates), "unresolved": len(w.unresolved_questions)})
        return rows


def create_projects_app(server: ProjectsServer):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse

    app = FastAPI(title="ReDevOps Projects")

    @app.get("/api/projects")
    def _projects():
        return [{"id": server.project_id, "name": server.project_name}]

    @app.get("/api/missions")
    def _missions():
        return server.missions()

    @app.get("/api/missions/{mid}")
    def _mission(mid: str):
        s = server.services.get(mid)
        if not s:
            raise HTTPException(404, "mission not found")
        return s.mission_view()

    @app.post("/api/missions/{mid}/decide")
    def _decide(mid: str, body: dict):
        s = server.services.get(mid)
        if not s:
            raise HTTPException(404, "mission not found")
        action = (body or {}).get("action", "approve")
        selected = tuple((body or {}).get("selected_ids", []))
        return s.decide((body or {}).get("actor", "owner"), action, selected)

    @app.get("/api/workflows")
    def _workflows():
        return server.workflow_rows()

    @app.get("/api/workflows/{wid}")
    def _workflow(wid: str):
        w = server.workflows.get(wid)
        if not w:
            raise HTTPException(404, "workflow not found")
        return w.to_dict()

    @app.get("/", response_class=HTMLResponse)
    def _index():
        return _PAGE

    return app


_PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>ReDevOps Projects</title>
<style>
:root{--bg:#f4f6f9;--panel:#fff;--ink:#151a22;--muted:#5b6675;--faint:#8a94a3;--line:#e3e8ef;
--line2:#d2d9e3;--accent:#2456e6;--accentsoft:#eaf0ff;--ok:#0e9f6e;--oksoft:#e6f6ef;--warn:#b7791f;
--warnsoft:#fbf3e2;--bad:#c0392b;--mono:'IBM Plex Mono',ui-monospace,monospace;
--body:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--panel:#161b22;--ink:#e7ecf3;--muted:#9aa6b5;
--faint:#6b7686;--line:#232b36;--line2:#303a48;--accent:#5b82ff;--accentsoft:#182238;--ok:#35c793;
--oksoft:#10241d;--warn:#e0a83e;--warnsoft:#2a2114;--bad:#f0776a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--body);line-height:1.5}
.top{border-bottom:1px solid var(--line);padding:14px 22px;display:flex;gap:12px;align-items:center;background:var(--panel)}
.top b{font-weight:700}.top .mono{font-family:var(--mono);font-size:12px;color:var(--faint)}
.wrap{display:grid;grid-template-columns:260px 1fr;min-height:calc(100vh - 51px)}
.side{border-right:1px solid var(--line);padding:16px;background:var(--panel)}
.side h3{font-family:var(--mono);font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);margin:0 0 10px}
.mrow{padding:10px 12px;border:1px solid var(--line);border-radius:10px;margin-bottom:8px;cursor:pointer}
.mrow:hover{border-color:var(--line2)}.mrow.sel{border-color:var(--accent);background:var(--accentsoft)}
.mrow .t{font-weight:600;font-size:14px}.mrow .s{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:3px}
.main{padding:26px 30px;max-width:900px}
.hgoal{font-size:22px;font-weight:700;margin:0 0 6px}.hsub{color:var(--muted);margin:0 0 16px}
.pill{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);font-size:11px;font-weight:500;
padding:4px 10px;border-radius:999px;border:1px solid var(--line2)}
.pill.ready{background:var(--accentsoft);color:var(--accent);border-color:transparent}
.pill.held{background:var(--warnsoft);color:var(--warn);border-color:transparent}
.pill.pub{background:var(--oksoft);color:var(--ok);border-color:transparent}
.pill.fail{background:#fdecea;color:var(--bad);border-color:transparent}
.lab{font-family:var(--mono);font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--faint);
margin:26px 0 10px;display:flex;gap:10px;align-items:center}.lab::after{content:"";flex:1;height:1px;background:var(--line)}
.art{border:1px solid var(--line);border-radius:12px;background:var(--panel);margin-bottom:12px;overflow:hidden}
.art .h{display:flex;align-items:center;gap:10px;padding:12px 15px;border-bottom:1px solid var(--line)}
.art .h .ty{font-weight:600;font-size:14px}.art .h .ch{font-family:var(--mono);font-size:11px;color:var(--faint)}
.art .b{padding:14px 16px;white-space:pre-wrap;font-size:15px}
.art .f{display:flex;gap:8px;align-items:center;padding:10px 15px;border-top:1px solid var(--line)}
.art .f .sp{flex:1}
.btn{font-family:var(--body);font-size:13px;font-weight:500;padding:6px 13px;border-radius:8px;
border:1px solid var(--line2);background:var(--panel);color:var(--ink);cursor:pointer}
.btn:hover{border-color:var(--accent)}.btn.pri{background:var(--accent);color:#fff;border-color:transparent}
.btn.pri:disabled{opacity:.5;cursor:not-allowed}.btn.ghost{color:var(--muted)}
.chk{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted)}
.reason{color:var(--warn);font-size:13px;font-family:var(--mono);padding:0 16px 12px}
.decision{border:1px solid var(--line2);border-radius:12px;background:var(--panel);padding:16px 18px;margin-top:8px}
.decision .con{background:var(--warnsoft);color:var(--warn);border-radius:8px;padding:9px 12px;font-size:13px;margin:8px 0 12px}
.rcpt{font-family:var(--mono);font-size:12px;padding:8px 12px;border:1px solid var(--line);border-radius:8px;margin-bottom:6px}
.rcpt a{color:var(--accent)}.ev{font-family:var(--mono);font-size:12px;color:var(--muted)}
.empty{color:var(--faint);font-size:14px}
.prov{display:inline-block;font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:.04em;
padding:2px 7px;border-radius:6px;margin-right:8px;vertical-align:middle}
.prov.OBSERVED{background:var(--oksoft);color:var(--ok)}
.prov.HUMAN_CONFIRMED{background:var(--oksoft);color:var(--ok)}
.prov.POLICY_DEFINED{background:var(--accentsoft);color:var(--accent)}
.prov.INFERRED{background:var(--warnsoft);color:var(--warn)}
.prov.UNKNOWN{background:#fdecea;color:var(--bad)}
.rule{border:1px solid var(--line);border-radius:10px;padding:11px 14px;margin-bottom:9px;background:var(--panel)}
.rule .st{font-size:14px}.rule .m{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:4px}
.q{border:1px solid var(--line2);border-left:3px solid var(--warn);border-radius:8px;background:var(--warnsoft);
color:var(--warn);padding:9px 12px;font-size:13px;margin-bottom:8px}
</style></head><body>
<div class=top><b>ReDevOps Projects</b><span class=mono id=projname></span><span class=mono style="margin-left:auto" id=note></span></div>
<div class=wrap>
  <div class=side><h3>Missions</h3><div id=mlist></div>
    <h3 style="margin-top:22px">Workflows</h3><div id=wlist></div></div>
  <div class=main id=main><p class=empty>Select a mission.</p></div>
</div>
<script>
let CUR=null;
const j=(u,o)=>fetch(u,o).then(r=>r.json());
const pill=s=>{const m={READY:'ready',HELD:'held',PUBLISHED:'pub',SENT:'pub',FAILED:'fail',REJECTED:'fail',EXECUTING:'ready'};
  return `<span class="pill ${m[s]||''}">${s}</span>`;};
async function boot(){const p=await j('/api/projects');document.getElementById('projname').textContent='· '+(p[0]?.name||'');loadList();loadWf();}
async function loadWf(){const ws=await j('/api/workflows');const el=document.getElementById('wlist');
  if(!ws.length){el.innerHTML='<p class=empty>No workflows yet. Teach one by demonstrating it.</p>';return;}
  el.innerHTML=ws.map(w=>`<div class="mrow ${w.id===CUR?'sel':''}" onclick="openWf('${w.id}')">
    <div class=t>${w.title}</div><div class=s>${w.status} · v${w.version} · ${w.rules} rules${w.unresolved?` · ⚠ ${w.unresolved} open`:''}</div></div>`).join('');}
async function openWf(wid){CUR=wid;loadList();loadWf();const w=await j('/api/workflows/'+wid);renderWf(w);}
function renderWf(w){
  const rules=(w.rules||[]).map(r=>`<div class=rule><div class=st><span class="prov ${r.provenance}">${r.provenance}</span>${r.statement}</div>
    <div class=m>${r.intent}${r.gate&&r.gate!=='G0'?' · gate '+r.gate:''}${r.confidence?' · conf '+r.confidence.toFixed(2):''}</div></div>`).join('')||'<span class=empty>no rules</span>';
  const qs=(w.unresolved_questions||[]).map(q=>`<div class=q>? ${q}</div>`).join('');
  const lf=w.learned_from||{};const src=[].concat(lf.recording_refs||[],lf.mission_refs||[],lf.decision_refs||[]).length;
  document.getElementById('main').innerHTML=
    `<p class=hgoal>${w.title}</p><p class=hsub>${w.trigger||''}</p>
     <div>${pill(w.status)} <span class=ev>v${w.version}${w.parent_version_id?' · from '+w.parent_version_id:''}${src?' · learned from '+src+' source(s)':''}</span></div>
     <div class=lab>Steps &amp; rules — what is observed, inferred, confirmed</div>${rules}
     ${qs?`<div class=lab>Unresolved — needs your answer before it can run</div>${qs}`:''}`;
}
async function loadList(){const ms=await j('/api/missions');const el=document.getElementById('mlist');
  if(!ms.length){el.innerHTML='<p class=empty>No missions.</p>';return;}
  el.innerHTML=ms.map(m=>`<div class="mrow ${m.id===CUR?'sel':''}" onclick="open_('${m.id}')"><div class=t>${m.title}</div><div class=s>${m.summary}</div></div>`).join('');}
async function open_(mid){CUR=mid;loadList();const v=await j('/api/missions/'+mid);render(v);}
function render(v){
  const arts=v.artifacts;
  const outputs=arts.map(a=>{
    const ready=a.status==='READY';
    const sel=ready?`<label class=chk><input type=checkbox class=selbox value="${a.artifact_id}" checked> publish</label>`:'';
    const acts=(a.allowed_actions||[]).map(x=>`<button class="btn ghost" onclick="alert('${x}: wired to Sidekick edit/version + regenerate in the full build')">${x}</button>`).join('');
    const reason=a.hold_reason?`<div class=reason>⚠ ${a.hold_reason}</div>`:'';
    let body;
    if(a.type==='Video') body=a.preview_ref?`<div class=b><video src="${a.preview_ref}" controls preload=metadata style="max-width:300px;border-radius:8px;background:#000"></video></div>`:`<div class="b empty">9:16 video · ${a.summary} · preview path unavailable</div>`;
    else if(a.type==='Image') body=a.preview_ref?`<div class=b><img src="${a.preview_ref}" alt="hero" style="max-width:300px;border-radius:8px"></div>`:`<div class="b empty">image · preview unavailable</div>`;
    else body=`<div class=b>${a.content||''}</div>`;
    return `<div class=art><div class=h><span class=ty>${a.title}</span><span class=ch>${a.subtype}</span><span style=flex:1></span>${pill(a.status)}</div>
      ${body}${reason}<div class=f>${sel}<span class=sp></span>${acts}</div></div>`;
  }).join('');
  const nd=v.needs_decision;
  let decision='';
  if(nd){decision=`<div class=lab>Needs your decision</div><div class=decision>
    <div>${nd.prompt}</div><div class=con>⚠ ${nd.consequences}</div>
    <button class="btn pri" id=pubbtn onclick="decide('approve')">Approve &amp; publish selected</button>
    <button class="btn" onclick="decide('reject')">Reject</button></div>`;}
  const rcpts=(v.receipts||[]).map(r=>`<div class=rcpt>${r.provider} · ${r.status} ${r.external_url?`· <a href="${r.external_url}" target=_blank>${r.external_url}</a>`:''}${r.error?' · '+r.error:''}</div>`).join('')||'<span class=empty>none yet</span>';
  const ev=(v.inputs||[]).map(i=>`<div class=ev>${i.ref}</div>`).join('')||'<span class=empty>—</span>';
  document.getElementById('main').innerHTML=
    `<p class=hgoal>${v.mission.title}</p><p class=hsub>${v.mission.goal}</p>
     <div>${pill('READY')} ${v.mission.summary}</div>
     <div class=lab>Outputs</div>${outputs}
     ${decision}
     <div class=lab>Actions / Receipts</div>${rcpts}
     <div class=lab>Inputs &amp; evidence (lineage)</div>${ev}`;
}
async function decide(action){
  const ids=[...document.querySelectorAll('.selbox:checked')].map(x=>x.value);
  if(action==='approve'&&!ids.length){alert('Select at least one ready output to publish.');return;}
  const btn=document.getElementById('pubbtn');if(btn){btn.disabled=true;btn.textContent='Publishing…';}
  const v=await j('/api/missions/'+CUR+'/decide',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({actor:'owner',action,selected_ids:ids})});
  render(v);loadList();
}
boot();
</script></body></html>"""

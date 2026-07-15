"""agentic-guide — the onboarding assistant that walks people through every app.

A RAG help bot over the stack's own docs (the redevops-rag pattern): ask "how do I use agentic-
billing?" or "walk me through Outreach Engine" and it retrieves the relevant app cards and answers
with citations — free Q&A **and** guided per-app walkthroughs (what it does → open dashboard →
settings → first action → what's approval-gated).

RBAC-native: every answer is filtered by the caller's role, so the guide only surfaces the apps a
user is allowed to see — the natural front-end for the v-next Context Runtime RBAC layer (same
principal → same visibility here and in the fleet).

  GET  /health              -> {"status","core":"redevops-rag","connected":true}
  POST /api/ask             -> {"question","role"} → answer + cited apps
  GET  /api/walkthrough     -> ?app=...&role=... → structured steps
  GET  /                    -> MD3 chat dashboard (role selector · ask · walkthroughs)
"""
from __future__ import annotations

import html
import json
import os
import urllib.request

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

# --- config ------------------------------------------------------------------
# The redevops-rag corpus, RBAC filter, retrieval + walkthrough all live in core.py
# (pure — no web / context-runtime deps) so the Mission Runtime operator can invoke them
# and they can be tested without booting the console. The LLM narration stays here (as an
# optional callback into core.answer); core is fully deterministic with llm=None.
from . import core
from .core import (  # noqa: E402
    APP_DOCS, ROLES, DEMO, visible_apps, doc_text, retrieve, walkthrough,
)

PORT = int(os.environ.get("PORT", "8215"))
LLM_BASE = os.environ.get("REDEVOPS_LLM_BASE_URL", "")
LLM_MODEL = os.environ.get("REDEVOPS_LLM_MODEL", "")


# --- optional LLM narration (guarded: works without any LLM configured) -------
def _llm(prompt: str) -> str | None:
    if not LLM_BASE:
        return None
    try:
        body = json.dumps({"model": LLM_MODEL or "default", "max_tokens": 500, "temperature": 0.3,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request(LLM_BASE.rstrip("/") + "/chat/completions", data=body,
                                     headers={"content-type": "application/json"})
        out = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
        return out["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def answer(question: str, role: str) -> dict:
    """Answer over the redevops-rag corpus, with the optional LLM narration blurb."""
    return core.answer(question, role, llm=_llm)


app = FastAPI(title="agentic-guide (core: redevops-rag)")


@app.get("/health")
def health():
    return {"status": "up", "core": "redevops-rag", "connected": True}


@app.post("/api/ask")
async def api_ask(req: Request):
    b = await req.json()
    return JSONResponse(answer(b.get("question", ""), b.get("role", "admin")))


@app.get("/api/walkthrough")
def api_walkthrough(app: str, role: str = "admin"):
    if app not in visible_apps(role):
        return JSONResponse({"error": f"'{app}' is not visible to role '{role}'"}, status_code=403)
    return JSONResponse(walkthrough(app))


@app.get("/", response_class=HTMLResponse)
def dashboard(role: str = "admin"):
    return HTMLResponse(render(role))


# ── MD3 chat dashboard ──
def _esc(v) -> str:
    return html.escape(str(v))


def render(role: str) -> str:
    apps = visible_apps(role)
    roles = "".join(f'<option value="{r}"{" selected" if r == role else ""}>{r}</option>' for r in ROLES)
    cards = ""
    for n in apps:
        d = APP_DOCS[n]
        cards += (f'<div class=app><div class=arow><b>{_esc(n)}</b><span class=chip>{_esc(d["core"])}</span>'
                  f'<span class=grp>{_esc(d["g"])}</span></div><div class=aw>{_esc(d["what"])}</div>'
                  f'<button class=wbtn onclick="walk(\'{_esc(n)}\')">▶ Walk me through it</button></div>')
    return f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>App Guide · Context Runtime</title>
<link rel=stylesheet href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;600&family=Roboto+Mono&display=swap">
<style>
:root{{--s:#131316;--sc:#1f1f23;--sch:#2a2a2e;--ov:#2f2f33;--on:#e4e2e6;--onm:#918f96;--p:#4fd1c5;--op:#00201c;--pc:#00504a;--opc:#a8f0e6;--sec:#f5b544;--r:16px;--f:"Roboto",system-ui,sans-serif;--m:ui-monospace,monospace}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--s);color:var(--on);font-family:var(--f);padding:24px}}
.shell{{max-width:1080px;margin:0 auto;display:flex;flex-direction:column;gap:18px}}
.top{{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}}
h1{{font:500 24px/32px var(--f);margin:0}}.sub{{color:var(--onm);font-size:13px;margin-top:2px}}
select{{background:var(--sch);color:var(--on);border:1px solid var(--ov);border-radius:999px;padding:8px 14px;font:500 13px var(--f)}}
.rbac{{font:500 12px var(--m);color:var(--sec);background:#4a3500;padding:6px 12px;border-radius:999px}}
.card{{background:var(--sc);border:1px solid var(--ov);border-radius:var(--r);padding:20px}}
.ask{{display:flex;gap:10px}}
input{{flex:1;background:var(--s);color:var(--on);border:1px solid var(--ov);border-radius:12px;padding:12px 15px;font:400 14px var(--f)}}
input:focus{{outline:none;border-color:var(--p)}}
.btn{{background:var(--p);color:var(--op);border:0;border-radius:999px;padding:11px 20px;font:600 14px var(--f);cursor:pointer}}
#ans{{margin-top:14px;white-space:pre-wrap;line-height:1.6;font-size:14px}}
.cite{{margin-top:10px;font:500 12px var(--m);color:var(--p)}}
.apps{{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}}
.app{{background:var(--sc);border:1px solid var(--ov);border-radius:12px;padding:14px}}
.arow{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
.chip{{font:500 11px var(--m);background:var(--sch);color:var(--onm);padding:3px 8px;border-radius:6px}}
.grp{{font:500 10px var(--m);color:var(--p);margin-left:auto;text-transform:uppercase;letter-spacing:.5px}}
.aw{{color:var(--onm);font-size:12.5px;margin:8px 0}}
.wbtn{{background:var(--sch);color:var(--p);border:1px solid var(--pc);border-radius:999px;padding:7px 13px;font:500 12px var(--f);cursor:pointer}}
h2{{font:500 16px var(--f);margin:6px 0}}.foot{{color:var(--onm);font-size:12px}}
</style></head><body><div class=shell>
<div class=top><div><h1>App Guide</h1><div class=sub>Ask how to use any app, or get a guided walkthrough. Answers are retrieved from each app's docs (redevops-rag).</div></div>
<div style="display:flex;gap:10px;align-items:center"><span class=rbac>🔐 RBAC role</span>
<select onchange="location.search='?role='+this.value">{roles}</select></div></div>
<div class=card><div class=ask><input id=q placeholder="e.g. how do I approve a dunning run? · walk me through Outreach Engine" onkeydown="if(event.key=='Enter')ask()">
<button class=btn onclick=ask()>Ask</button></div><div id=ans></div><div class=cite id=cite></div></div>
<h2>Apps you can see as <b style="color:var(--p)">{_esc(role)}</b> ({len(apps)})</h2>
<div class=foot style="margin:-4px 0 4px">Switch the RBAC role above — the guide only surfaces (and answers about) the apps that role is granted. This is the v-next Context Runtime RBAC layer, previewed.</div>
<div class=apps>{cards}</div>
</div>
<script>
const ROLE="{_esc(role)}";
async function ask(){{const q=document.getElementById('q').value.trim();if(!q)return;
 document.getElementById('ans').textContent='…';document.getElementById('cite').textContent='';
 const r=await fetch('api/ask',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{question:q,role:ROLE}})}});
 const d=await r.json();document.getElementById('ans').textContent=d.answer;
 document.getElementById('cite').textContent='cited: '+(d.cited||[]).join(', ');}}
async function walk(a){{const r=await fetch('api/walkthrough?role='+ROLE+'&app='+a);const d=await r.json();
 if(d.error){{document.getElementById('ans').textContent=d.error;return}}
 document.getElementById('ans').innerHTML='<b>Walkthrough — '+a+'</b>\\n\\n'+d.steps.map((s,i)=>(i+1)+'. '+s.replace(/\\*\\*(.*?)\\*\\*/g,'<b>$1</b>')).join('\\n\\n');
 document.getElementById('cite').textContent='core: '+d.core+' · group: '+d.group;window.scrollTo(0,0);}}
</script></body></html>"""


# ── Mission Runtime operator surface (Phase-1 production wiring) ──
# GET /capabilities + POST /invoke, so the runtime can drive the guide as an operator.
# Guarded: if agentic-os isn't installed, the app still runs standalone.
try:
    from .operator import build_guide_operator
    app.include_router(build_guide_operator().router())
except Exception:  # noqa: BLE001 — agentic_os absent → no operator surface
    pass

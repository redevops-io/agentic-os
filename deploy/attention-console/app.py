"""Founder Attention Queue — the Business-OS home at demo.redevops.io/attention.

Home is not a list of runtime names. It is the founder's decision list: approvals to give, missions blocked,
dollars held pending a call, evidence still too thin to act on — one prioritized queue across every business
system, produced by running the REAL world-runtime (agentic_os.world) and harvesting the points each governed
mission routed to a human. Every item shows why, which business system, the dollars at stake, a realism label,
and where it sits on the Observe → Recommend → Approve → Autonomous ladder. Nothing here sends or spends;
approve/decline are demo affordances over already-governed, SIMULATE-labelled decisions.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from runtime_contracts import AuthorityContext, PrincipalRef
from agentic_os.world import ALL_WORLDS, build_attention_queue, summarize

_SCOPES = ("read:crm", "read:geo", "write:quote", "write:crm", "read:secrets", "write:vendor", "write:billing")
_SEEDS = {"after-hours-lead": "8842", "kyc-ownership": "clean", "finance-leakage": "4471",
          "gtm-pilot-discovery": "c1", "creator-sponsorship": "s1", "sponsorship-booking": "s1"}
_BLOCK_LABEL = {"revenue_intelligence": "Revenue & Intelligence", "finance": "Finance",
                "security": "Security & Compliance", "customer_success": "Customer Success",
                "platform_ops": "Platform Ops", "runtime": "Runtime"}


def _authority():
    return AuthorityContext(authority_id="ctx", principal=PrincipalRef(id="founder", tenant="redevops"),
                            purpose="attention home", scope=_SCOPES)


def _queue():
    items = build_attention_queue(ALL_WORLDS, authority=_authority(), seeds=_SEEDS, offline=True)
    rows = []
    for it in items:
        d = it.to_dict()
        d["business_label"] = _BLOCK_LABEL.get(it.business_block, it.business_block.title())
        rows.append(d)
    return {"summary": summarize(items), "items": rows}


app = FastAPI(title="ReDevOps Attention Queue")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/attention")
def api_attention():
    return JSONResponse(_queue())


@app.get("/attention", response_class=HTMLResponse)
def page():
    return _PAGE


_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Attention · ReDevOps Business OS</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root{--bg:#f4f5f7;--card:#ffffff;--ink:#14181f;--muted:#5b6472;--line:#e3e6ea;--accent:#2f6df6;
--approval:#b7791f;--approval-bg:#fdf3dd;--blocked:#c0392b;--blocked-bg:#fbe6e3;--atrisk:#c05621;--atrisk-bg:#fdeadd;
--evidence:#2b6cb0;--evidence-bg:#e4eefb;--review:#556070;--review-bg:#eceef1;--sim:#6b7280;}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1217;--card:#171b22;--ink:#e9edf3;
--muted:#9aa4b2;--line:#262c36;--accent:#5b8cff;--approval-bg:#2c2410;--blocked-bg:#2c1512;--atrisk-bg:#2c1a0e;
--evidence-bg:#101f31;--review-bg:#1b1f26;}}
*{box-sizing:border-box}html,body{margin:0}body{background:var(--bg);color:var(--ink);
font-family:"IBM Plex Sans",system-ui,sans-serif;line-height:1.45;-webkit-font-smoothing:antialiased}
.wrap{max-width:960px;margin:0 auto;padding:32px 20px 64px}
.eyebrow{font:600 12px/1 "IBM Plex Mono",monospace;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
h1{font-family:Archivo,sans-serif;font-weight:800;font-size:clamp(26px,4vw,38px);margin:.25em 0 .1em;text-wrap:balance}
.sub{color:var(--muted);margin:0 0 24px;max-width:60ch}
.counters{display:flex;flex-wrap:wrap;gap:12px;margin:0 0 28px}
.counter{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px;min-width:120px}
.counter .n{font-family:Archivo,sans-serif;font-weight:800;font-size:24px;font-variant-numeric:tabular-nums}
.counter .l{font:500 11px/1.3 "IBM Plex Mono",monospace;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.counter.big .n{color:var(--accent)}
.group{margin:26px 0 10px;display:flex;align-items:baseline;gap:10px}
.group h2{font-family:Archivo,sans-serif;font-weight:700;font-size:15px;margin:0;letter-spacing:.01em}
.group .cnt{font:500 12px "IBM Plex Mono",monospace;color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin:10px 0;
display:grid;grid-template-columns:1fr auto;gap:6px 16px;align-items:start}
.card .title{font-weight:600;font-size:15px;grid-column:1}
.card .why{color:var(--muted);font-size:13.5px;grid-column:1;margin-top:2px}
.card .meta{grid-column:1;display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;align-items:center}
.badge{font:600 11px/1 "IBM Plex Mono",monospace;letter-spacing:.05em;padding:5px 9px;border-radius:999px;text-transform:uppercase}
.b-APPROVAL{color:var(--approval);background:var(--approval-bg)}.b-BLOCKED{color:var(--blocked);background:var(--blocked-bg)}
.b-AT_RISK{color:var(--atrisk);background:var(--atrisk-bg)}.b-NEEDS_EVIDENCE{color:var(--evidence);background:var(--evidence-bg)}
.b-REVIEW{color:var(--review);background:var(--review-bg)}
.pill{font:500 11px/1 "IBM Plex Mono",monospace;color:var(--muted);border:1px solid var(--line);padding:5px 9px;border-radius:999px}
.dollars{grid-column:2;grid-row:1/3;text-align:right;font-family:Archivo,sans-serif;font-weight:800;
font-size:20px;font-variant-numeric:tabular-nums;white-space:nowrap}
.dollars .z{color:var(--muted);font-weight:600;font-size:13px}
.act{grid-column:1/-1;display:flex;gap:8px;margin-top:12px;padding-top:12px;border-top:1px dashed var(--line)}
.act .s{font-size:12.5px;color:var(--muted);align-self:center;margin-right:auto}
button{font:600 12px "IBM Plex Sans",sans-serif;padding:7px 14px;border-radius:8px;border:1px solid var(--line);
background:transparent;color:var(--ink);cursor:pointer}button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button:disabled{opacity:.55;cursor:default}
.foot{color:var(--muted);font-size:12.5px;margin-top:30px;border-top:1px solid var(--line);padding-top:14px}
.foot code{font-family:"IBM Plex Mono",monospace;background:var(--review-bg);padding:1px 5px;border-radius:4px}
</style></head><body><div class="wrap">
<div class="eyebrow">ReDevOps Business OS</div>
<h1>What needs your attention</h1>
<p class="sub">One queue across every business system. Each item is a decision the governed Runtime routed to
you — with the evidence, the dollars at stake, and where it sits on the autonomy ladder. Nothing here sends
or spends; approvals act on already-governed, simulated decisions.</p>
<div class="counters" id="counters"></div>
<div id="queue"></div>
<div class="foot" id="foot"></div>
</div>
<script>
const KIND_ORDER=["APPROVAL","BLOCKED","AT_RISK","NEEDS_EVIDENCE","REVIEW"];
const money=n=> n>0 ? "$"+Math.round(n).toLocaleString() : "—";
async function load(){
  const r=await fetch("api/attention"); const d=await r.json();
  const s=d.summary, k=s.by_kind||{};
  document.getElementById("counters").innerHTML=
    `<div class="counter big"><div class="n">${money(s.dollars_awaiting_decision)}</div><div class="l">awaiting a decision</div></div>`+
    KIND_ORDER.filter(x=>k[x]).map(x=>`<div class="counter"><div class="n">${k[x]}</div><div class="l">${x.replace("_"," ").toLowerCase()}</div></div>`).join("");
  // group by business system, preserving priority order
  const groups={}; const order=[];
  d.items.forEach(it=>{ if(!groups[it.business_label]){groups[it.business_label]=[];order.push(it.business_label);} groups[it.business_label].push(it); });
  document.getElementById("queue").innerHTML=order.map(g=>{
    const rows=groups[g].map(cardHTML).join("");
    return `<div class="group"><h2>${g}</h2><span class="cnt">${groups[g].length} item${groups[g].length>1?"s":""}</span></div>${rows}`;
  }).join("");
  const rl = d.items.some(it=>/SIM/i.test(it.realism||""))?"Decisions run under SIMULATE — labelled, never real spend.":"";
  document.getElementById("foot").innerHTML=`${d.items.length} decisions across ${order.length} business systems · <code>GET /api/attention</code> · ${rl}`;
}
function cardHTML(it){
  const rl = it.realism ? `<span class="pill">${it.realism}</span>` : "";
  return `<div class="card">
    <div class="title">${esc(it.title)}</div>
    <div class="dollars">${it.dollar_impact>0?money(it.dollar_impact):'<span class="z">no $ gate</span>'}</div>
    <div class="why">${esc(it.why)}</div>
    <div class="meta"><span class="badge b-${it.kind}">${it.kind.replace("_"," ")}</span>
      <span class="pill">${esc(it.world_id)}</span><span class="pill">ladder: ${it.autonomy}</span>${rl}</div>
    <div class="act"><span class="s">${esc(it.suggested_action)}</span>
      ${it.kind==="APPROVAL"?'<button>Decline</button><button class="primary">Approve</button>':
        it.kind==="BLOCKED"?'<button class="primary">Unblock</button>':
        it.kind==="NEEDS_EVIDENCE"?'<button class="primary">Gather evidence</button>':'<button class="primary">Review</button>'}</div>
  </div>`;
}
function esc(s){return (s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
document.querySelectorAll && document.addEventListener("click",e=>{const b=e.target.closest("button");if(b&&!b.disabled){b.textContent="Done";b.disabled=true;}});
load();
</script></body></html>"""

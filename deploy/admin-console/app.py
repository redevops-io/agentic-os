"""Business-OS Admin Console — demo.redevops.io/admin.

The governance/admin view over the world-runtime: which OSS cores are wired LIVE vs the in-memory demo store,
whether every governed mission's invariants held (zero violations), what budgets are committed vs remaining,
and where each mission sits on the autonomy ladder. It runs the real engine and reports what actually
happened — never a hand-maintained status page. Read-only; nothing here sends or spends.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from runtime_contracts import AuthorityContext, PrincipalRef
from agentic_os.world import ALL_WORLDS, build_admin_snapshot

_SCOPES = ("read:crm", "read:geo", "write:quote", "write:crm", "read:secrets", "write:vendor", "write:billing")
_SEEDS = {"after-hours-lead": "8842", "kyc-ownership": "clean", "finance-leakage": "4471",
          "gtm-pilot-discovery": "c1", "creator-sponsorship": "s1", "sponsorship-booking": "s1",
          "paid-acquisition": "s1", "contact-center": "c1"}
_SYS = {"revenue_intelligence": "Revenue & Intelligence", "finance": "Finance",
        "customer_success": "Customer Success", "security": "Security & Compliance",
        "platform_ops": "Platform Ops", "runtime": "Runtime"}


def _authority():
    return AuthorityContext(authority_id="ctx", principal=PrincipalRef(id="admin", tenant="redevops"),
                            purpose="admin console", scope=_SCOPES)


def _snapshot():
    s = build_admin_snapshot(ALL_WORLDS, authority=_authority(), seeds=_SEEDS, offline=True)
    for c in s["cores"]:
        c["business_label"] = _SYS.get(c["business_system"], c["business_system"])
    for a in s["autonomy"]:
        a["business_label"] = _SYS.get(a["business_system"], a["business_system"])
    return s


app = FastAPI(title="ReDevOps Admin Console")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/admin")
def api_admin():
    return JSONResponse(_snapshot())


@app.get("/admin", response_class=HTMLResponse)
def page():
    return _PAGE


_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Admin · ReDevOps Business OS</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root{--bg:#f4f5f7;--card:#fff;--ink:#14181f;--muted:#5b6472;--line:#e3e6ea;--accent:#2f6df6;
--live:#1f8a4c;--live-bg:#e3f4ea;--seed:#b7791f;--seed-bg:#fdf3dd;--ok:#1f8a4c;--bad:#c0392b;--bad-bg:#fbe6e3;}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1217;--card:#171b22;--ink:#e9edf3;
--muted:#9aa4b2;--line:#262c36;--accent:#5b8cff;--live-bg:#10261a;--seed-bg:#2c2410;--bad-bg:#2c1512;}}
*{box-sizing:border-box}html,body{margin:0}body{background:var(--bg);color:var(--ink);
font-family:"IBM Plex Sans",system-ui,sans-serif;line-height:1.45;-webkit-font-smoothing:antialiased}
.wrap{max-width:1000px;margin:0 auto;padding:32px 20px 64px}
.eyebrow{font:600 12px/1 "IBM Plex Mono",monospace;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
h1{font-family:Archivo,sans-serif;font-weight:800;font-size:clamp(26px,4vw,36px);margin:.25em 0 .1em}
.sub{color:var(--muted);margin:0 0 24px;max-width:64ch}
.counters{display:flex;flex-wrap:wrap;gap:12px;margin:0 0 30px}
.counter{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px;min-width:120px}
.counter .n{font-family:Archivo,sans-serif;font-weight:800;font-size:22px;font-variant-numeric:tabular-nums}
.counter .l{font:500 11px/1.3 "IBM Plex Mono",monospace;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)}
.counter .n.ok{color:var(--ok)}.counter .n.bad{color:var(--bad)}
h2{font-family:Archivo,sans-serif;font-weight:700;font-size:16px;margin:28px 0 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden;margin-bottom:8px}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;font:600 11px/1 "IBM Plex Mono",monospace;letter-spacing:.06em;text-transform:uppercase;
color:var(--muted);padding:11px 16px;border-bottom:1px solid var(--line)}
td{padding:11px 16px;border-bottom:1px solid var(--line)}tr:last-child td{border-bottom:0}
td.num{font-variant-numeric:tabular-nums;text-align:right;font-family:"IBM Plex Mono",monospace}
.pill{font:600 11px/1 "IBM Plex Mono",monospace;padding:4px 9px;border-radius:999px}
.p-LIVE{color:var(--live);background:var(--live-bg)}.p-SEED,.p-SEEDED{color:var(--seed);background:var(--seed-bg)}
.p-APPROVE{color:var(--accent);background:transparent;border:1px solid var(--line)}
.p-AUTONOMOUS{color:var(--muted);background:transparent;border:1px solid var(--line)}
.ok{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600;background:var(--bad-bg);padding:2px 8px;border-radius:6px}
.foot{color:var(--muted);font-size:12.5px;margin-top:28px;border-top:1px solid var(--line);padding-top:14px}
.foot code{font-family:"IBM Plex Mono",monospace;background:var(--seed-bg);padding:1px 5px;border-radius:4px}
</style></head><body><div class="wrap">
<div class="eyebrow">ReDevOps Business OS</div>
<h1>Admin &amp; Governance</h1>
<p class="sub">A live read over the world-runtime: which cores are wired, whether every governed mission's
invariants held, budgets committed vs remaining, and where each mission sits on the autonomy ladder. Nothing
here is hand-maintained — it runs the engine and reports what actually happened.</p>
<div class="counters" id="counters"></div>
<h2>OSS cores — adapter &amp; realism</h2><div class="card"><table id="cores"></table></div>
<h2>Governance — invariants per mission</h2><div class="card"><table id="gov"></table></div>
<h2>Budgets — Budget Governor</h2><div class="card"><table id="budgets"></table></div>
<h2>Autonomy ladder</h2><div class="card"><table id="autonomy"></table></div>
<div class="foot" id="foot"></div>
</div>
<script>
const money=n=> "$"+Math.round(n||0).toLocaleString();
function tbl(el,head,rows){document.getElementById(el).innerHTML=
  "<thead><tr>"+head.map(h=>`<th>${h}</th>`).join("")+"</tr></thead><tbody>"+rows.join("")+"</tbody>";}
async function load(){
  const d=await (await fetch("api/admin")).json(), s=d.summary;
  document.getElementById("counters").innerHTML=[
    [`${s.cores_live}/${s.cores_total}`,"cores live",s.cores_live>0?"ok":""],
    [s.governance_clean?"clean":"violations","governance",s.governance_clean?"ok":"bad"],
    [s.worlds,"missions",""],[s.approval_gated,"approval-gated",""],
    [money(s.budget_committed),"committed",""],[money(s.budget_remaining),"remaining",""]
  ].map(([n,l,c])=>`<div class="counter"><div class="n ${c}">${n}</div><div class="l">${l}</div></div>`).join("");
  tbl("cores",["App","OSS core","Business system","Adapter","Realism"],
    d.cores.map(c=>`<tr><td>${c.app}</td><td>${c.core}</td><td>${c.business_label}</td>
      <td class="mono">${c.adapter}</td><td><span class="pill p-${c.status}">${c.realism}</span></td></tr>`));
  tbl("gov",["Mission","Invariants checked","Violations","Status"],
    d.governance.map(g=>`<tr><td>${g.world}</td><td class="num">${g.invariants}</td>
      <td class="num">${g.violations}</td><td>${g.clean?'<span class="ok">✓ all held</span>':'<span class="bad">'+g.violations+' violated</span>'}</td></tr>`));
  tbl("budgets",["Mission","Budget","Committed","Remaining"],
    (d.budgets.length?d.budgets:[]).map(b=>`<tr><td>${b.world}</td><td class="num">${money(b.total_budget)}</td>
      <td class="num">${money(b.committed_total)}</td><td class="num">${money(b.remaining)}</td></tr>`)
    ||[]);
  if(!d.budgets.length)document.getElementById("budgets").innerHTML="<tbody><tr><td style='padding:14px 16px;color:var(--muted)'>no active budgets</td></tr></tbody>";
  tbl("autonomy",["Mission","Business system","Ladder rung"],
    d.autonomy.map(a=>`<tr><td>${a.world}</td><td>${a.business_label}</td>
      <td><span class="pill p-${a.rung}">${a.rung}</span></td></tr>`));
  document.getElementById("foot").innerHTML=`${d.worlds.length} missions · ${d.cores.length} cores · <code>GET /api/admin</code> · cores read LIVE only when a real core answers a health probe; otherwise the in-memory demo store (SEEDED).`;
}
load();
</script></body></html>"""

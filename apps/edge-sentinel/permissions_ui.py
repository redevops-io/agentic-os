"""edge-sentinel · live permissions demo (row-scope + column-mask over the vuln DB).

A control-plane-style UI to set a "query agent"'s permissions on the DB + table and watch the
resulting query re-slice live: full access (left) vs the scoped grant (right). The decision mirrors
the enterprise PermissionsPlane semantics — privileged bypass -> database grant -> table grant ->
row scope (owns_rows_of) -> column mask. It reads the REAL Doris vuln DB when reachable, else a
representative sample, so the demo always renders.

The permissioned payload is HONEST: withheld rows and masked column VALUES never leave the server
(the client only learns WHICH columns were masked, so it can render •••). Self-contained — reuses
the shared NVD/OSV vuln DB (context_runtime.integrations.vuln_db) when present, else the sample.
"""
from __future__ import annotations

import os

# Consumer of the shared NVD/OSV vuln DB (Apache Doris) — same optional integration app.py uses.
try:
    from context_runtime.integrations.vuln_db import VulnDB as _VulnDB, Principal as _Principal
    _VDB = _VulnDB()
except Exception:  # noqa: BLE001
    _VDB = None
    _Principal = None

_PERM_DB = os.environ.get("DORIS_DATABASE", "context_runtime")
_PERM_TABLE = "vulns"
# Row scope is demonstrated on `ecosystem` (a RowFilter column) because it actually partitions the
# live vuln DB (PyPI/npm/…), whereas source/severity/cvss are constant there. Any column works — the
# plane's RowScope maps to a RowFilter{column, in}; this just picks the one that varies.
_PERM_SCOPE_COL = "ecosystem"
_PERM_COLS = ["cve_id", "package", "ecosystem", "severity", "cvss", "source", "fixed_version", "summary", "refs"]

_PERM_SAMPLE = [
    {"cve_id": "CVE-2024-3094", "package": "xz-utils", "ecosystem": "Debian", "severity": "CRITICAL", "cvss": 10.0,
     "source": "nvd", "owner": "nvd", "fixed_version": "5.6.2", "summary": "Malicious backdoor in xz/liblzma reachable via sshd.",
     "refs": "https://nvd.nist.gov/vuln/detail/CVE-2024-3094"},
    {"cve_id": "CVE-2021-44228", "package": "log4j-core", "ecosystem": "Maven", "severity": "CRITICAL", "cvss": 10.0,
     "source": "nvd", "owner": "nvd", "fixed_version": "2.17.1", "summary": "Log4Shell: JNDI lookup enables remote code execution.",
     "refs": "https://nvd.nist.gov/vuln/detail/CVE-2021-44228"},
    {"cve_id": "CVE-2022-22965", "package": "spring-core", "ecosystem": "Maven", "severity": "CRITICAL", "cvss": 9.8,
     "source": "nvd", "owner": "nvd", "fixed_version": "5.3.18", "summary": "Spring4Shell: data binding RCE on JDK 9+.",
     "refs": "https://nvd.nist.gov/vuln/detail/CVE-2022-22965"},
    {"cve_id": "CVE-2023-44487", "package": "nghttp2", "ecosystem": "Debian", "severity": "HIGH", "cvss": 7.5,
     "source": "nvd", "owner": "nvd", "fixed_version": "1.57.0", "summary": "HTTP/2 Rapid Reset denial of service.",
     "refs": "https://nvd.nist.gov/vuln/detail/CVE-2023-44487"},
    {"cve_id": "GHSA-jfh8-c2jp-5v3q", "package": "log4j-core", "ecosystem": "Maven", "severity": "CRITICAL", "cvss": 10.0,
     "source": "osv", "owner": "osv", "fixed_version": "2.17.1", "summary": "Log4Shell (OSV advisory mirror) — JNDI RCE.",
     "refs": "https://osv.dev/vulnerability/GHSA-jfh8-c2jp-5v3q"},
    {"cve_id": "GHSA-3xgq-45jj-v275", "package": "moment", "ecosystem": "npm", "severity": "HIGH", "cvss": 7.5,
     "source": "osv", "owner": "osv", "fixed_version": "2.29.4", "summary": "Regular-expression denial of service in moment.",
     "refs": "https://osv.dev/vulnerability/GHSA-3xgq-45jj-v275"},
    {"cve_id": "GHSA-p6mc-m468-83gw", "package": "lodash", "ecosystem": "npm", "severity": "HIGH", "cvss": 7.4,
     "source": "osv", "owner": "osv", "fixed_version": "4.17.21", "summary": "Prototype pollution in lodash set/merge.",
     "refs": "https://osv.dev/vulnerability/GHSA-p6mc-m468-83gw"},
    {"cve_id": "PYSEC-2023-135", "package": "requests", "ecosystem": "PyPI", "severity": "MEDIUM", "cvss": 6.1,
     "source": "osv", "owner": "osv", "fixed_version": "2.31.0", "summary": "Proxy-Authorization header leak on cross-host redirect.",
     "refs": "https://osv.dev/vulnerability/PYSEC-2023-135"},
    {"cve_id": "CVE-2023-38545", "package": "curl", "ecosystem": "Debian", "severity": "HIGH", "cvss": 7.5,
     "source": "nvd", "owner": "nvd", "fixed_version": "8.4.0", "summary": "SOCKS5 heap buffer overflow in curl.",
     "refs": "https://nvd.nist.gov/vuln/detail/CVE-2023-38545"},
    {"cve_id": "GHSA-7rjr-3q55-vv33", "package": "log4j-core", "ecosystem": "Maven", "severity": "MEDIUM", "cvss": 6.6,
     "source": "osv", "owner": "osv", "fixed_version": "2.17.0", "summary": "Log4j: uncontrolled recursion from self-referential lookup.",
     "refs": "https://osv.dev/vulnerability/GHSA-7rjr-3q55-vv33"},
]


def fetch_all() -> tuple[list[dict], str]:
    """The full (privileged) row set for the demo, plus a source label ('doris' | 'sample')."""
    if _VDB is not None and _Principal is not None:
        try:
            if _VDB.available():
                rows = _VDB.lookup(principal=_Principal(roles=frozenset({"security"})), limit=200)
                if rows:
                    return rows, "doris"
        except Exception:  # noqa: BLE001
            pass
    return [dict(r) for r in _PERM_SAMPLE], "sample"


def decide(cfg: dict) -> dict:
    """Compute an AccessDecision from a permission config — mirrors PermissionsPlane.Authorize:
    privileged bypass -> database grant -> table grant -> row scope -> column mask."""
    role = str(cfg.get("role", "analyst")).lower()
    if role in ("admin", "security"):
        return {"allowed": True, "reason": f"privileged role '{role}' — full access (bypass)",
                "scope": "all", "row_filter": None, "masked_columns": []}
    if not cfg.get("db", True):
        return {"allowed": False, "reason": f"denied: no grant on database '{_PERM_DB}'",
                "scope": "none", "row_filter": None, "masked_columns": []}
    if not cfg.get("table", True):
        return {"allowed": False, "reason": f"denied: no grant on table '{_PERM_DB}.{_PERM_TABLE}'",
                "scope": "none", "row_filter": None, "masked_columns": []}
    scope_vals = [str(s) for s in (cfg.get("scope") or [])]
    masked = [c for c in (cfg.get("mask") or []) if c in _PERM_COLS]
    return {"allowed": True,
            "reason": f"analyst grant · row-scope own ({_PERM_SCOPE_COL} ∈ {scope_vals or '∅'}) · masked {masked or 'none'}",
            "scope": "own", "row_filter": {"column": _PERM_SCOPE_COL, "in": scope_vals}, "masked_columns": masked}


def universe(rows: list[dict], query: str) -> list[dict]:
    """The retrieval result BEFORE permissions — a shared package/CVE search filter (applies to both
    sides equally, since it's a query filter, not a grant)."""
    q = (query or "").strip().lower()
    if not q:
        return list(rows)
    out: list[dict] = []
    for r in rows:
        if q in str(r.get("package", "")).lower() or q in str(r.get("cve_id", "")).lower():
            out.append(r)
    return out


def scopes(univ: list[dict]) -> list[str]:
    """Distinct row-scope values present (the ecosystems the UI offers as grant chips)."""
    seen: dict[str, None] = {}
    for r in univ:
        v = str(r.get(_PERM_SCOPE_COL, "") or "").strip()
        if v:
            seen.setdefault(v, None)
    return sorted(seen)


def annotate(univ: list[dict], cfg: dict) -> tuple[dict, list[dict], int]:
    """The scoped identity's ACTUAL view: only the rows it may read, with masked column VALUES nulled.
    Withheld rows and masked values never leave the server — an honest permissioned payload, not a
    UI-only hide. Returns (decision, visible_records, visible_count)."""
    decision = decide(cfg)
    masked = decision.get("masked_columns") or []
    rf = decision.get("row_filter")
    allowset = {str(s).lower() for s in ((rf or {}).get("in") or [])}
    recs: list[dict] = []
    for r in univ:
        if not decision["allowed"]:
            continue  # denied grant: nothing is readable
        if rf is not None:
            val = str(r.get(_PERM_SCOPE_COL, "") or "").lower()
            if not (allowset and val in allowset):
                continue  # row scope: outside the grant → not returned at all
        rec = {c: (None if c in masked else r.get(c)) for c in _PERM_COLS}
        rec["_masked"] = list(masked)  # so the UI renders ••• (the value itself is not sent)
        recs.append(rec)
    return decision, recs, len(recs)


def query(body: dict) -> dict:
    """Run the vuln query as full-access (left) and the posted scoped grant (right)."""
    limited = (body or {}).get("limited") or {}
    filters = (body or {}).get("filters") or {}
    q = str(filters.get("q") or "")
    rows, source = fetch_all()
    univ = universe(rows, q)
    full_dec = decide({"role": "security"})
    full_records = [{c: r.get(c) for c in _PERM_COLS} for r in univ]  # permissionless: all rows, all cols
    lim_dec, lim_records, visible = annotate(univ, limited)
    return {
        "db": _PERM_DB, "table": _PERM_TABLE, "source": source, "total": len(univ),
        "columns": _PERM_COLS, "scope_col": _PERM_SCOPE_COL, "scopes": scopes(univ),
        "full": {"decision": full_dec, "count": len(univ), "records": full_records},
        "limited": {"decision": lim_dec, "count": visible, "withheld": len(univ) - visible,
                    "masked_columns": lim_dec.get("masked_columns") or [], "records": lim_records},
    }


PERM_PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vuln-DB Access Control — live permissions</title>
<style>
:root{--primary:#4fd1c5;--bg:#0e0e10;--card:#141416;--card2:#1a1a1d;--bd:#2b2b30;--tx:#e7e5ea;--mut:#9b99a1}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.5 Roboto,system-ui,sans-serif}
a{color:var(--primary)}
.wrap{max-width:1180px;margin:0 auto;padding:20px}
header h1{font-size:20px;margin:0 0 4px}
header p{color:var(--mut);margin:0 0 16px;max-width:900px}
code{background:#000;border:1px solid var(--bd);border-radius:5px;padding:1px 6px;color:var(--primary)}
.controls{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:16px;margin-bottom:18px}
.controls h2{font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:var(--mut);margin:0 0 12px}
.row{display:flex;flex-wrap:wrap;gap:20px;align-items:flex-start}
.grp{display:flex;flex-direction:column;gap:7px}
.grp .lbl{font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:var(--mut)}
.seg{display:inline-flex;border:1px solid var(--bd);border-radius:9px;overflow:hidden}
.seg button{background:transparent;color:var(--tx);border:0;padding:7px 12px;cursor:pointer;font:inherit}
.seg button.on{background:var(--primary);color:#08312d;font-weight:600}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--bd);border-radius:20px;padding:6px 12px;cursor:pointer;user-select:none}
.chip.on{border-color:var(--primary);background:rgba(79,209,197,.12)}
.chip input{display:none}
.num{width:66px;background:#000;border:1px solid var(--bd);border-radius:8px;color:var(--tx);padding:6px 8px}
.panels{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:820px){.panels{grid-template-columns:1fr}}
.panel{background:var(--card);border:1px solid var(--bd);border-radius:14px;overflow:hidden}
.panel.lim{border-color:var(--primary)}
.phead{padding:12px 14px;border-bottom:1px solid var(--bd);display:flex;justify-content:space-between;align-items:center;gap:10px}
.phead .who{font-weight:600}
.phead .who small{display:block;color:var(--mut);font-weight:400;font-size:11px}
.count{background:#000;border:1px solid var(--bd);border-radius:20px;padding:3px 10px;font-variant-numeric:tabular-nums;white-space:nowrap}
.decision{padding:9px 14px;font-size:12px;border-bottom:1px solid var(--bd);white-space:normal}
.decision.ok{color:#8fe3d6}
.decision.deny{color:#ff9b9b}
.hits{padding:10px;overflow:auto;max-height:62vh;display:flex;flex-direction:column;gap:8px}
.hit{border:1px solid var(--bd);border-radius:10px;padding:10px 12px;background:#131316}
.hit.withheld{opacity:.5;border-style:dashed}
.hit .fn{display:flex;justify-content:space-between;gap:8px;font-family:'Roboto Mono',ui-monospace,monospace;font-size:12.5px;color:var(--tx)}
.hit .fn .cvss{color:var(--mut);font-size:11px}
.bar{height:5px;border-radius:3px;background:#000;overflow:hidden;margin:6px 0}
.bar i{display:block;height:100%;background:var(--primary)}
.bar.hi i{background:#ff9b9b}.bar.med i{background:#e7d488}
.meta{display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-size:11px;color:var(--mut);margin:2px 0 5px}
.sum{font-size:12px;color:#cfcdd3;line-height:1.45}
.mut{color:var(--mut)}
.mask{color:#6a6a72;font-style:italic}
.lock{color:#ff9b9b;font-size:11px;white-space:nowrap}
.sev-CRITICAL{color:#ff9b9b;font-weight:600}.sev-HIGH{color:#ffb877}.sev-MEDIUM{color:#e7d488}.sev-LOW{color:#9bd0ff}
.src{text-transform:uppercase;font-size:10px;letter-spacing:.4px;border:1px solid var(--bd);border-radius:4px;padding:1px 5px;color:var(--mut)}
.deny-box{padding:22px 14px;color:var(--mut);text-align:center}
.foot{color:var(--mut);font-size:11px;margin-top:14px;max-width:900px}
</style></head>
<body>
<div class="wrap">
<header>
<h1>&#128274; Vuln-DB Access Control &mdash; live permissions</h1>
<p>The same retrieval, two ways &mdash; <b>permissionless</b> (Security Admin, left) vs <b>permissioned</b> (the scoped Query Agent, right). Change the agent's grant on <code id="res">context_runtime.vulns</code> below and the right column re-slices live: withheld hits are <b>dimmed</b> (row scope), masked fields show as <b>&bull;&bull;&bull;</b>. <span id="src"></span></p>
</header>

<div class="controls">
<h2>Query Agent &mdash; grant</h2>
<div class="row">
  <div class="grp"><span class="lbl">Role</span>
    <div class="seg" id="role">
      <button data-role="analyst" class="on">Analyst</button>
      <button data-role="security">Security (privileged)</button>
    </div>
  </div>
  <div class="grp"><span class="lbl">Grants</span>
    <div class="chips">
      <label class="chip on" id="c-db"><input type="checkbox" checked> database <code>context_runtime</code></label>
      <label class="chip on" id="c-tbl"><input type="checkbox" checked> table <code>vulns</code></label>
    </div>
  </div>
  <div class="grp"><span class="lbl">Row scope &mdash; ecosystems the agent may read (owns_rows_of)</span>
    <div class="chips" id="eco-chips"></div>
  </div>
  <div class="grp"><span class="lbl">Column mask</span>
    <div class="chips">
      <label class="chip on" id="m-refs"><input type="checkbox" checked> mask <code>refs</code></label>
      <label class="chip" id="m-sum"><input type="checkbox"> mask <code>summary</code></label>
    </div>
  </div>
  <div class="grp"><span class="lbl">Query filter</span>
    <input class="num" id="q" type="text" placeholder="package or CVE…" style="width:160px">
  </div>
</div>
</div>

<div class="panels">
  <div class="panel"><div class="phead"><div class="who">Security Admin<small>role: security &middot; full access (permissionless)</small></div><span class="count" id="full-count">&mdash;</span></div>
    <div class="decision ok" id="full-dec"></div><div class="hits" id="full-hits"></div></div>
  <div class="panel lim"><div class="phead"><div class="who">Query Agent<small id="lim-sub">scoped grant</small></div><span class="count" id="lim-count">&mdash;</span></div>
    <div class="decision" id="lim-dec"></div><div class="hits" id="lim-hits"></div></div>
</div>
<div class="foot">The decision mirrors the enterprise <b>PermissionsPlane</b>: privileged bypass &rarr; database grant &rarr; table grant &rarr; row scope (owns_rows_of) &rarr; column mask. Min-CVSS is a shared query filter (applies to both), not a permission.</div>
</div>

<script>
const API = location.pathname.replace(/\/ui$/, "/query");
let role = "analyst";
let ecoState = null;   // Set of ecosystems granted to the Query Agent (the row-scope grant)
function cfg(){
  return {
    role: role,
    db: document.querySelector("#c-db input").checked,
    table: document.querySelector("#c-tbl input").checked,
    scope: ecoState ? Array.from(ecoState) : [],
    mask: [["#m-refs","refs"],["#m-sum","summary"]].filter(x=>document.querySelector(x[0]+" input").checked).map(x=>x[1])
  };
}
function renderEcoChips(list){
  const el=document.getElementById("eco-chips");
  el.innerHTML = list.length ? list.map(function(e){
    const on = ecoState.has(e);
    return '<label class="chip '+(on?"on":"")+'" data-eco="'+esc(e)+'"><input type="checkbox" '+(on?"checked":"")+'> '+esc(e)+'</label>';
  }).join("") : '<span class="mut" style="font-size:12px">no rows</span>';
  el.querySelectorAll("label[data-eco]").forEach(function(lab){
    const inp=lab.querySelector("input");
    inp.addEventListener("change",function(){
      const e=lab.getAttribute("data-eco");
      if(inp.checked) ecoState.add(e); else ecoState.delete(e);
      lab.classList.toggle("on",inp.checked); debounced();
    });
  });
}
function esc(s){return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}
function bar(cvss){
  const p=Math.max(0,Math.min(1,(Number(cvss)||0)/10));
  const cls=cvss>=9?"hi":(cvss>=7?"med":"");
  return '<div class="bar '+cls+'"><i style="width:'+(p*100).toFixed(0)+'%"></i></div>';
}
function hitCard(r, mode){
  const masked = mode==="lim" ? (r._masked||[]) : [];
  const m = c => masked.indexOf(c)>=0;
  const sum = m("summary") ? '<span class="mask">&bull;&bull;&bull; masked</span>' : esc(r.summary||"");
  const refs = m("refs") ? '<span class="mask">&bull;&bull;&bull;</span>'
                         : (r.refs ? '<a href="'+esc(r.refs)+'" target="_blank" rel="noopener">ref</a>' : '&mdash;');
  const cvss = Number(r.cvss)||0;
  const cvssTag = cvss>0 ? '<span class="cvss">CVSS '+esc(r.cvss)+'</span>' : '';
  const sevTag = (r.severity && String(r.severity).toUpperCase()!=="UNKNOWN") ? '<span class="sev-'+esc(r.severity)+'">'+esc(r.severity)+'</span>' : '';
  return '<div class="hit"><div class="fn"><span>'+esc(r.cve_id)+'</span>'+cvssTag+'</div>'+
    (cvss>0 ? bar(cvss) : '')+
    '<div class="meta"><span class="src">'+esc(r.ecosystem||"—")+'</span><span>'+esc(r.package)+'</span><span>fixed: '+esc(r.fixed_version||"—")+
    '</span>'+sevTag+'<span>refs: '+refs+'</span></div>'+
    '<div class="sum">'+sum+'</div></div>';
}
function renderHits(id, records, mode, withheld){
  const el=document.getElementById(id);
  let html=(records||[]).map(function(r){ return hitCard(r,mode); }).join("");
  if(mode==="lim" && withheld>0)
    html += '<div class="hit withheld" style="text-align:center;border-style:dashed">'+
            '<span class="lock">&#128274; '+withheld+' more hit'+(withheld>1?"s":"")+' withheld &middot; outside the row scope grant</span></div>';
  el.innerHTML = html || '<div class="deny-box">no hits match the query</div>';
}
function setDec(pfx, dec){
  const d=document.getElementById(pfx+"-dec");
  d.className="decision "+(dec.allowed?"ok":"deny");
  d.textContent=(dec.allowed?"✔ allow · ":"✕ ")+dec.reason;
}
let timer=null;
async function refresh(){
  const q=document.getElementById("q").value;
  const body={limited:cfg(), filters:{q:q}};
  try{
    const r=await fetch(API,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const d=await r.json();
    if(ecoState===null){
      const list=d.scopes||[];
      ecoState=new Set(list.length>1 ? list.slice(0,list.length-1) : list);  // default: all-but-one → an immediate visible diff
      renderEcoChips(list);
      return refresh();
    }
    renderEcoChips(d.scopes||[]);
    document.getElementById("res").textContent=d.db+"."+d.table;
    document.getElementById("src").innerHTML = d.source==="doris"
      ? "Live Doris data ("+d.total+" rows)."
      : '<b style="color:#e7d488">Sample data</b> &mdash; Doris unreachable ('+d.total+' rows).';
    document.getElementById("lim-sub").textContent = "role: "+cfg().role;
    document.getElementById("full-count").textContent = d.full.count+" hits";
    const L=d.limited;
    document.getElementById("lim-count").textContent = L.count+" of "+d.total+" hits"+(L.withheld?(" · "+L.withheld+" withheld"):"");
    setDec("full", d.full.decision); setDec("lim", L.decision);
    renderHits("full-hits", d.full.records, "full", 0);
    if(!L.decision.allowed){
      document.getElementById("lim-hits").innerHTML='<div class="deny-box" style="color:#ff9b9b">Access denied &mdash; '+esc(L.decision.reason)+'</div>';
    } else {
      renderHits("lim-hits", L.records, "lim", L.withheld);
    }
  }catch(e){ document.getElementById("lim-dec").textContent="error: "+e; }
}
function debounced(){ clearTimeout(timer); timer=setTimeout(refresh,120); }
document.querySelectorAll("#role button").forEach(function(b){ b.addEventListener("click",function(){
  role=b.dataset.role;
  document.querySelectorAll("#role button").forEach(function(x){ x.classList.toggle("on",x===b); });
  debounced();
}); });
document.querySelectorAll(".chip input").forEach(function(inp){ inp.addEventListener("change",function(){
  inp.closest(".chip").classList.toggle("on",inp.checked); debounced();
}); });
document.getElementById("q").addEventListener("input",debounced);
refresh();
</script>
</body></html>"""

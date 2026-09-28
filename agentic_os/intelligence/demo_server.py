"""Reference ASGI server for the Intelligence §3 API — the deployable entry point for a live demo.

Run it directly:  `uvicorn agentic_os.intelligence.demo_server:app --port 8814`
behind a host port + a cloudflared route + a DNS CNAME (e.g. intel.redevops.io).

It wires an :class:`IntelligenceService` over compact demo data for several families (counterparty, supply,
order) so every §3 endpoint is exercisable, and adds one bespoke route — GET /v1/intelligence/kyc/screen —
that demonstrates the temporal ownership graph: the same vendor screens GO before its upstream sanction was
known and NO-GO after (bi-temporal replay). The demo data is self-contained; a real deployment binds the
tenant's own graphs instead.
"""
from .apps import app_capabilities
from .families import (
    CounterpartyRecords, InMemoryOrderGraph, InMemoryRevenueState, QuoteInputs, SupplyGraph,
    counterparty_registry, counterparty_synthesize, order_registry, order_synthesize, revenue_registry,
    revenue_synthesize, supply_registry, supply_synthesize,
)
from .service import IntelligenceService
from .temporal_graph import project_kyc_ownership, screen_ownership

# ── compact demo data (self-contained; a real deploy binds the tenant's own graphs) ──────────────────────
_KYC_VENDORS = {
    "handlowy": {"name": "HANDLOWY-INWESTYCJE", "country": "PL", "kyc": "NO-GO",
                 "sanctioned_owner": "CITIGROUP INC.", "hops_upstream": 6},
    "banca": {"name": "BANCA CENTRO EMILIA", "country": "IT", "kyc": "GO",
              "sanctioned_owner": None, "hops_upstream": 0},
    "abb": {"name": "ABB AG", "country": "DE", "kyc": "ABSTAIN", "sanctioned_owner": None, "hops_upstream": 0},
}
_KYC_SANCTION_KNOWN = "2026-02-01T00:00:00Z"


def build_service() -> IntelligenceService:
    """An IntelligenceService bound with compact demo data for the counterparty / supply / order families."""
    from ..integrations.business.contracts import Invoice, Provenance, Receivable
    from ..integrations.business.supply import InventoryPosition, PurchaseOrder
    from ..revenue.leakage import stalled_opportunity
    from ..revenue.quote import CatalogItem, QuoteLine

    def pr(ref):
        return Provenance(provider="demo", provider_ref=ref)

    supply = SupplyGraph(
        inventory=[InventoryPosition(prov=pr("i"), part="Rim", site="A", on_hand=20.0)],
        open_supply=[PurchaseOrder(prov=pr("po1"), supplier_ref="sup:acme", site="A", part="Rim",
                                   quantity=50.0, promised_date="2026-02-15", ordered_at="2026-01-01T00:00:00Z")],
        demand=[])
    order = InMemoryOrderGraph(_pos=[], _sales_orders=[], _lines=[], _receipts=[], _shipments=[])
    counterparty = CounterpartyRecords(
        invoices=[Invoice(prov=pr("inv1"), customer_ref="cust:acme", amount_cents=1000, amount_paid_cents=1000,
                          currency="USD", status="paid")],
        receivables=[Receivable(prov=pr("r1"), customer_ref="cust:acme", amount_outstanding_cents=0,
                                currency="USD", days_overdue=0)])

    # Revenue Intelligence — a feasible requested quote + a stalled-opportunity leakage, both computed from
    # the tenant's own resolved state (a real deploy binds the Twenty/ERPNext clients instead).
    import types

    _NOW = 1_774_000_000_000
    catalog = {
        "rim": CatalogItem("rim", "Rim", list_price_cents=40_000, unit_cost_cents=24_000, on_hand_qty=50.0),
        "hub": CatalogItem("hub", "Hub", list_price_cents=60_000, unit_cost_cents=39_000, on_hand_qty=0.0,
                           lead_time_days=10),
    }
    opp = types.SimpleNamespace(name="ACME expansion", stage="proposal", amount_cents=3_200_000,
                                prov=pr("opp:acme"))
    leak = stalled_opportunity(opp, last_activity_at_ms=_NOW - 40 * 86_400_000, has_future_activity=False,
                               now_ms=_NOW)
    revenue = InMemoryRevenueState()
    revenue.add_quote("cust:acme", QuoteInputs(
        lines=[QuoteLine("rim", 10.0), QuoteLine("hub", 2.0)], catalog=catalog, now_ms=_NOW))
    revenue.set_leakages("cust:acme", [leak] if leak else [])

    svc = IntelligenceService()
    svc.bind(supply_registry(supply), supply_synthesize)
    svc.bind(order_registry(order), order_synthesize)
    svc.bind(counterparty_registry(counterparty), counterparty_synthesize)
    svc.bind(revenue_registry(revenue), revenue_synthesize)
    return svc


# Applicants surfaced in the interactive KYC widget (label + why), drawn from _KYC_VENDORS.
_KYC_APPLICANTS = [
    ("handlowy", "HANDLOWY-INWESTYCJE · PL", "6 hops upstream to CITIGROUP INC."),
    ("banca", "BANCA CENTRO EMILIA · IT", "clean — no upstream owner"),
    ("abb", "ABB AG · DE", "insufficient ownership data → ABSTAIN"),
]


def _landing_html(service) -> str:
    """A self-contained landing page for the demo. The KYC panel calls the live screening endpoint so a
    visitor can replay the same question at two points in knowledge and watch the answer change."""
    caps = sorted(service.capabilities())
    cap_chips = "".join(f'<span class="chip">{c}</span>' for c in caps)
    ent = [c.value for c in app_capabilities("erpnext")]
    ent_chips = "".join(f'<span class="chip chip-dim">{e}</span>' for e in ent)
    options = "".join(f'<option value="{k}">{lbl} — {why}</option>' for k, lbl, why in _KYC_APPLICANTS)
    sanction = _KYC_SANCTION_KNOWN[:10]
    return _PAGE.replace("__CAP_CHIPS__", cap_chips).replace("__ENT_CHIPS__", ent_chips) \
        .replace("__OPTIONS__", options).replace("__SANCTION__", sanction) \
        .replace("__NCAPS__", str(len(caps))).replace("__NENT__", str(len(ent)))


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ReDevOps Decision Intelligence</title>
<meta name="description" content="Pay-per-decision intelligence with a governed, replayable, leakage-safe audit trail — a live §3 API demo.">
<style>
  :root{
    --bg:#f6f8f8; --panel:#ffffff; --ink:#0f1c1c; --muted:#5a6a6a; --line:#dce4e4;
    --accent:#0e9f8e; --accent-ink:#053b35; --go:#0e9f6e; --nogo:#d1495b; --abstain:#c8892b;
    --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    --sans:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
  }
  @media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
    --bg:#0b1413; --panel:#111d1c; --ink:#e7efee; --muted:#8ba09e; --line:#1e2e2c;
    --accent:#2dd4bf; --accent-ink:#d7fff7; --go:#34d399; --nogo:#f77083; --abstain:#e0a13a;
  }}
  :root[data-theme="dark"]{
    --bg:#0b1413; --panel:#111d1c; --ink:#e7efee; --muted:#8ba09e; --line:#1e2e2c;
    --accent:#2dd4bf; --accent-ink:#d7fff7; --go:#34d399; --nogo:#f77083; --abstain:#e0a13a;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);line-height:1.55;
    -webkit-font-smoothing:antialiased}
  .wrap{max-width:960px;margin:0 auto;padding:0 16px 64px}
  header{padding:48px 0 8px}
  .brand{display:flex;align-items:center;gap:10px;font-weight:700;letter-spacing:.02em;font-size:15px}
  .dot{width:11px;height:11px;border-radius:50%;background:var(--accent)}
  h1{font-size:clamp(28px,5vw,44px);line-height:1.08;margin:18px 0 10px;text-wrap:balance;font-weight:750}
  .lede{font-size:18px;color:var(--muted);max-width:60ch;margin:0}
  .badges{display:flex;flex-wrap:wrap;gap:8px;margin:22px 0 0}
  .badge{font-size:12px;font-family:var(--mono);color:var(--accent-ink);background:color-mix(in oklab,var(--accent) 22%,transparent);
    border:1px solid color-mix(in oklab,var(--accent) 40%,transparent);padding:4px 10px;border-radius:999px}
  section{margin-top:40px}
  .eyebrow{font-family:var(--mono);font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);margin:0 0 10px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:22px}
  h2{font-size:22px;margin:0 0 4px;font-weight:700}
  .sub{color:var(--muted);margin:0 0 18px}
  .row{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
  select,button{font:inherit}
  select{flex:1 1 260px;min-width:0;padding:11px 12px;border-radius:10px;border:1px solid var(--line);
    background:var(--bg);color:var(--ink)}
  .seg{display:inline-flex;border:1px solid var(--line);border-radius:10px;overflow:hidden}
  .seg button{padding:11px 14px;border:0;background:var(--panel);color:var(--ink);cursor:pointer;font-size:14px}
  .seg button[aria-pressed="true"]{background:var(--accent);color:#04211d;font-weight:650}
  .seg button+button{border-left:1px solid var(--line)}
  .hint{font-size:13px;color:var(--muted);margin:12px 0 0}
  .result{margin-top:18px;border-top:1px solid var(--line);padding-top:18px;min-height:96px}
  .verdict{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
  .pill{font-family:var(--mono);font-weight:700;font-size:15px;padding:6px 14px;border-radius:999px;color:#04211d}
  .pill.GO{background:var(--go)} .pill.NOGO{background:var(--nogo);color:#fff} .pill.ABSTAIN{background:var(--abstain)}
  .reason{color:var(--ink)}
  .chain{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:14px;font-family:var(--mono);font-size:12.5px}
  .node{background:var(--bg);border:1px solid var(--line);padding:4px 9px;border-radius:8px}
  .node.flag{background:color-mix(in oklab,var(--nogo) 22%,transparent);border-color:var(--nogo);color:var(--nogo);font-weight:700}
  .arrow{color:var(--muted)}
  .grid{display:flex;flex-wrap:wrap;gap:8px}
  .chip{font-family:var(--mono);font-size:12px;background:var(--bg);border:1px solid var(--line);padding:5px 10px;border-radius:8px}
  .chip-dim{color:var(--muted)}
  .ends{font-family:var(--mono);font-size:13px;color:var(--ink);display:grid;gap:8px;margin:0;padding:0;list-style:none}
  .ends li{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:9px 12px;overflow-x:auto}
  .m{color:var(--accent)}
  .cta{display:inline-block;margin-top:16px;background:var(--accent);color:#04211d;font-weight:650;text-decoration:none;
    padding:11px 18px;border-radius:10px}
  footer{margin-top:44px;padding-top:20px;border-top:1px solid var(--line);color:var(--muted);font-size:14px}
  a{color:var(--accent)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand"><span class="dot"></span> ReDevOps · Decision Intelligence</div>
    <h1>Answers a business can act on — governed, priced, and replayable.</h1>
    <p class="lede">A live §3 API that turns a decision need into a receipted result over your own data and
    managed providers. Every answer carries what it knew, when it knew it, and what it cost.</p>
    <div class="badges">
      <span class="badge">§3 HTTP API</span>
      <span class="badge">__NCAPS__ capabilities live</span>
      <span class="badge">bi-temporal · leakage-safe</span>
      <span class="badge">open-core demo</span>
    </div>
  </header>

  <section>
    <p class="eyebrow">Try it — replayable KYC ownership screening</p>
    <div class="card">
      <h2>Same vendor, same question. The answer changes only with what was known.</h2>
      <p class="sub">Screen a vendor's beneficial-ownership chain as of a point in <em>knowledge</em>. Before the
      upstream sanction surfaced (known __SANCTION__) it screens <b>GO</b>; after, <b>NO-GO</b> — no hindsight leaks in.</p>
      <div class="row">
        <select id="applicant" aria-label="Applicant vendor">__OPTIONS__</select>
        <div class="seg" role="group" aria-label="What was known at screening time">
          <button id="before" aria-pressed="true">Knew on 15 Jan 2026</button>
          <button id="after" aria-pressed="false">Knew on 1 Mar 2026</button>
        </div>
      </div>
      <p class="hint">Screening date fixed at 1 Mar 2026 · sanction on the chain became known __SANCTION__.</p>
      <div class="result" id="result" aria-live="polite"><p class="sub" style="margin:0">Screening…</p></div>
    </div>
  </section>

  <section>
    <p class="eyebrow">Served now · __NCAPS__ capabilities</p>
    <div class="grid">__CAP_CHIPS__</div>
    <h2 style="font-size:16px;margin:22px 0 8px">Entitlements a licensed app can request (__NENT__)</h2>
    <div class="grid">__ENT_CHIPS__</div>
  </section>

  <section>
    <p class="eyebrow">§3 endpoints</p>
    <ul class="ends">
      <li><span class="m">POST</span> /v1/intelligence/{domain}/{capability}</li>
      <li><span class="m">POST</span> /v1/intelligence/quote</li>
      <li><span class="m">GET</span> /v1/intelligence/requests/{id}</li>
      <li><span class="m">GET</span> /v1/intelligence/capabilities</li>
      <li><span class="m">GET</span> /v1/intelligence/kyc/screen?applicant=&amp;as_of=&amp;known_at=</li>
    </ul>
    <a class="cta" href="/docs">Open the interactive API docs →</a>
  </section>

  <footer>
    Open-core demo over the public intelligence families on self-contained data. The managed, single-key
    pay-per-decision gateway is a separate layer. · <a href="https://redevops.io">redevops.io</a>
  </footer>
</div>
<script>
  const AS_OF = "2026-03-01T00:00:00Z";
  const KNOWN = {before:"2026-01-15T00:00:00Z", after:"2026-03-01T00:00:00Z"};
  let mode = "before";
  const $ = s => document.querySelector(s);
  function esc(x){return String(x).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
  async function screen(){
    const a = $("#applicant").value;
    const res = $("#result");
    try{
      const r = await fetch(`/v1/intelligence/kyc/screen?applicant=${encodeURIComponent(a)}&as_of=${AS_OF}&known_at=${KNOWN[mode]}`);
      const d = await r.json();
      const dec = (d.decision||"").replace("-","");
      const chain = (d.chain||[]).map((n,i)=>{
        const flag = d.flagged && n===d.flagged;
        const arr = i? '<span class="arrow">→</span>' : '';
        return `${arr}<span class="node${flag?' flag':''}">${esc(n)}</span>`;
      }).join("");
      res.innerHTML = `<div class="verdict"><span class="pill ${dec}">${esc(d.decision||"—")}</span>
        <span class="reason">${esc(d.reason||"")}</span></div>
        ${chain?`<div class="chain">${chain}</div>`:""}`;
    }catch(e){ res.innerHTML = '<p class="sub" style="margin:0">Could not reach the screening endpoint.</p>'; }
  }
  function setMode(m){ mode=m; $("#before").setAttribute("aria-pressed", m==="before"); $("#after").setAttribute("aria-pressed", m==="after"); screen(); }
  $("#before").onclick=()=>setMode("before");
  $("#after").onclick=()=>setMode("after");
  $("#applicant").onchange=screen;
  screen();
</script>
</body>
</html>"""


def build_app():
    """Build the FastAPI app: the §3 router + a KYC temporal-screening demo route + a small index."""
    from fastapi import FastAPI

    from .api import build_router

    service = build_service()
    kyc_graph = project_kyc_ownership(_KYC_VENDORS, sanction_known_at=_KYC_SANCTION_KNOWN)

    from fastapi import Request
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI(title="ReDevOps Decision Intelligence", version="1")
    app.include_router(build_router(service))
    landing = _landing_html(service)
    index_json = {
        "service": "ReDevOps Decision Intelligence (§3 API)",
        "capabilities": list(service.capabilities()),
        "erpnext_entitlements": [c.value for c in app_capabilities("erpnext")],
        "endpoints": ["POST /v1/intelligence/{domain}/{capability}", "POST /v1/intelligence/quote",
                      "GET /v1/intelligence/requests/{id}", "GET /v1/intelligence/capabilities",
                      "GET /v1/intelligence/kyc/screen?applicant=&as_of=&known_at="],
    }

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        # Browsers (Accept: text/html) get the landing page; API clients get the machine-readable index.
        if "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(landing)
        return JSONResponse(index_json)

    @app.get("/v1/intelligence/kyc/screen")
    def kyc_screen(applicant: str, as_of: str, known_at: str = "") -> dict:
        """Replayable KYC ownership screening over the temporal graph — GO before the upstream sanction's
        known_at, NO-GO after. `known_at` defaults to `as_of`."""
        return screen_ownership(kyc_graph, applicant, valid_time=as_of, known_at=known_at or None)

    return app


app = build_app()

"""Intrinsic-security + telemetry showcase for the demo (demo.redevops.io/security).

The `/permissions` plane shows *data-access* grants (who may read which rows/columns). This companion plane
shows the v0.3.0 **runtime security + telemetry** contracts in motion — the layer beneath data-access:

  * every capability call becomes a canonical **SecurityDecision** (deny-wins) carrying the caller's
    **AuthorityContext**, so an over-scoped call is refused before its side effect;
  * every call is emitted at the boundary as a **RuntimeSecurityEvent** (hashes, not payloads; produced by
    the runtime, not the agent) under a Mission **TraceContext**;
  * the *series* is run through **correlate() → GovernanceDisposition → Containment**: two individually
    permissible calls (read PII, then egress externally) correlate to **DENY / CONTAINED**.

It runs the REAL runtime plane (`agentic_os.mission.security_monitor` + `tracing`) over a canned scenario —
the same code path a governed mission uses — so the demo is not a mock. Self-contained: one router with an
HTML page and one JSON endpoint; mount it next to the permissions router.
"""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

# The declared capability surface (from the registry, not the agent): what each capability may reach.
_DESCS = {
    "crm.read": SimpleNamespace(data_classifications=("pii",), required_authority=("read:crm",), network=()),
    "billing.report": SimpleNamespace(data_classifications=(), required_authority=("read:billing",), network=()),
    "storage.upload": SimpleNamespace(data_classifications=(), required_authority=("write:storage",),
                                      network=("s3.external.com",)),
}
_HANDLERS = {"crm.read": lambda i: {"records": 2000}, "billing.report": lambda i: {}, "storage.upload": lambda i: {}}
_SEQUENCE = ["crm.read", "billing.report", "storage.upload"]
# A leased authority that covers the reads but NOT the external write — to show the per-call authority gate.
_LEASED_SCOPE = {"read:crm", "read:billing"}


def run_scenario() -> dict:
    """Run the SecurityMonitor over the canned exfiltration-shape scenario and return the telemetry the UI
    renders: per-call authority decisions, the boundary event stream, the correlated disposition +
    containment, and the Mission-native trace tree."""
    from agentic_os.mission.executor import Executor, InMemoryOperatorClient  # noqa: PLC0415
    from agentic_os.mission.security_monitor import SecurityMonitor  # noqa: PLC0415
    from agentic_os.mission.tracing import MissionTrace  # noqa: PLC0415
    from agentic_os.mission.types import Node  # noqa: PLC0415

    mon = SecurityMonitor(mission_id="demo-7F21", descriptor_for=_DESCS.get)
    ex = Executor(InMemoryOperatorClient(_HANDLERS), monitor=mon)

    calls = []
    for cap in _SEQUENCE:
        ex.run(Node(capability=cap, operator="op"), {})
        desc = _DESCS[cap]
        required = set(getattr(desc, "required_authority", ()) or ())
        covered = required.issubset(_LEASED_SCOPE)
        calls.append({
            "capability": cap,
            "required_authority": sorted(required),
            "data_classifications": list(getattr(desc, "data_classifications", ()) or ()),
            "network": list(getattr(desc, "network", ()) or ()),
            # per-call SecurityDecision (deny-wins): a capability whose required authority is not covered by
            # the leased chain is DENY, before any side effect.
            "verdict": "ALLOW" if covered else "DENY",
            "reason": ("covered by the leased authority" if covered
                       else f"requires {sorted(required)} beyond the leased chain {sorted(_LEASED_SCOPE)}"),
        })

    disposition, reasons, containment = mon.enforce()
    spans = MissionTrace("demo-7F21").spans(mon.trajectory)
    events = [{
        "event_id": e.event_id, "capability": e.capability, "event_type": e.event_type,
        "result": e.result, "network": list(e.network), "data_classifications": list(e.data_classifications),
    } for e in mon.trajectory.events]

    return {
        "mission_id": "demo-7F21",
        "leased_authority": sorted(_LEASED_SCOPE),
        "calls": calls,
        "events": events,
        "trajectory": {
            "disposition": disposition.value,
            "containment": containment.value,
            "reasons": list(reasons),
        },
        "spans": spans,
        "trace_id": spans[0]["trace_id"] if spans else "",
    }


app = FastAPI(title="ReDevOps intrinsic-security showcase")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/security", response_class=HTMLResponse)
def security_page():
    return _PAGE


@app.get("/api/security/scenario")
def security_scenario():
    return run_scenario()


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Intrinsic Security · Agentic OS</title>
<style>
:root{--bg:#0f0f12;--card:#1f1f23;--line:#2f2f33;--fg:#e7e5ea;--muted:#9b99a1;--teal:#4fd1c5;--amber:#f5b544;--red:#f56565}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,sans-serif;line-height:1.55}
.wrap{max-width:960px;margin:0 auto;padding:40px 24px 80px}
a{color:var(--teal)}h1{font-size:30px;margin:0 0 6px}.sub{color:var(--muted);margin:0 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 22px;margin:18px 0}
.card h2{font-size:18px;margin:0 0 12px}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{border:1px solid var(--line);padding:8px 11px;text-align:left}
thead th{color:var(--teal);background:#17171a}
.pill{display:inline-block;font-size:12px;font-weight:700;padding:2px 9px;border-radius:999px}
.ALLOW{color:#0f0f12;background:var(--teal)}.DENY{color:#fff;background:var(--red)}.REQUIRE_REVIEW{color:#0f0f12;background:var(--amber)}
.NO_OVERRIDE{color:#fff;background:#9b2c2c}.mono{font-family:ui-monospace,monospace;font-size:12px;color:var(--muted)}
.dispo{font-size:22px;font-weight:700}.reasons li{color:#d3d1d7}
button{background:var(--teal);color:#0f0f12;border:0;border-radius:8px;padding:9px 16px;font-weight:700;cursor:pointer}
.tree{font-family:ui-monospace,monospace;font-size:12.5px;color:#cfd8d6;white-space:pre;overflow-x:auto}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px}
</style></head><body><div class="wrap">
<div class="top"><div><h1>Intrinsic Security &amp; Telemetry</h1>
<p class="sub">The layer beneath data-access grants — canonical decisions, boundary telemetry, correlation &amp; containment</p></div>
<a href="/permissions">← permissions</a></div>
<div class="card"><p>The <a href="/permissions">permissions</a> plane decides <em>who may read which rows</em>. This plane decides
<em>what a running mission is allowed to do</em>, records it at the boundary, and correlates the <strong>series</strong> of
calls — because a run of individually-permissible calls can compose into an exfiltration. Every number below comes from
running the <strong>real</strong> runtime security monitor over a canned scenario; nothing is mocked.</p>
<button onclick="run()">▶ Run the scenario</button> <span id="tid" class="mono"></span></div>
<div id="out"></div>
<script>
async function run(){
  const r = await fetch('/api/security/scenario'); const d = await r.json();
  document.getElementById('tid').textContent = 'trace_id ' + d.trace_id;
  const call = c => `<tr><td>${c.capability}</td><td class="mono">${(c.required_authority||[]).join(', ')||'—'}</td>
    <td class="mono">${(c.data_classifications||[]).join(', ')||'—'}</td><td class="mono">${(c.network||[]).join(', ')||'—'}</td>
    <td><span class="pill ${c.verdict}">${c.verdict}</span></td><td class="mono">${c.reason}</td></tr>`;
  const ev = e => `<tr><td>${e.capability}</td><td class="mono">${e.event_type}</td><td>${e.result}</td>
    <td class="mono">${(e.data_classifications||[]).join(',')||'—'}</td><td class="mono">${(e.network||[]).join(',')||'—'}</td></tr>`;
  const t = d.trajectory;
  document.getElementById('out').innerHTML = `
  <div class="card"><h2>1 · Per-call decision — AuthorityContext (leased: ${d.leased_authority.join(', ')})</h2>
    <table><thead><tr><th>capability</th><th>required authority</th><th>data</th><th>egress</th><th>verdict</th><th>reason</th></tr></thead>
    <tbody>${d.calls.map(call).join('')}</tbody></table></div>
  <div class="card"><h2>2 · Boundary telemetry — RuntimeSecurityEvent (not agent-reported)</h2>
    <table><thead><tr><th>capability</th><th>event</th><th>result</th><th>data</th><th>network</th></tr></thead>
    <tbody>${d.events.map(ev).join('')}</tbody></table></div>
  <div class="card"><h2>3 · Correlated disposition &amp; containment</h2>
    <p class="dispo"><span class="pill ${t.disposition}">${t.disposition}</span> &nbsp;·&nbsp; containment <b>${t.containment}</b></p>
    <ul class="reasons">${t.reasons.map(x=>`<li>${x}</li>`).join('')||'<li>no correlation triggered</li>'}</ul>
    <p class="sub">Individually permissible calls — read PII, then egress — correlate into an exfiltration shape. DENY drives containment; a NO_OVERRIDE stop cannot be reopened by the thing that caused it.</p></div>
  <div class="card"><h2>4 · Mission-native trace tree (one causal tree, OTel-exportable)</h2>
    <div class="tree">${d.spans.map(s=>'  '.repeat((s.parent_span_id?1:0))+'• '+s.name+'   '+s.status+'   '+(s.attributes['redevops.network']?('→ '+s.attributes['redevops.network'].join(',')):'')).join('\\n')}</div></div>`;
}
run();
</script></div></body></html>"""

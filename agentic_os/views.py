"""HTML views for the control plane chrome: the per-module nav shell and the
overview / "how it works" page.

Kept out of control_plane.py so the big HTML strings don't bury the routes.
These reuse the same MD3 dark theme as the main dashboard (DASHBOARD_HTML in
control_plane.py); the shared design tokens live in ``_BASE_CSS`` below.

Why a shell: each module serves its OWN self-contained dashboard (inline CSS,
no shared chrome). Proxied raw, a module page is a dead end — no way back to the
control plane, no way to jump to a sibling module, no sense of where you are.
``module_shell`` wraps the proxied page (rendered in a same-origin iframe at
``/m/<name>/raw``) in a persistent top bar: back-to-OS, a breadcrumb, a module
switcher, a live health dot, and the source link.
"""
from __future__ import annotations

import html
import json

# Shared design tokens + primitives (a trimmed subset of the dashboard's CSS).
_BASE_CSS = """
  :root{
    --surface:#131316; --surface-container-low:#1b1b1f; --surface-container:#1f1f23;
    --surface-container-high:#2a2a2e; --surface-container-highest:#353539;
    --on-surface:#e4e2e6; --on-surface-variant:#c7c5ca; --on-surface-muted:#918f96;
    --outline-variant:#2f2f33;
    --primary:#4fd1c5; --on-primary:#00201c; --primary-container:#00504a; --on-primary-container:#a8f0e6;
    --secondary:#f5b544; --success:#5bd98a; --success-container:#0f3d22;
    --danger:#f2544f; --danger-container:#5c1512; --info:#5aa9f0; --info-container:#103a5c;
    --sp-2:8px;--sp-3:12px;--sp-4:16px;--sp-5:24px;--sp-6:32px;
    --radius-sm:8px;--radius-md:12px;--radius-lg:16px;--radius-pill:999px;
    --font-sans:"Roboto",system-ui,-apple-system,"Segoe UI",sans-serif;
    --font-mono:"Roboto Mono",ui-monospace,"SF Mono",monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--surface);color:var(--on-surface);font-family:var(--font-sans);line-height:1.45}
  a{color:var(--primary);text-decoration:none}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--on-surface-muted);flex:none;display:inline-block}
  .dot.up{background:var(--success);box-shadow:0 0 8px rgba(91,217,138,.7)}
  .dot.down{background:var(--danger)} .dot.na{background:var(--on-surface-muted)}
"""


def module_shell(*, name: str, group: str, repo: str, switcher: list[dict]) -> str:
    """Wrap a module's proxied dashboard in persistent nav chrome.

    ``switcher`` is the list of modules that have a live dashboard, each
    ``{"name","group"}`` — used to populate the jump-to-module dropdown.
    """
    # Build the <optgroup>-grouped switcher server-side so it works without JS.
    by_group: dict[str, list[str]] = {}
    for m in switcher:
        by_group.setdefault(m["group"], []).append(m["name"])
    opts = []
    for g, members in by_group.items():
        opts.append(f'<optgroup label="{html.escape(g)}">')
        for n in members:
            sel = " selected" if n == name else ""
            label = n.replace("agentic-", "").replace("-", " ").title()
            opts.append(f'<option value="{html.escape(n)}"{sel}>{html.escape(label)}</option>')
        opts.append("</optgroup>")
    switcher_html = "".join(opts)
    src_url = "https://github.com/" + html.escape(repo)
    crumb = html.escape(name.replace("agentic-", "").replace("-", " ").title())

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(name)} · Context Runtime</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500&family=Roboto+Mono:wght@400;500&display=swap">
<style>{_BASE_CSS}
  body{{display:flex;flex-direction:column;height:100vh;overflow:hidden}}
  .navbar{{display:flex;align-items:center;gap:var(--sp-4);flex-wrap:wrap;
    padding:var(--sp-3) var(--sp-5);background:var(--surface-container-low);
    border-bottom:1px solid var(--outline-variant);flex:none}}
  .back{{display:inline-flex;align-items:center;gap:6px;font:600 15px/1 var(--font-sans);
    color:var(--on-primary);background:var(--primary);padding:11px 20px;border-radius:var(--radius-pill);box-shadow:var(--shadow-1)}}
  .back:hover{{background:var(--primary-container);color:var(--on-primary-container)}}
  .iconbtn{{background:var(--surface-container-high);color:var(--on-surface-variant);border:1px solid var(--outline-variant);
    border-radius:var(--radius-sm);padding:6px 10px;font:500 14px/1 var(--font-sans);cursor:pointer}}
  .iconbtn:hover{{color:var(--primary);border-color:var(--primary)}}
  .crumbs{{font:400 13px/18px var(--font-sans);color:var(--on-surface-muted)}}
  .crumbs a{{color:var(--on-surface-variant)}} .crumbs b{{color:var(--on-surface)}}
  .spacer{{flex:1}}
  .switch{{display:flex;align-items:center;gap:var(--sp-2);font:400 13px/1 var(--font-sans);color:var(--on-surface-muted)}}
  select{{background:var(--surface-container-high);color:var(--on-surface);border:1px solid var(--outline-variant);
    border-radius:var(--radius-sm);padding:7px 10px;font:500 13px/1 var(--font-sans)}}
  .health{{display:inline-flex;align-items:center;gap:6px;font:400 13px/1 var(--font-mono);color:var(--on-surface-muted)}}
  .src{{font:400 13px/1 var(--font-sans);color:var(--on-surface-muted)}} .src:hover{{color:var(--primary)}}
  .demo-banner{{display:flex;align-items:center;gap:var(--sp-3);flex:none;
    padding:7px var(--sp-5);background:var(--info-container);color:var(--info);
    font:400 13px/18px var(--font-sans);border-bottom:1px solid var(--outline-variant)}}
  .demo-banner b{{color:var(--on-surface)}}
  .demo-banner button{{margin-left:auto;background:none;border:1px solid currentColor;color:inherit;
    border-radius:var(--radius-pill);padding:2px 10px;font:500 12px/1 var(--font-sans);cursor:pointer}}
  .frame-wrap{{position:relative;flex:1;display:flex}}
  .loading{{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;gap:var(--sp-3);
    background:var(--surface);color:var(--on-surface-muted);font:400 14px/1 var(--font-sans);z-index:1}}
  .spinner{{width:18px;height:18px;border:2px solid var(--outline-variant);border-top-color:var(--primary);
    border-radius:50%;animation:spin .8s linear infinite}}
  @keyframes spin{{to{{transform:rotate(360deg)}}}}
  iframe{{flex:1;width:100%;border:0;background:var(--surface)}}
</style>
</head>
<body>
  <nav class="navbar">
    <a class="back" href="/">&larr; Back to OS</a>
    <span class="crumbs"><a href="/">Context Runtime</a> / <a href="/overview">{html.escape(group)}</a> / <b>{crumb}</b></span>
    <span class="spacer"></span>
    <span class="health"><span class="dot na" id="hdot"></span><span id="hlbl">checking…</span></span>
    <label class="switch">jump to
      <select onchange="if(this.value)location.href='/m/'+this.value">{switcher_html}</select>
    </label>
    <button class="iconbtn" onclick="reframe()" title="Reload this dashboard">&#8635;</button>
    <a class="src" href="{src_url}" target="_blank" rel="noopener">source &#8599;</a>
  </nav>
  <div class="demo-banner" id="demoBanner">
    Demo data for <b>Summit Roofing Co.</b>, a fictional tenant — not a real customer's account.
    <button onclick="document.getElementById('demoBanner').remove()">dismiss</button>
  </div>
  <div class="frame-wrap">
    <div class="loading" id="loading"><span class="spinner"></span> Loading {html.escape(name)}…</div>
    <iframe src="/m/{html.escape(name)}/raw" title="{html.escape(name)} dashboard"
            onload="frameLoaded()"></iframe>
  </div>
<script>
const NAME = {json.dumps(name)};
function reframe(){{ var f = document.querySelector('iframe'); f.src = f.src; }}
function frameLoaded(){{
  var l = document.getElementById('loading'); if (l) l.style.display = 'none';
  // The module dashboard is proxied same-origin, so retarget its EXTERNAL links to
  // open in a new tab — otherwise clicking one navigates the iframe to a site that
  // refuses framing and the dashboard goes blank (no way back but a reload).
  try {{
    var d = document.querySelector('iframe').contentDocument;
    d.querySelectorAll('a[href]').forEach(function(a){{
      var h = a.getAttribute('href') || '';
      if (/^https?:\\/\\//i.test(h) && a.host !== location.host) {{ a.target = '_blank'; a.rel = 'noopener'; }}
    }});
  }} catch (e) {{}}
}}
async function health(){{
  try{{
    const r = await fetch('/api/fleet'); if(!r.ok) return;
    const m = (await r.json()).find(x => x.name === NAME); if(!m) return;
    const dot = document.getElementById('hdot'), lbl = document.getElementById('hlbl');
    dot.className = 'dot ' + (m.health === 'up' ? 'up' : m.health === 'down' ? 'down' : 'na');
    let t = m.health;
    if(m.core) t += ' · core: ' + m.core + (m.connected ? ' ✓' : ' ✕');
    lbl.textContent = t;
  }}catch(e){{}}
}}
health(); setInterval(health, 5000);
</script>
</body>
</html>"""


def overview_page(*, groups: dict, has_agent: set, module_meta: dict, workflows: list) -> str:
    """The /overview "how it works" page: kernel + module map + workflows.

    ``module_meta[name]`` = {"pain","tagline","core","repo","group"}; ``has_agent``
    is the set of module names with a live dashboard; ``workflows`` mirrors the
    dashboard's cross-module flows. Live health is fetched client-side.
    """
    # Kernel pieces — the "what it's comprised of" the user asked for.
    kernel = [
        ("Registry", "A simple config file (no coding needed) that lists every module, the agents it runs, and which actions must pause for your approval."),
        ("Fleet", "The coordinator: starts the modules, gives each its agents, runs them on a schedule, and drives workflows that span several modules."),
        ("Router", "Sends every task to the cheapest model that can do it well — a model on your own hardware first, a premium one only for the hard 5%."),
        ("Context", "The shared memory of your business — the profile, customers, and policies every agent works from."),
        ("Approvals & Audit", "The human-in-the-loop gate: anything that moves money, touches compliance, or changes infrastructure pauses here for your one-click sign-off, and every decision is logged."),
        ("Permissions", "Fine-grained data access for every app — grant a subject read/write on a database, table or corpus, sliced by row scope and column mask. Set it up and preview it live.", "/permissions"),
    ]
    kernel_html = "".join(
        (lambda t, d, href: (
            f'<a class="kcard" style="display:block" href="{html.escape(href)}" id="{html.escape(t.split()[0].lower())}">'
            f'<h3>{html.escape(t)} &rarr;</h3><p>{html.escape(d)}</p></a>'
        ) if href else (
            f'<div class="kcard" id="{html.escape(t.split()[0].lower())}"><h3>{html.escape(t)}</h3><p>{html.escape(d)}</p></div>'
        ))(k[0], k[1], k[2] if len(k) > 2 else "")
        for k in kernel
    )

    # One-line "what it does" gloss per open-source core.
    core_blurb = {
        "Lago": "subscriptions & billing", "ERPNext": "bookkeeping & accounting",
        "Chatwoot": "customer-support inbox", "Postiz": "social scheduling",
        "CrowdSec": "intrusion detection", "OpenSCAP": "security-compliance scans",
        "Metabase": "business analytics", "changedetection": "website change monitoring",
        "Umami": "web analytics", "Twenty CRM": "CRM & pipeline", "Listmonk": "email/SMS lists",
        "redevops-rag": "hybrid RAG over the docs",
    }
    # Per-module override — several apps share the ERPNext core but do very different jobs,
    # so the core gloss alone ("bookkeeping & accounting") would mislabel them.
    module_core_blurb = {
        "agentic-crm": "CRM & sales pipeline",
        "agentic-privacy": "contact records & DSAR",
        "growth-assistant": "leads & CRM",
    }

    # Module map, grouped, each with its real OSS core.
    sections = []
    for g, members in groups.items():
        cards = []
        for n in members:
            meta = module_meta.get(n, {})
            live = n in has_agent
            core = meta.get("core") or ""
            blurb = module_core_blurb.get(n) or core_blurb.get(core)
            core_label = f"core: {core}" + (f" — {blurb}" if blurb else "")
            core_html = f'<span class="core">{html.escape(core_label)}</span>' if core else ""
            href = f"/m/{html.escape(n)}" if live else "https://github.com/" + html.escape(meta.get("repo", ""))
            tgt = "" if live else ' target="_blank" rel="noopener"'
            open_lbl = "Open dashboard &#8594;" if live else "source &#8599;"
            cards.append(
                f'<a class="omod" href="{href}"{tgt} data-name="{html.escape(n)}">'
                f'<span class="omod__top"><span class="dot na omod__dot"></span>'
                f'<span class="omod__name">{html.escape(n)}</span></span>'
                f'<span class="omod__pain">{html.escape(meta.get("pain",""))}</span>'
                f'{core_html}<span class="omod__open">{open_lbl}</span></a>'
            )
        sections.append(
            f'<section class="ogroup"><h2 class="ogroup__label">{html.escape(g)}</h2>'
            f'<div class="ogrid">{"".join(cards)}</div></section>'
        )
    modules_html = "".join(sections)

    from urllib.parse import quote

    def _wf(w):
        # deep-link into the Projects cockpit with the mission pre-filled + ready to run
        href = f'/cockpit?template={quote(w.get("template", ""))}&goal={quote(w.get("goal", w["name"]))}'
        return (
            f'<a class="wf" href="{html.escape(href)}">'
            f'<span class="wf-top"><span class="wf-name">{html.escape(w["name"])}</span>'
            f'<span class="wf-run">Run in Projects &#8594;</span></span>'
            + (f'<span class="wf-desc">{html.escape(w["desc"])}</span>' if w.get("desc") else "")
            + f'<span class="wf-steps">{html.escape(" → ".join(w["steps"]))}</span></a>')
    workflows_html = "".join(_wf(w) for w in workflows)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>How it works · Context Runtime</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500&family=Roboto+Mono:wght@400;500&display=swap">
<style>{_BASE_CSS}
  body{{padding:var(--sp-5)}}
  .shell{{max-width:1200px;margin-inline:auto;display:flex;flex-direction:column;gap:var(--sp-6)}}
  .navbar{{display:flex;align-items:center;gap:var(--sp-4);flex-wrap:wrap}}
  .back{{display:inline-flex;align-items:center;gap:6px;font:500 14px/1 var(--font-sans);
    color:var(--on-primary);background:var(--primary);padding:9px 14px;border-radius:var(--radius-pill)}}
  .back:hover{{background:var(--primary-container);color:var(--on-primary-container)}}
  h1{{margin:0;font:400 26px/32px var(--font-sans)}} h1 .accent{{color:var(--primary)}}
  .lede{{color:var(--on-surface-variant);font:400 15px/22px var(--font-sans);max-width:80ch;margin:0}}
  .flow{{display:flex;flex-wrap:wrap;align-items:center;gap:var(--sp-3);
    background:var(--surface-container-low);border:1px solid var(--outline-variant);
    border-radius:var(--radius-lg);padding:var(--sp-5)}}
  .flow .node{{font:500 13px/1 var(--font-sans);color:var(--on-surface);background:var(--surface-container-high);
    border:1px solid var(--outline-variant);padding:8px 12px;border-radius:var(--radius-pill)}}
  .flow .node.gate{{color:var(--secondary);border-color:var(--secondary)}}
  .flow .arr{{color:var(--on-surface-muted);font:400 16px/1 var(--font-mono)}}
  .sect-label{{font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;
    color:var(--primary);display:flex;align-items:center;gap:var(--sp-3);margin:0}}
  .sect-label::after{{content:"";flex:1;height:1px;background:var(--outline-variant)}}
  .kgrid{{display:grid;gap:var(--sp-4);grid-template-columns:repeat(auto-fit,minmax(240px,1fr));margin-top:var(--sp-4)}}
  .kcard{{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-5)}}
  .kcard h3{{margin:0 0 var(--sp-2);font:500 16px/22px var(--font-sans);color:var(--primary)}}
  .kcard p{{margin:0;color:var(--on-surface-variant);font:400 14px/20px var(--font-sans)}}
  .ogroup{{display:flex;flex-direction:column;gap:var(--sp-4);margin-top:var(--sp-4)}}
  .ogroup__label{{font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;color:var(--on-surface-muted);margin:0}}
  .ogrid{{display:grid;gap:var(--sp-3);grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}}
  .omod{{display:flex;flex-direction:column;gap:6px;background:var(--surface-container);
    border:1px solid var(--outline-variant);border-radius:var(--radius-md);padding:var(--sp-4);
    transition:border-color .15s,background .15s}}
  .omod:hover{{border-color:var(--primary);background:var(--surface-container-high)}}
  .omod__top{{display:flex;align-items:center;gap:var(--sp-2)}}
  .omod__name{{font:500 15px/20px var(--font-sans);color:var(--on-surface)}}
  .omod__pain{{font:400 13px/18px var(--font-sans);color:var(--on-surface-variant)}}
  .core{{align-self:flex-start;font:500 11px/16px var(--font-mono);color:var(--on-surface-muted)}}
  .omod__open{{align-self:flex-start;font:500 12px/16px var(--font-sans);color:var(--primary);margin-top:2px}}
  .wlist{{list-style:none;margin:var(--sp-4) 0 0;padding:0;display:flex;flex-direction:column;gap:var(--sp-3)}}
  .wlist a.wf{{text-decoration:none;background:var(--surface-container);border:1px solid var(--outline-variant);
    border-radius:var(--radius-md);padding:var(--sp-4);display:flex;flex-direction:column;gap:4px;
    transition:border-color .15s,background .15s}}
  .wlist a.wf:hover{{border-color:var(--primary);background:var(--surface-container-high)}}
  .wf-top{{display:flex;align-items:center;justify-content:space-between;gap:var(--sp-3)}}
  .wf-name{{font:500 14px/20px var(--font-sans);color:var(--primary)}}
  .wf-run{{font:600 12px/16px var(--font-sans);color:var(--primary);opacity:.5;white-space:nowrap}}
  .wlist a.wf:hover .wf-run{{opacity:1}}
  .wf-desc{{font:400 13px/18px var(--font-sans);color:var(--on-surface-variant)}}
  .wf-steps{{font:400 12px/18px var(--font-mono);color:var(--on-surface-muted)}}
  .legend{{font:400 13px/18px var(--font-sans);color:var(--on-surface-muted);margin:0 0 var(--sp-2)}}
</style>
</head>
<body>
<div class="shell">
  <div class="navbar">
    <a class="back" href="/">&larr; Back</a>
    <h1><span class="accent">How it works</span> — Context Runtime</h1>
  </div>
  <p class="lede">One control plane runs your whole business as a fleet of <b>agents</b> — automated
  assistants that carry out tasks for you — on a server you own. Each module is built on a proven
  open-source tool and adds agents on top; the kernel coordinates them, sends every task to the
  cheapest capable AI model, and pauses anything risky for your one-click approval. Everything below
  is running live on demo data for a fictional tenant, <b>Summit Roofing Co.</b></p>

  <div class="flow">
    <span class="node">You</span><span class="arr">→</span>
    <span class="node">Control plane</span><span class="arr">→</span>
    <span class="node">Fleet</span><span class="arr">→</span>
    <span class="node">Router (cheapest model)</span><span class="arr">→</span>
    <span class="node">Module agent</span><span class="arr">→</span>
    <span class="node">OSS core</span>
    <span class="arr">·</span><span class="node gate">money / compliance / infra → approval</span>
  </div>

  <section>
    <h2 class="sect-label">The mission layer — the unit of delivery is a mission</h2>
    <p class="lede" style="margin-top:var(--sp-3)">Above the fleet sits <b>Mission Runtime</b>. The thing you deliver is no longer a release — it is a <b>mission</b>: an executable business objective that is authored, verified, deployed and observed as one governed workflow. Anything that moves money, touches compliance or changes infrastructure pauses for your sign-off, and a mission that fails <b>unwinds its own committed effects</b>.</p>
    <div class="flow" style="margin-top:var(--sp-4)">
      <span class="node">Author</span><span class="arr">→</span>
      <span class="node">Mission CI · simulate · verify · replay</span><span class="arr">→</span>
      <span class="node gate">supply-chain + approval gate</span><span class="arr">→</span>
      <span class="node">Deploy · Terraform + Ansible</span><span class="arr">→</span>
      <span class="node">Run</span><span class="arr">→</span>
      <span class="node">Observe · EXPLAIN + timeline</span>
    </div>
    <div class="kgrid">
      <div class="kcard"><h3>Missions</h3><p>An objective — like <i>Revenue Rescue</i> or <i>New-customer onboarding</i> — planned and run across several modules at once. Every step is event-sourced, so a mission is replayable, resumable and fully auditable.</p></div>
      <div class="kcard"><h3>Mission CI</h3><p>A mission is tested before it ships: simulate the outcome, verify it against evidence, replay it from the log, and regress it against known-good missions. A failing mission is blocked from promotion.</p></div>
      <div class="kcard"><h3>Deployment is a mission</h3><p>Shipping infrastructure runs <i>through</i> the runtime — an infra operator wraps Terraform and Ansible, gated by a supply-chain scan and your approval, and rolled back by the same saga that unwinds any mission.</p></div>
      <div class="kcard"><h3>Monitoring agent</h3><p>A standing container watches the live deployment; when a rule fires it spawns a governed response mission and alerts you for one-click sign-off. The loop runs on the infra, so your own machine can be powered down.</p></div>
    </div>
    <p class="legend" style="margin-top:var(--sp-4)">Full detail: <a href="https://redevops.io/whitepaper-v6" style="color:var(--primary)">Whitepaper v6</a> &middot; <a href="https://redevops.io/sidekick/under-the-hood" style="color:var(--primary)">Sidekick under-the-hood</a> &middot; <a href="https://redevops.io/mission-runtime/under-the-hood" style="color:var(--primary)">Mission Runtime under-the-hood</a></p>
  </section>

  <section>
    <h2 class="sect-label">The kernel — what runs underneath</h2>
    <div class="kgrid">{kernel_html}</div>
  </section>

  <section>
    <h2 class="sect-label">The modules — grouped by what they do</h2>
    <p class="legend">Each module's <b>core</b> is the open-source tool it is built on.</p>
    {modules_html}
  </section>

  <section>
    <h2 class="sect-label">Cross-app missions — click to run in Projects</h2>
    <p class="legend">Each is a real runnable mission (a kernel template over live operators). Click one to open the Projects cockpit with the workflow pre-filled, ready to launch.</p>
    <ul class="wlist">{workflows_html}</ul>
  </section>
</div>
<script>
// Live health dots on the module map.
async function health(){{
  try{{
    const r = await fetch('/api/fleet'); if(!r.ok) return;
    const fleet = await r.json();
    document.querySelectorAll('.omod').forEach(a => {{
      const m = fleet.find(x => x.name === a.dataset.name); if(!m) return;
      const dot = a.querySelector('.omod__dot');
      dot.className = 'dot omod__dot ' + (m.health === 'up' ? 'up' : m.health === 'down' ? 'down' : 'na');
    }});
  }}catch(e){{}}
}}
health(); setInterval(health, 5000);
</script>
</body>
</html>"""


def permissions_page(*, subjects, roles, resource, columns, subject_kinds, resource_kinds, actions, row_scopes) -> str:
    """The kernel Permissions admin: define grants for your apps (subject → resource, row-scope +
    column-mask) and preview live what each subject can see. Served at /permissions."""
    subj_opts = "".join(f'<option value="{html.escape(s)}">' for s in list(subjects) + list(roles))
    sk_opts = "".join(f'<option value="{k}">{k}</option>' for k in subject_kinds)
    rk_opts = "".join(f'<option value="{k}"{" selected" if k==resource["kind"] else ""}>{k}</option>' for k in resource_kinds)
    rs_opts = "".join(f'<option value="{s}">{s}</option>' for s in row_scopes)
    act_boxes = "".join(f'<label class="chip"><input type="checkbox" value="{a}"{" checked" if a=="read" else ""}> {a}</label>' for a in actions)
    mask_boxes = "".join(f'<label class="chip"><input type="checkbox" value="{c}"> {c}</label>' for c in resource["maskable"])
    rowcol_opts = "".join(f'<option value="{c}">' for c in resource["row_columns"])
    tmpl = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Permissions · Agentic OS</title>
<style>__BASE_CSS__
  .wrap{max-width:1240px;margin:0 auto;padding:20px}
  .top{display:flex;align-items:center;gap:12px;margin-bottom:6px}
  .top a{color:var(--on-surface-muted);font-size:13px}
  h1{font-size:21px;margin:2px 0 2px}
  .sub{color:var(--on-surface-muted);margin:0 0 18px;max-width:920px}
  .card{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:16px;margin-bottom:18px}
  .card h2{font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:var(--on-surface-muted);margin:0 0 12px}
  code{background:#000;border:1px solid var(--outline-variant);border-radius:6px;padding:1px 6px;color:var(--primary);font-family:var(--font-mono)}
  .form{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end}
  .f{display:flex;flex-direction:column;gap:5px}
  .f label.l{font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:var(--on-surface-muted)}
  input,select{background:#0e0e10;border:1px solid var(--outline-variant);border-radius:8px;color:var(--on-surface);padding:7px 9px;font:inherit;font-size:13px}
  input[type=text]{min-width:150px}
  .chips{display:flex;gap:7px;flex-wrap:wrap}
  .chip{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--outline-variant);border-radius:var(--radius-pill);padding:5px 10px;font-size:12px;cursor:pointer;user-select:none}
  .chip input{padding:0;margin:0}
  .btn{background:var(--primary);color:var(--on-primary);border:0;border-radius:9px;padding:8px 15px;font-weight:600;cursor:pointer;font:inherit}
  .btn.ghost{background:transparent;color:var(--on-surface-variant);border:1px solid var(--outline-variant)}
  table{border-collapse:collapse;width:100%;font-size:12.5px}
  th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--outline-variant);white-space:nowrap}
  th{color:var(--on-surface-muted);text-transform:uppercase;font-size:10px;letter-spacing:.4px}
  .gtable td .pill{font-family:var(--font-mono);font-size:11px;color:var(--on-surface-variant)}
  .del{background:transparent;border:1px solid var(--outline-variant);color:var(--danger);border-radius:6px;padding:2px 8px;cursor:pointer}
  .panels{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media(max-width:900px){.panels{grid-template-columns:1fr}}
  .panel{background:var(--surface-container-low);border:1px solid var(--outline-variant);border-radius:var(--radius-md);overflow:hidden}
  .panel.lim{border-color:var(--primary)}
  .phead{padding:11px 13px;border-bottom:1px solid var(--outline-variant);display:flex;justify-content:space-between;align-items:center;gap:10px}
  .phead b{font-size:13px} .phead small{display:block;color:var(--on-surface-muted);font-weight:400;font-size:11px}
  .count{background:#000;border:1px solid var(--outline-variant);border-radius:var(--radius-pill);padding:3px 10px;font-family:var(--font-mono);font-size:11px;white-space:nowrap}
  .decision{padding:8px 13px;font-size:12px;border-bottom:1px solid var(--outline-variant)}
  .decision.ok{color:#8fe3d6} .decision.deny{color:#ff9b9b}
  .ptbl{overflow:auto;max-height:52vh}
  .mask{color:#5c5c63;font-style:italic}
  .badge{font-family:var(--font-mono);font-size:10px;border:1px solid var(--outline-variant);border-radius:4px;padding:1px 5px;color:var(--on-surface-muted);text-transform:uppercase}
  .withheld{padding:9px 13px;color:var(--on-surface-muted);font-size:12px;border-top:1px dashed var(--outline-variant);text-align:center}
  .deny-box{padding:20px;text-align:center;color:#ff9b9b}
  .note{color:var(--on-surface-muted);font-size:11.5px;margin-top:6px}
</style></head><body><div class="wrap">
<div class="top"><a href="/overview">&larr; Agentic OS</a><span class="dot up"></span></div>
<h1>&#128273; Permissions &mdash; access control for your apps</h1>
<p class="sub">Define who may read which data, at row + column granularity, and preview live what each subject sees. Grants persist to a store the plane enforces on every tool call (privileged bypass &rarr; database grant &rarr; table grant &rarr; row scope &rarr; column mask). Editing here updates the <b>preview</b>; a client app enforces the same grants by installing the authorizer over the shared store.</p>
<p class="sub">This is the <b>data-access</b> plane. The layer beneath it &mdash; per-call security decisions, boundary telemetry, and correlation &amp; containment of a <em>series</em> of calls &mdash; is the v0.3.0 intrinsic-security plane: <a href="/security">Intrinsic Security &amp; Telemetry &rarr;</a></p>
<div style="display:flex;gap:12px;align-items:center;margin-bottom:16px;flex-wrap:wrap">
  <span id="pstatus" class="badge" style="font-size:11px">checking…</span>
  <input id="apikey" type="password" placeholder="admin X-API-Key (if set)" style="width:230px" oninput="saveKey()">
  <span class="note">grants are AES-GCM encrypted at rest &amp; fail-closed; the write API is gated by this key.</span>
</div>

<div class="card"><h2>Add a grant</h2>
<div class="form">
  <div class="f"><label class="l">Subject</label>
    <div style="display:flex;gap:6px"><select id="sk">__SK__</select>
    <input type="text" id="subj" list="subjlist" placeholder="app / role / user"><datalist id="subjlist">__SUBJOPTS__</datalist></div></div>
  <div class="f"><label class="l">Resource</label>
    <div style="display:flex;gap:6px"><select id="rk">__RK__</select>
    <input type="text" id="rname" value="__RESOURCE__"></div></div>
  <div class="f"><label class="l">Actions</label><div class="chips" id="acts">__ACTS__</div></div>
  <div class="f"><label class="l">Row scope</label>
    <div style="display:flex;gap:6px;align-items:center"><select id="rs">__RS__</select>
    <input type="text" id="rcol" list="rowcols" placeholder="column" value="owner" style="width:100px"><datalist id="rowcols">__ROWCOLS__</datalist>
    <input type="text" id="rvals" placeholder="values (comma)" style="width:150px"></div></div>
  <div class="f"><label class="l">Column mask</label><div class="chips" id="masks">__MASKS__</div></div>
  <button class="btn" onclick="addGrant()">Add grant</button>
</div>
<div class="note">Row scope: <b>all</b> = every row &middot; <b>own</b> = rows where <code>column</code> = the user &middot; <b>in</b> = rows where <code>column</code> &isin; the comma values.</div>
</div>

<div class="card"><h2>Grants</h2><div class="ptbl"><table class="gtable"><thead><tr>
  <th>Subject</th><th>Resource</th><th>Actions</th><th>Row scope</th><th>Column mask</th><th></th></tr></thead>
  <tbody id="grows"><tr><td colspan="6" class="mask">loading…</td></tr></tbody></table></div></div>

<div class="card"><h2>Preview &mdash; what a subject can see</h2>
<div class="form" style="margin-bottom:14px">
  <div class="f"><label class="l">Preview as</label>
    <div style="display:flex;gap:6px"><select id="psk">__SK__</select>
    <input type="text" id="psubj" list="subjlist" placeholder="app / role / user" value="support"></div></div>
  <button class="btn ghost" onclick="runPreview()">Preview</button>
</div>
<div class="panels">
  <div class="panel"><div class="phead"><div><b>Full access</b><small>role: admin &middot; permissionless</small></div><span class="count" id="fcount">&mdash;</span></div>
    <div class="decision ok" id="fdec"></div><div class="ptbl" id="ftbl"></div></div>
  <div class="panel lim"><div class="phead"><div><b id="psub">subject</b><small>permissioned</small></div><span class="count" id="lcount">&mdash;</span></div>
    <div class="decision" id="ldec"></div><div class="ptbl" id="ltbl"></div></div>
</div></div>
</div>
<script>
const COLS=__COLUMNS__;
function esc(s){return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
function checked(id){return Array.from(document.querySelectorAll("#"+id+" input:checked")).map(x=>x.value);}
async function loadGrants(){
  const r=await fetch("api/permissions/grants"); const gs=await r.json();
  const tb=document.getElementById("grows");
  if(!gs.length){ tb.innerHTML='<tr><td colspan="6" class="mask">no grants yet — add one above</td></tr>'; return; }
  tb.innerHTML=gs.map(function(g){
    const scope = g.row_scope==="all"?"all rows":(g.row_scope==="own"?("own · "+esc(g.row_column)):(esc(g.row_column)+" ∈ ["+(g.row_values||[]).map(esc).join(", ")+"]"));
    return '<tr><td><span class="badge">'+esc(g.subject_kind)+'</span> '+esc(g.subject)+'</td>'+
      '<td class="pill">'+esc(g.resource_kind)+':'+esc(g.resource_name)+'</td>'+
      '<td>'+(g.actions||[]).map(esc).join(", ")+'</td>'+
      '<td>'+scope+'</td>'+
      '<td class="mask">'+((g.masked_columns||[]).map(esc).join(", ")||"—")+'</td>'+
      '<td><button class="del" onclick="delGrant(\''+g.id+'\')">remove</button></td></tr>';
  }).join("");
}
async function addGrant(){
  const body={subject_kind:document.getElementById("sk").value, subject:document.getElementById("subj").value.trim(),
    resource_kind:document.getElementById("rk").value, resource_name:document.getElementById("rname").value.trim(),
    actions:checked("acts"), row_scope:document.getElementById("rs").value,
    row_column:document.getElementById("rcol").value.trim()||"owner",
    row_values:document.getElementById("rvals").value.split(",").map(s=>s.trim()).filter(Boolean),
    masked_columns:checked("masks")};
  if(!body.subject||!body.resource_name){alert("subject and resource are required");return;}
  const r=await fetch("api/permissions/grants",{method:"POST",headers:wHeaders(),body:JSON.stringify(body)});
  if(r.status===401){alert("Admin X-API-Key required or invalid.");return;}
  if(!r.ok){alert("Failed: "+(await r.text()));return;}
  await loadGrants(); runPreview(); loadStatus();
}
async function delGrant(id){
  const r=await fetch("api/permissions/grants/"+id,{method:"DELETE",headers:wHeaders()});
  if(r.status===401){alert("Admin X-API-Key required or invalid.");return;}
  await loadGrants(); runPreview(); loadStatus();
}
function saveKey(){try{localStorage.setItem("cp_api_key",document.getElementById("apikey").value);}catch(e){}}
function wHeaders(){const h={"Content-Type":"application/json"};const k=document.getElementById("apikey").value;if(k)h["X-API-Key"]=k;return h;}
async function loadStatus(){
  try{const s=await (await fetch("api/permissions/status")).json();const el=document.getElementById("pstatus");
    if(s.sealed){el.textContent="⛔ store SEALED — tamper/wrong key (failing closed)";el.style.color="#ff9b9b";el.style.borderColor="#ff9b9b";}
    else if(s.encrypted){el.textContent="🔒 encrypted at rest · AES-GCM · key:"+s.key_source+" · "+s.count+" grants";el.style.color="#8fe3d6";}
    else{el.textContent="⚠ PLAINTEXT — set PERMISSIONS_KEY";el.style.color="#e7d488";}
  }catch(e){}
}
function recTable(records, mode, withheld, decision){
  if(mode==="lim" && decision && !decision.allowed) return '<div class="deny-box">&#10007; '+esc(decision.reason)+'</div>';
  let h='<table><thead><tr>'+COLS.map(c=>'<th>'+esc(c)+'</th>').join("")+'</tr></thead><tbody>';
  for(const r of records){
    const masked=(mode==="lim")?(r._masked||[]):[];
    h+='<tr>'+COLS.map(function(c){
      if(masked.indexOf(c)>=0) return '<td class="mask">•••</td>';
      const v=r[c]; return '<td>'+(v==null?'<span class="mask">—</span>':esc(v))+'</td>';
    }).join("")+'</tr>';
  }
  h+='</tbody></table>';
  if(mode==="lim" && withheld>0) h+='<div class="withheld">&#128274; '+withheld+' row(s) withheld · outside the row scope grant</div>';
  return h;
}
async function runPreview(){
  const sk=document.getElementById("psk").value, subj=document.getElementById("psubj").value.trim();
  document.getElementById("psub").textContent=sk+": "+(subj||"—");
  const r=await fetch("api/permissions/preview",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({subject_kind:sk,subject:subj})});
  const d=await r.json();
  document.getElementById("fcount").textContent=d.full.count+" rows";
  const L=d.limited; document.getElementById("lcount").textContent=L.count+" of "+d.full.count+" rows"+(L.withheld?(" · "+L.withheld+" withheld"):"");
  document.getElementById("fdec").className="decision ok"; document.getElementById("fdec").textContent="✔ allow · privileged (full access)";
  const ld=document.getElementById("ldec"); ld.className="decision "+(L.decision.allowed?"ok":"deny");
  ld.textContent=(L.decision.allowed?"✔ ":"✕ ")+L.decision.reason;
  document.getElementById("ftbl").innerHTML=recTable(d.full.records,"full",0,null);
  document.getElementById("ltbl").innerHTML=recTable(L.records,"lim",L.withheld,L.decision);
}
try{document.getElementById("apikey").value=localStorage.getItem("cp_api_key")||"";}catch(e){}
loadStatus(); loadGrants(); runPreview();
</script></body></html>"""
    return (tmpl
            .replace("__BASE_CSS__", _BASE_CSS)
            .replace("__RESOURCE__", html.escape(resource["name"]))
            .replace("__COLUMNS__", json.dumps(columns))
            .replace("__SUBJOPTS__", subj_opts)
            .replace("__SK__", sk_opts)
            .replace("__RK__", rk_opts)
            .replace("__RS__", rs_opts)
            .replace("__ACTS__", act_boxes)
            .replace("__MASKS__", mask_boxes)
            .replace("__ROWCOLS__", rowcol_opts))

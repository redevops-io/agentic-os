# Dark-Theme Material Design 3 Dashboard Spec (hand-codeable CSS)

Implement these EXACT tokens + patterns. Dark theme. No JS libs. MD3 rules: dark elevation = lighter surface tone (NOT shadow); Roboto 400/500 only; 8dp grid; state-layer hover .08; numerics in mono + tabular figures; inverted pyramid (KPIs top-left → trends → detail tables); equal card heights per row snapped to a 12-col grid.

## (A) Design tokens — paste into every page's <style>
```css
:root{
  --surface-dim:#0e0e11; --surface:#131316; --surface-bright:#393a3d;
  --surface-container-lowest:#0d0e10; --surface-container-low:#1b1b1f;
  --surface-container:#1f1f23; --surface-container-high:#2a2a2e; --surface-container-highest:#353539;
  --on-surface:#e4e2e6; --on-surface-variant:#c7c5ca; --on-surface-muted:#918f96;
  --outline:#938f99; --outline-variant:#2f2f33;
  --primary:#4fd1c5; --on-primary:#00201c; --primary-container:#00504a; --on-primary-container:#a8f0e6;
  --secondary:#f5b544; --on-secondary:#3d2e00; --secondary-container:#5c4500;
  --success:#5bd98a; --success-container:#0f3d22; --warning:#f5b544; --warning-container:#4a3500;
  --danger:#f2544f; --danger-container:#5c1512; --info:#5aa9f0; --info-container:#103a5c;
  --sp-1:4px;--sp-2:8px;--sp-3:12px;--sp-4:16px;--sp-5:24px;--sp-6:32px;--sp-7:40px;--sp-8:48px;
  --radius-sm:8px;--radius-md:12px;--radius-lg:16px;--radius-xl:28px;--radius-pill:999px;
  --shadow-1:0 1px 2px rgba(0,0,0,.45);--shadow-2:0 2px 6px rgba(0,0,0,.5);
  --font-sans:"Roboto",system-ui,-apple-system,"Segoe UI",sans-serif;
  --font-mono:"Roboto Mono",ui-monospace,"SF Mono",monospace;
}
.display-l{font:400 57px/64px var(--font-sans);letter-spacing:-.25px}
.headline-m{font:400 28px/36px var(--font-sans)} .headline-s{font:400 24px/32px var(--font-sans)}
.title-l{font:400 22px/28px var(--font-sans)} .title-m{font:500 16px/24px var(--font-sans);letter-spacing:.15px}
.title-s{font:500 14px/20px var(--font-sans)} .body-m{font:400 14px/20px var(--font-sans)}
.body-s{font:400 12px/16px var(--font-sans)} .label-m{font:500 12px/16px var(--font-sans);letter-spacing:.5px}
```
Usage: page bg `--surface`; section bands `--surface-container-low`; default cards `--surface-container`; hover/raised `--surface-container-high`; inputs/modals `--surface-container-highest`. Big KPI numbers → mono, tabular figures (`font-feature-settings:"tnum"`). Card titles → `.title-m`. Labels/pills → `.label-m` uppercase.

## (B) Reusable patterns
```css
.page{background:var(--surface);color:var(--on-surface);font-family:var(--font-sans);padding:var(--sp-5);margin:0}
.shell{max-width:1440px;margin-inline:auto;display:flex;flex-direction:column;gap:var(--sp-5)}
.grid{display:grid;gap:var(--sp-4);grid-template-columns:repeat(12,1fr)}
.kpi-row{display:grid;gap:var(--sp-4);grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
.col-3{grid-column:span 3}.col-4{grid-column:span 4}.col-6{grid-column:span 6}.col-8{grid-column:span 8}.col-12{grid-column:span 12}
@media(max-width:839px){[class^="col-"]{grid-column:span 12}}
.card{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-5);display:flex;flex-direction:column;gap:var(--sp-4)}
.card__head{display:flex;align-items:center;justify-content:space-between}
.tile{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-4) var(--sp-5);display:flex;flex-direction:column;gap:var(--sp-1)}
.tile__label{font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;color:var(--on-surface-muted)}
.tile__value{font:500 32px/40px var(--font-mono);color:var(--on-surface);font-feature-settings:"tnum"}
.tile__delta{font:500 12px/16px var(--font-sans)} .tile__delta--up{color:var(--success)} .tile__delta--down{color:var(--danger)}
.pill{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 10px;border-radius:var(--radius-pill);font:500 12px/1 var(--font-sans)}
.pill--success{background:var(--success-container);color:var(--success)}.pill--warn{background:var(--warning-container);color:var(--warning)}
.pill--danger{background:var(--danger-container);color:var(--danger)}.pill--info{background:var(--info-container);color:var(--info)}
.pill__dot{width:6px;height:6px;border-radius:50%;background:currentColor}
.table{width:100%;border-collapse:collapse;font-size:14px}
.table th{text-align:left;color:var(--on-surface-muted);font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;padding:var(--sp-3) var(--sp-4);border-bottom:1px solid var(--outline-variant)}
.table td{padding:var(--sp-3) var(--sp-4);color:var(--on-surface);border-bottom:1px solid var(--outline-variant)}
.table td.num{text-align:right;font-family:var(--font-mono);font-feature-settings:"tnum"}
.table tbody tr:hover{background:rgba(228,226,230,.08)}
.banner{display:flex;align-items:center;gap:var(--sp-4);padding:var(--sp-4) var(--sp-5);border-radius:var(--radius-md);border-left:4px solid var(--warning);background:var(--warning-container);color:var(--on-surface)}
.bar{height:8px;border-radius:var(--radius-pill);background:var(--surface-container-highest);overflow:hidden}
.bar>span{display:block;height:100%;background:var(--primary)}
```
- Sparkline: inline ~88×26 SVG `<polyline stroke=var(--primary) fill=none stroke-width=2>`, no axes.
- Empty state: centered muted text in a card.
- Section header: small uppercase `.label-m` in `--primary` + thin divider, then the grid band.

## (C) Per-category layout (top→bottom)
Each module dashboard: header (module name + "Summit Roofing Co." tenant + green "agent active" pill) → KPI summary row (4-6 tiles) → mid trends/breakdowns → bottom detail table/queue. Approval-gated modules show the `.banner` directly under the header.
1. **agentic-billing** (Stripe-style): KPIs collected/MRR · outstanding · success rate · active jobs. Mid: gross-volume sparkline + collected-vs-failed bars. Bottom: invoice table (status pills) + overdue queue.
2. **agentic-books** (QuickBooks/Xero): KPIs cash position · net income · A/R · A/P. Mid: P&L summary (income/expense/net bars) + cash trend. Bottom: uncategorized-txn queue + month-end close checklist with progress bar.
3. **agentic-support** (Zendesk/Intercom): KPIs open tickets · first-response vs SLA · resolution · CSAT. Mid: SLA pills + channel breakdown bars. Bottom: ticket queue table (priority pill, age, SLA countdown).
4. **social-autopilot** (Hootsuite/Buffer): KPIs scheduled · engagement · reach · follower delta. Mid: publishing queue (next 7 days). Bottom: per-network stats table.
5. **edge-sentinel** (SOC/SIEM): status banner first (green normal/red threat). KPIs threats blocked 24h · open alerts by severity · devices online/total · detection time. Mid: alert feed grouped by severity (color pills) + attack-trend sparkline. Bottom: device/asset table.
6. **agentic-compliance** (Vanta/Drata): KPIs control pass rate% · passing/failing · frameworks · evidence freshness. Mid: framework-status cards (SOC2/ISO/HIPAA progress bars). Bottom: expiring/failing items queue (owner, due-date, stale pill).
7. **control-tower** (Metabase/Looker): "ask anything" pill bar at top. KPI scorecards (each w/ sparkline+delta). Mid: large revenue trend (6 mo CSS bars). Bottom: breakdown table (bar-in-cell).
8. **growth-engine** (GA4/HubSpot): KPIs leads · conversions · CAC/CPL · ROAS · spend. Mid: conversion funnel bars + channel trend. Bottom: channel attribution table (source, spend, conv, CPL, ROAS color-coded).
9. **market-radar** (Crayon/Klue): KPIs competitors tracked · changes this week · price moves · new campaigns. Mid: competitor cards grid (positioning, last-change pill). Bottom: change feed (timestamp, competitor, change-type pill) + price-move table.

## Control-plane dashboard (the fleet home)
- Tighten the top: header row (title + "Run your whole business on agents" + tenant line + "N/15 up" pill) as a compact app bar on `--surface-container-low`. Move **Approvals** + **Cross-module workflows** into a SLIM full-width band (two equal cards, not a huge empty block) — keep them compact, not dominating the fold.
- Module groups: each functional group is a section with a small uppercase `--primary` label + divider, then an auto-fit grid of EQUAL-HEIGHT module cards. Cards: repo visual (16:9, object-cover, radius top), title + status pill, pain label, tagline, agent chips, approval-gated note, footer with "Open dashboard" filled teal button + "source ↗". Snap to the grid; consistent gaps `--sp-4`; consistent card heights per row.
- Keep live `/api/fleet` polling, `/m/{name}` proxy, `/assets` mount.

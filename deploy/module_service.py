"""Per-module live activity dashboard for the Summit Roofing Co. demo tenant.

This is the demo stand-in service that every module gets so the control-plane fleet
lights up green. Instead of a bare placeholder page, each module renders a RICH,
self-contained dark-themed activity dashboard for ONE fictional tenant —
"Summit Roofing Co.", a ~12-person local roofing contractor. All data is mock data
driven from the MODULE_DATA dict keyed by MODULE_NAME.

The whole UI is a hand-coded dark Material Design 3 system (see deploy/MD3_SPEC.md):
MD3 tokens + reusable patterns live in BASE_CSS, reused by every module page. Each
module renders to its category-appropriate layout (billing, books, support, social,
SIEM, compliance, BI, growth, market-intel). No external libraries, no JS charting —
inline CSS only (flex/grid, KPI tiles, CSS bar meters, inline SVG sparklines, tables).

Endpoints:
  GET /health         -> {"status": "ok", "module": NAME}
  GET /info           -> {"module": NAME, "pain": PAIN, "agents": [...]}
  GET /               -> rich HTML activity dashboard (HTMLResponse)
  GET /api/activity   -> the module's mock data as JSON

Configured entirely via environment:
  MODULE_NAME    e.g. "edge-sentinel"
  MODULE_PAIN    e.g. "network security & systems handled"
  MODULE_AGENTS  comma-separated, e.g. "triage,remediation"
  PORT           port to bind (uvicorn), default 8000

    uvicorn deploy.module_service:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import html
import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

NAME = os.environ.get("MODULE_NAME", "module")
PAIN = os.environ.get("MODULE_PAIN", "")
AGENTS = [a.strip() for a in os.environ.get("MODULE_AGENTS", "").split(",") if a.strip()]

TENANT = "Summit Roofing Co."
TENANT_SUB = "demo tenant"

app = FastAPI(title=f"{NAME} (Summit Roofing Co. demo)")


# --- per-module mock data ----------------------------------------------------
# Every module's dashboard is driven from this dict. Each entry describes a
# believable slice of Summit Roofing Co.'s day-to-day, agent-run operations.
MODULE_DATA: dict[str, dict] = {
    "edge-sentinel": {
        "display": "Edge Sentinel",
        "subtitle": "Network security & systems, triaged and explained by an agent.",
        "kpis": [
            {"label": "Threats blocked today", "value": "37", "note": "+9 vs yesterday"},
            {"label": "Devices on shop wifi", "value": "14", "note": "2 new this week"},
            {"label": "Firewall", "value": "UP", "note": "WAN + LAN healthy"},
            {"label": "Last scan", "value": "6m ago", "note": "Suricata + nmap sweep"},
        ],
        "approval": {
            "action": "remediation",
            "text": "Remediation: block IP 45.143.x.x at the edge firewall (repeated port scans against the office NVR).",
        },
        "table": {
            "title": "Recent security alerts",
            "head": ["Time", "Event", "Source", "Action"],
            "rows": [
                ["08:14", "Blocked port scan 45.143.x → office NVR", "WAN", "auto-blocked"],
                ["07:52", "New device joined: Crew-iPad-3", "Shop wifi", "tagged, trusted"],
                ["07:09", "Suricata: ET SCAN nmap probe", "WAN", "logged"],
                ["Yesterday", "Failed VPN login ×3 (estimator laptop)", "Remote", "rate-limited"],
                ["Yesterday", "Firmware update applied to gateway", "LAN", "complete"],
            ],
        },
        "bars": {
            "title": "Blocked events by category (today)",
            "items": [
                {"label": "Port / network scans", "pct": 54},
                {"label": "Malware / C2", "pct": 24},
                {"label": "Brute-force logins", "pct": 16},
                {"label": "Other", "pct": 6},
            ],
        },
    },
    "agentic-support": {
        "display": "Agentic Support",
        "subtitle": "Front-line support that answers, schedules, and escalates — 24/7.",
        "kpis": [
            {"label": "Open tickets", "value": "8", "note": "3 awaiting customer"},
            {"label": "Avg first response", "value": "4m", "note": "SLA 30m"},
            {"label": "Resolved this week", "value": "41", "note": "+12% WoW"},
            {"label": "CSAT", "value": "4.8", "note": "of 5.0 · 96 ratings"},
        ],
        "approval": None,
        "feed": {
            "title": "Live ticket feed",
            "items": [
                {"who": "Website", "what": "Quote request — 2,200 sqft asphalt re-roof, Henderson", "when": "9m ago", "tag": "new"},
                {"who": "Phone", "what": "Reschedule — rain delay, Tue crew → Thu", "when": "22m ago", "tag": "scheduling"},
                {"who": "Email", "what": "Warranty — minor leak at ridge, Oak Park job", "when": "1h ago", "tag": "warranty"},
                {"who": "Facebook", "what": "DM — 'do you do gutter guards?'", "when": "2h ago", "tag": "answered"},
                {"who": "Phone", "what": "Estimate follow-up — Maple St gutters", "when": "3h ago", "tag": "follow-up"},
            ],
        },
        "bars": {
            "title": "Tickets by channel (this week)",
            "items": [
                {"label": "Phone", "pct": 44},
                {"label": "Website", "pct": 28},
                {"label": "Email", "pct": 18},
                {"label": "Facebook", "pct": 10},
            ],
        },
    },
    "agentic-billing": {
        "display": "Agentic Billing",
        "subtitle": "Checkout to reconciliation — with a human in the loop when money moves.",
        "kpis": [
            {"label": "Collected MTD", "value": "$148,200", "note": "June"},
            {"label": "Outstanding", "value": "$19,400", "note": "4 invoices"},
            {"label": "Payment success", "value": "97%", "note": "card + ACH"},
            {"label": "Avg days to pay", "value": "11", "note": "net-30 terms"},
        ],
        "approval": {
            "action": "refund",
            "text": "Refund: $450 — cancelled roof inspection (customer rescheduled then cancelled).",
        },
        "table": {
            "title": "Recent invoices",
            "head": ["Invoice", "Job", "Amount", "Status"],
            "rows": [
                ["#1042", "Henderson asphalt re-roof", "$14,200", "PAID"],
                ["#1048", "Maple St gutters", "$2,300", "OVERDUE 6d"],
                ["#1051", "Commercial flat-roof deposit", "$8,000", "PAID"],
                ["#1052", "Oak Park ridge repair (warranty)", "$0", "NO CHARGE"],
                ["#1053", "Elm Ave tear-off + felt", "$6,750", "SENT"],
            ],
        },
        "bars": {
            "title": "Collections by method (MTD)",
            "items": [
                {"label": "ACH / bank transfer", "pct": 52},
                {"label": "Card", "pct": 33},
                {"label": "Check (logged)", "pct": 15},
            ],
        },
    },
    "agentic-books": {
        "display": "Agentic Books",
        "subtitle": "Books that categorize, reconcile, and close themselves.",
        "kpis": [
            {"label": "Revenue MTD", "value": "$148k", "note": "June"},
            {"label": "Materials", "value": "$52k", "note": "shingles, felt, metal"},
            {"label": "Payroll", "value": "$61k", "note": "12 crew + office"},
            {"label": "Net", "value": "$21k", "note": "14% margin"},
        ],
        "approval": {
            "action": "close",
            "text": "Month-end close for June is staged (80% complete) and awaiting your approval to post.",
        },
        "bars": {
            "title": "P&L this month",
            "items": [
                {"label": "Revenue", "pct": 100},
                {"label": "Materials (COGS)", "pct": 35},
                {"label": "Payroll", "pct": 41},
                {"label": "Overhead", "pct": 10},
                {"label": "Net profit", "pct": 14},
            ],
        },
        "table": {
            "title": "Bookkeeping status",
            "head": ["Item", "Detail", "State"],
            "rows": [
                ["Uncategorized txns", "7 awaiting category", "needs review"],
                ["Bank reconciliation", "Operating + payroll accounts", "matched"],
                ["Month-end close", "June — 80% complete", "awaiting approval"],
                ["Sales tax accrual", "Q2 estimate", "on track"],
            ],
        },
    },
    "agentic-compliance": {
        "display": "Agentic Compliance",
        "subtitle": "Continuous license, insurance & safety monitoring — audit-ready.",
        "kpis": [
            {"label": "Licenses tracked", "value": "4", "note": "1 expiring soon"},
            {"label": "Insurance policies", "value": "3", "note": "GL, WC, auto"},
            {"label": "Crew safety trained", "value": "11/12", "note": "OSHA fall protection"},
            {"label": "Open findings", "value": "1", "note": "GL renewal due"},
        ],
        "approval": {
            "action": "policy_change",
            "text": "Policy change: increase General Liability coverage to $2M before renewal — awaiting approval.",
        },
        "table": {
            "title": "License & insurance status",
            "head": ["Item", "Status", "Expires"],
            "rows": [
                ["State contractor license", "ACTIVE", "2027-03"],
                ["General Liability insurance", "⚠ 80 days", "2026-09"],
                ["Workers' Comp insurance", "ACTIVE", "2026-12"],
                ["Commercial auto policy", "ACTIVE", "2027-01"],
                ["OSHA fall-protection training", "11/12 crew", "annual"],
            ],
        },
    },
    "control-tower": {
        "display": "Control Tower",
        "subtitle": "The owner's single pane — ask your business anything, in plain language.",
        "kpis": [
            {"label": "Jobs in progress", "value": "6", "note": "2 commercial"},
            {"label": "Won this month", "value": "11", "note": "of 19 bids"},
            {"label": "Revenue MTD", "value": "$148k", "note": "+18% YoY"},
            {"label": "Crew utilization", "value": "86%", "note": "target 80%"},
        ],
        "approval": None,
        "ask": "Show me which jobs are at risk of running over budget this week.",
        "bars": {
            "title": "Revenue, last 6 months",
            "items": [
                {"label": "Jan", "pct": 58},
                {"label": "Feb", "pct": 52},
                {"label": "Mar", "pct": 71},
                {"label": "Apr", "pct": 83},
                {"label": "May", "pct": 90},
                {"label": "Jun", "pct": 100},
            ],
        },
        "table": {
            "title": "Headline metrics",
            "head": ["Metric", "Value", "Trend"],
            "rows": [
                ["Win rate", "58%", "↑ up from 51%"],
                ["Avg job value", "$13,400", "↑"],
                ["Backlog", "$214k", "→ steady"],
                ["Pipeline (open bids)", "$96k", "↑"],
            ],
        },
    },
    "market-radar": {
        "display": "Market Radar",
        "subtitle": "Watch every competitor and get briefed before they move.",
        "kpis": [
            {"label": "Competitors tracked", "value": "6", "note": "within 15 mi"},
            {"label": "Price moves (7d)", "value": "3", "note": "2 down, 1 up"},
            {"label": "New entrants", "value": "1", "note": "Peak Exteriors"},
            {"label": "Your rank", "value": "#2", "note": "'roofing' local"},
        ],
        "approval": None,
        "competitors": [
            {"name": "Apex Roofing", "position": "Price leader · #1 local", "change": "price ↓", "tone": "danger"},
            {"name": "Peak Exteriors", "position": "New entrant · 3 mi away", "change": "new", "tone": "warn"},
            {"name": "Ridgeline Contractors", "position": "Now offering metal roofs", "change": "new service", "tone": "info"},
            {"name": "BlueSky Exteriors", "position": "Running spring promo", "change": "promo", "tone": "warn"},
        ],
        "table": {
            "title": "Competitor watch",
            "head": ["Competitor", "Signal", "Note"],
            "rows": [
                ["Apex Roofing", "Asphalt $4.10/sqft", "↓ from $4.45"],
                ["Peak Exteriors", "NEW competitor 3mi away", "just opened"],
                ["Summit Roofing", "Rank #2 on 'roofing <town>'", "Apex is #1"],
                ["Ridgeline Contractors", "Now offering metal roofs", "new service"],
                ["BlueSky Exteriors", "Running 10% spring promo", "ends Jun 30"],
            ],
        },
        "feed": {
            "title": "Promo & ad watch",
            "items": [
                {"who": "Apex Roofing", "what": "'Free gutter cleaning with re-roof' — Google Ads", "when": "today", "tag": "promo"},
                {"who": "BlueSky", "what": "10% spring discount, expires Jun 30", "when": "2d ago", "tag": "promo"},
                {"who": "Peak Exteriors", "what": "New Facebook page, $300/wk ad spend est.", "when": "4d ago", "tag": "new"},
            ],
        },
    },
    "growth-engine": {
        "display": "Growth Engine",
        "subtitle": "Know what's working and put spend where it pays.",
        "kpis": [
            {"label": "Cost per lead", "value": "$42", "note": "↓ from $51"},
            {"label": "Leads MTD", "value": "73", "note": "+11 vs May"},
            {"label": "ROAS", "value": "5.1x", "note": "blended"},
            {"label": "Booked rate", "value": "34%", "note": "lead → job"},
        ],
        "approval": {
            "action": "budget_change",
            "text": "Budget change: shift $600 from Facebook to Google Ads (Google CPL is 40% lower this month).",
        },
        "funnel": {
            "title": "Lead-to-job funnel (MTD)",
            "items": [
                {"label": "Leads", "pct": 100, "value": "73"},
                {"label": "Qualified", "pct": 70, "value": "51"},
                {"label": "Estimates sent", "pct": 48, "value": "35"},
                {"label": "Booked jobs", "pct": 34, "value": "25"},
            ],
        },
        "bars": {
            "title": "Leads by source (MTD)",
            "items": [
                {"label": "Google Ads", "pct": 38},
                {"label": "Referrals", "pct": 31},
                {"label": "Yard signs", "pct": 18},
                {"label": "Facebook", "pct": 13},
            ],
        },
        "table": {
            "title": "Channel performance",
            "head": ["Channel", "Leads", "Cost / lead", "ROAS"],
            "rows": [
                ["Google Ads", "28", "$38", "5.8x"],
                ["Referrals", "23", "$0", "∞"],
                ["Yard signs", "13", "$21", "4.1x"],
                ["Facebook", "9", "$71", "2.2x"],
            ],
        },
    },
    "social-autopilot": {
        "display": "Social Autopilot",
        "subtitle": "Create, schedule, and engage across social — on autopilot.",
        "kpis": [
            {"label": "Scheduled posts", "value": "6", "note": "next 2 weeks"},
            {"label": "Engagement (7d)", "value": "1,240", "note": "+22%"},
            {"label": "New followers", "value": "+58", "note": "this week"},
            {"label": "Reach (7d)", "value": "9.3k", "note": "FB + Instagram"},
        ],
        "approval": {
            "action": "publish",
            "text": "Publish: 'before/after carousel' from the Oak Park Victorian restoration — awaiting approval.",
        },
        "feed": {
            "title": "Scheduled & published posts",
            "items": [
                {"who": "Instagram", "what": "Completed: Victorian restoration, Oak Park 📸", "when": "Fri 9am", "tag": "scheduled"},
                {"who": "Facebook", "what": "Tip: 5 signs you need a new roof", "when": "Mon 8am", "tag": "scheduled"},
                {"who": "Facebook", "what": "Crew spotlight: meet foreman Dave", "when": "Wed", "tag": "draft"},
                {"who": "Instagram", "what": "Storm-season prep checklist", "when": "Published", "tag": "live"},
            ],
        },
        "bars": {
            "title": "Engagement by platform (7d)",
            "items": [
                {"label": "Facebook", "pct": 56},
                {"label": "Instagram", "pct": 34},
                {"label": "Google Business", "pct": 10},
            ],
        },
    },
}


def _data() -> dict:
    """Return the data block for this module, or a generic themed fallback."""
    if NAME in MODULE_DATA:
        return MODULE_DATA[NAME]
    agents = ", ".join(AGENTS) or "agents"
    return {
        "display": NAME.replace("-", " ").title(),
        "subtitle": PAIN or "Agent-run module for Summit Roofing Co.",
        "kpis": [
            {"label": "Status", "value": "Active", "note": "agent online"},
            {"label": "Agents", "value": str(len(AGENTS) or 1), "note": agents},
            {"label": "Tenant", "value": "Summit", "note": "demo"},
            {"label": "Uptime", "value": "100%", "note": "7d"},
        ],
        "approval": None,
        "table": {
            "title": "Recent activity",
            "head": ["Time", "Event"],
            "rows": [["now", "Module running for " + TENANT]],
        },
    }


# --- shared Material Design 3 (dark) CSS -------------------------------------
# Tokens + reusable patterns from deploy/MD3_SPEC.md sections (A) + (B).
# Reused verbatim by every module page (and mirrored in control_plane.py).
BASE_CSS = """
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
*{box-sizing:border-box}
.display-l{font:400 57px/64px var(--font-sans);letter-spacing:-.25px}
.headline-m{font:400 28px/36px var(--font-sans)} .headline-s{font:400 24px/32px var(--font-sans)}
.title-l{font:400 22px/28px var(--font-sans)} .title-m{font:500 16px/24px var(--font-sans);letter-spacing:.15px}
.title-s{font:500 14px/20px var(--font-sans)} .body-m{font:400 14px/20px var(--font-sans)}
.body-s{font:400 12px/16px var(--font-sans)} .label-m{font:500 12px/16px var(--font-sans);letter-spacing:.5px}

.page{background:var(--surface);color:var(--on-surface);font-family:var(--font-sans);padding:var(--sp-5);margin:0}
.shell{max-width:1440px;margin-inline:auto;display:flex;flex-direction:column;gap:var(--sp-5)}
.grid{display:grid;gap:var(--sp-4);grid-template-columns:repeat(12,1fr)}
.kpi-row{display:grid;gap:var(--sp-4);grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
.col-3{grid-column:span 3}.col-4{grid-column:span 4}.col-6{grid-column:span 6}.col-8{grid-column:span 8}.col-12{grid-column:span 12}
@media(max-width:839px){[class^="col-"]{grid-column:span 12}}
.card{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-5);display:flex;flex-direction:column;gap:var(--sp-4)}
.card__head{display:flex;align-items:center;justify-content:space-between;gap:var(--sp-3)}
.card__title{font:500 16px/24px var(--font-sans);letter-spacing:.15px;color:var(--on-surface);margin:0}
.tile{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-4) var(--sp-5);display:flex;flex-direction:column;gap:var(--sp-1)}
.tile__label{font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;color:var(--on-surface-muted)}
.tile__value{font:500 32px/40px var(--font-mono);color:var(--on-surface);font-feature-settings:"tnum"}
.tile__delta{font:500 12px/16px var(--font-sans);color:var(--on-surface-variant)} .tile__delta--up{color:var(--success)} .tile__delta--down{color:var(--danger)}
.pill{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 10px;border-radius:var(--radius-pill);font:500 12px/1 var(--font-sans)}
.pill--success{background:var(--success-container);color:var(--success)}.pill--warn{background:var(--warning-container);color:var(--warning)}
.pill--danger{background:var(--danger-container);color:var(--danger)}.pill--info{background:var(--info-container);color:var(--info)}
.pill--neutral{background:var(--surface-container-highest);color:var(--on-surface-variant)}
.pill__dot{width:6px;height:6px;border-radius:50%;background:currentColor}
.table{width:100%;border-collapse:collapse;font-size:14px}
.table th{text-align:left;color:var(--on-surface-muted);font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;padding:var(--sp-3) var(--sp-4);border-bottom:1px solid var(--outline-variant)}
.table td{padding:var(--sp-3) var(--sp-4);color:var(--on-surface);border-bottom:1px solid var(--outline-variant)}
.table td.num{text-align:right;font-family:var(--font-mono);font-feature-settings:"tnum"}
.table tbody tr:last-child td{border-bottom:none}
.table tbody tr:hover{background:rgba(228,226,230,.08)}
.banner{display:flex;align-items:center;gap:var(--sp-4);padding:var(--sp-4) var(--sp-5);border-radius:var(--radius-md);border-left:4px solid var(--warning);background:var(--warning-container);color:var(--on-surface)}
.bar{height:8px;border-radius:var(--radius-pill);background:var(--surface-container-highest);overflow:hidden}
.bar>span{display:block;height:100%;background:var(--primary)}
"""

# Per-module styling that builds on BASE_CSS (layout helpers specific to these pages).
PAGE_CSS = """
a{color:var(--primary);text-decoration:none}
.appbar{background:var(--surface-container-low);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-5) var(--sp-5)}
.appbar__row{display:flex;align-items:center;gap:var(--sp-3);flex-wrap:wrap}
.appbar h1{margin:0;font:400 28px/36px var(--font-sans);color:var(--on-surface)}
.appbar__tenant{margin-top:var(--sp-3);color:var(--on-surface-variant);font:400 14px/20px var(--font-sans)}
.appbar__tenant b{color:var(--on-surface)}
.tag{font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;color:var(--secondary);border:1px solid var(--secondary-container);background:var(--secondary-container);color:var(--secondary);padding:1px 8px;border-radius:var(--radius-sm);margin-left:var(--sp-2)}
.appbar__sub{margin-top:var(--sp-2);color:var(--on-surface-muted);font:400 14px/20px var(--font-sans);max-width:780px}
.section-label{font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;color:var(--primary);display:flex;align-items:center;gap:var(--sp-3);margin:0}
.section-label::after{content:"";flex:1;height:1px;background:var(--outline-variant)}
.barlist{display:flex;flex-direction:column;gap:var(--sp-4)}
.barlist__row{display:grid;grid-template-columns:160px 1fr 52px;align-items:center;gap:var(--sp-4)}
.barlist__label{color:var(--on-surface-variant);font:400 14px/20px var(--font-sans)}
.barlist__pct{text-align:right;font-family:var(--font-mono);font-feature-settings:"tnum";font-size:13px;color:var(--on-surface-variant)}
@media(max-width:839px){.barlist__row{grid-template-columns:110px 1fr 44px}}
.feed{list-style:none;margin:0;padding:0;display:flex;flex-direction:column}
.feed__item{display:grid;grid-template-columns:96px 1fr auto auto;gap:var(--sp-4);align-items:center;padding:var(--sp-3) 0;border-bottom:1px solid var(--outline-variant)}
.feed__item:last-child{border-bottom:none}
.feed__who{color:var(--primary);font:500 12px/16px var(--font-sans)}
.feed__what{color:var(--on-surface);font:400 14px/20px var(--font-sans)}
.feed__when{color:var(--on-surface-muted);font:400 12px/16px var(--font-sans);white-space:nowrap;font-family:var(--font-mono)}
@media(max-width:839px){.feed__item{grid-template-columns:1fr auto}}
.ask{display:flex;align-items:center;gap:var(--sp-3);background:var(--surface-container-highest);border:1px solid var(--outline-variant);border-radius:var(--radius-pill);padding:var(--sp-3) var(--sp-5)}
.ask__prompt{color:var(--primary);font-family:var(--font-mono);font-weight:500}
.ask__q{color:var(--on-surface);font:400 14px/20px var(--font-sans)}
.scorecard{display:flex;flex-direction:column;gap:var(--sp-2)}
.scorecard__foot{display:flex;align-items:center;justify-content:space-between;gap:var(--sp-3)}
.compgrid{display:grid;gap:var(--sp-4);grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
.comp{background:var(--surface-container-high);border:1px solid var(--outline-variant);border-radius:var(--radius-md);padding:var(--sp-4);display:flex;flex-direction:column;gap:var(--sp-2)}
.comp__name{font:500 16px/24px var(--font-sans);color:var(--on-surface)}
.comp__pos{color:var(--on-surface-muted);font:400 13px/18px var(--font-sans)}
.footer{color:var(--on-surface-muted);font:400 12px/16px var(--font-sans);text-align:center;padding-top:var(--sp-2)}
"""

FONT_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    'family=Roboto:wght@400;500&family=Roboto+Mono:wght@400;500&display=swap">'
)


# --- HTML rendering (inline CSS only) ----------------------------------------
def _esc(v) -> str:
    return html.escape(str(v))


def _status_pill_for(value: str) -> str:
    """Map a status string to an MD3 pill class for table cells."""
    v = value.upper()
    if any(k in v for k in ("PAID", "ACTIVE", "MATCHED", "COMPLETE", "LIVE", "TRUSTED", "ON TRACK")):
        return "pill--success"
    if any(k in v for k in ("OVERDUE", "FAIL", "DOWN", "BLOCK")):
        return "pill--danger"
    if any(k in v for k in ("⚠", "AWAIT", "NEEDS", "REVIEW", "PENDING", "DAYS", "SENT", "DRAFT", "SCHEDULED")):
        return "pill--warn"
    return "pill--neutral"


def _kpi_tiles(kpis: list[dict]) -> str:
    cells = ""
    for k in kpis:
        note = k.get("note", "")
        cls = "tile__delta"
        if note.startswith("+") or "↓ from" in note or "↑" in note or "up" in note.lower():
            cls += " tile__delta--up"
        elif note.startswith("-") or "↓" in note:
            cls += " tile__delta--down"
        cells += (
            "<div class='tile'>"
            f"<div class='tile__label'>{_esc(k['label'])}</div>"
            f"<div class='tile__value'>{_esc(k['value'])}</div>"
            f"<div class='{cls}'>{_esc(note)}</div>"
            "</div>"
        )
    return f"<section class='kpi-row'>{cells}</section>"


def _banner(ap: dict | None) -> str:
    if not ap:
        return ""
    return (
        "<div class='banner'>"
        "<span class='pill pill--warn'><span class='pill__dot'></span>1 approval</span>"
        f"<span class='label-m' style='text-transform:uppercase;color:var(--warning)'>{_esc(ap['action'])}</span>"
        f"<span class='body-m'>{_esc(ap['text'])}</span>"
        "</div>"
    )


def _barlist(title: str, items: list[dict], show_value: bool = False) -> str:
    rows = ""
    for b in items:
        pct = int(b["pct"])
        right = _esc(b.get("value", f"{pct}%")) if show_value else f"{pct}%"
        rows += (
            "<div class='barlist__row'>"
            f"<div class='barlist__label'>{_esc(b['label'])}</div>"
            f"<div class='bar'><span style='width:{pct}%'></span></div>"
            f"<div class='barlist__pct'>{right}</div>"
            "</div>"
        )
    return (
        "<div class='card'>"
        f"<div class='card__head'><h2 class='card__title'>{_esc(title)}</h2></div>"
        f"<div class='barlist'>{rows}</div>"
        "</div>"
    )


def _table_card(table: dict | None, status_col: int | None = None) -> str:
    if not table:
        return ""
    head = "".join(f"<th>{_esc(h)}</th>" for h in table["head"])
    body = ""
    for row in table["rows"]:
        cells = ""
        for i, c in enumerate(row):
            txt = str(c)
            if status_col is not None and i == status_col:
                pill = _status_pill_for(txt)
                cells += f"<td><span class='pill {pill}'>{_esc(txt)}</span></td>"
            elif i > 0 and any(ch.isdigit() for ch in txt) and ("$" in txt or "%" in txt or "x" in txt or txt.replace(",", "").isdigit()):
                cells += f"<td class='num'>{_esc(txt)}</td>"
            else:
                cells += f"<td>{_esc(c)}</td>"
        body += f"<tr>{cells}</tr>"
    return (
        "<div class='card'>"
        f"<div class='card__head'><h2 class='card__title'>{_esc(table['title'])}</h2></div>"
        f"<table class='table'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        "</div>"
    )


def _feed_card(feed: dict | None) -> str:
    if not feed:
        return ""
    items = ""
    for it in feed["items"]:
        items += (
            "<li class='feed__item'>"
            f"<span class='feed__who'>{_esc(it['who'])}</span>"
            f"<span class='feed__what'>{_esc(it['what'])}</span>"
            f"<span class='feed__when'>{_esc(it['when'])}</span>"
            f"<span class='pill pill--neutral'>{_esc(it.get('tag', ''))}</span>"
            "</li>"
        )
    return (
        "<div class='card'>"
        f"<div class='card__head'><h2 class='card__title'>{_esc(feed['title'])}</h2></div>"
        f"<ul class='feed'>{items}</ul>"
        "</div>"
    )


def _section(label: str, body: str) -> str:
    return (
        "<section class='shell' style='gap:var(--sp-4)'>"
        f"<div class='section-label'>{_esc(label)}</div>{body}</section>"
    )


def _two_col(a: str, b: str) -> str:
    """Two equal-height cards side by side in the 12-col grid."""
    return (
        "<div class='grid'>"
        f"<div class='col-6'>{a}</div><div class='col-6'>{b}</div>"
        "</div>"
    )


def _competitor_grid(comps: list[dict]) -> str:
    tone = {"danger": "pill--danger", "warn": "pill--warn", "info": "pill--info", "success": "pill--success"}
    cells = ""
    for c in comps:
        cells += (
            "<div class='comp'>"
            "<div class='card__head'>"
            f"<span class='comp__name'>{_esc(c['name'])}</span>"
            f"<span class='pill {tone.get(c.get('tone',''), 'pill--neutral')}'>{_esc(c['change'])}</span>"
            "</div>"
            f"<div class='comp__pos'>{_esc(c['position'])}</div>"
            "</div>"
        )
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Competitor positioning</h2></div>"
        f"<div class='compgrid'>{cells}</div>"
        "</div>"
    )


# --- per-category body composition -------------------------------------------
def _mid_and_detail(d: dict) -> str:
    """Default mid (bars) + detail (table/feed) composition, per category layout."""
    name = NAME
    bars = d.get("bars")
    table = d.get("table")
    feed = d.get("feed")

    mid = ""
    detail = ""

    # Market-radar: competitor cards in the mid, feed + table in detail.
    if name == "market-radar":
        mid = _competitor_grid(d.get("competitors", []))
        detail = _two_col(_table_card(table, status_col=None), _feed_card(feed))
        return _section("Positioning", mid) + _section("Change feed", detail)

    # Growth-engine: funnel + source bars mid, channel table detail.
    if name == "growth-engine":
        funnel = d.get("funnel")
        mid = _two_col(
            _barlist(funnel["title"], funnel["items"], show_value=True),
            _barlist(bars["title"], bars["items"]),
        )
        detail = _table_card(table, status_col=None)
        return _section("Funnel & channels", mid) + _section("Attribution", detail)

    # Control-tower: revenue bars mid, breakdown table detail.
    if name == "control-tower":
        mid = _barlist(bars["title"], bars["items"])
        detail = _table_card(table, status_col=None)
        return _section("Revenue trend", mid) + _section("Breakdown", detail)

    # Support / social: bars + feed.
    if feed and bars and not table:
        mid = _two_col(_feed_card(feed), _barlist(bars["title"], bars["items"]))
        return _section("Activity", mid)

    # Billing / edge-sentinel / agentic-books: bars + table.
    if bars and table:
        mid = _two_col(_table_card(table, _status_col_for(d)), _barlist(bars["title"], bars["items"]))
        return _section("Detail", mid)

    # Compliance: table only (status pills).
    if table and not bars:
        return _section("Detail", _table_card(table, _status_col_for(d)))

    # Fallback.
    parts = []
    if bars:
        parts.append(_barlist(bars["title"], bars["items"]))
    if table:
        parts.append(_table_card(table, _status_col_for(d)))
    if feed:
        parts.append(_feed_card(feed))
    return _section("Detail", "".join(parts) or "")


def _status_col_for(d: dict) -> int | None:
    """Which table column holds a status worth rendering as a pill."""
    table = d.get("table")
    if not table:
        return None
    head = [h.lower() for h in table["head"]]
    for key in ("status", "state", "action"):
        if key in head:
            return head.index(key)
    return None


def render(d: dict) -> str:
    # Header
    pill = "<span class='pill pill--success'><span class='pill__dot'></span>agent active</span>"
    ask_html = ""
    if d.get("ask"):
        ask_html = (
            "<section class='shell' style='gap:var(--sp-4)'>"
            "<div class='section-label'>Ask anything</div>"
            "<div class='ask'><span class='ask__prompt'>&gt;_</span>"
            f"<span class='ask__q'>{_esc(d['ask'])}</span></div>"
            "</section>"
        )

    body = (
        _banner(d.get("approval"))
        + _kpi_tiles(d["kpis"])
        + ask_html
        + _mid_and_detail(d)
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(d['display'])} — {_esc(TENANT)}</title>
{FONT_LINK}
<style>{BASE_CSS}{PAGE_CSS}</style>
</head>
<body class="page">
<div class="shell">
  <header class="appbar">
    <div class="appbar__row">
      <h1>{_esc(d['display'])}</h1>
      {pill}
    </div>
    <div class="appbar__tenant"><b>{_esc(TENANT)}</b><span class="tag">{_esc(TENANT_SUB)}</span></div>
    <div class="appbar__sub">{_esc(d['subtitle'])}</div>
  </header>
  {body}
  <footer class="footer">{_esc(NAME)} · live activity for {_esc(TENANT)} (demo) ·
    <a href="/api/activity">/api/activity</a> · part of the redevops.io Agentic Business OS</footer>
</div>
</body>
</html>"""


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "module": NAME}


@app.get("/info")
def info() -> dict:
    return {"module": NAME, "pain": PAIN, "agents": AGENTS}


@app.get("/api/activity")
def activity() -> JSONResponse:
    return JSONResponse({"module": NAME, "tenant": TENANT, **_data()})


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return render(_data())


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))

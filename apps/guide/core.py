"""agentic-guide core — the pure redevops-rag retrieval over the stack's app docs.

No web framework, no context-runtime: just stdlib token-overlap retrieval over the app
corpus, RBAC-filtered. This is the layer the FastAPI app renders from AND the Mission
Runtime operator invokes, so the capability handlers can be tested without booting the
console.

`answer(question, role, llm=...)` takes an optional narration callback — the LLM call
lives in app.py (urllib to REDEVOPS_LLM_BASE_URL), keeping this module dependency-light
and fully deterministic: with llm=None it falls back to a retrieved summary, so the
operator drives it without any network.
"""
from __future__ import annotations

import re
from typing import Callable

DEMO = "https://demo.redevops.io"

# ── the corpus: one usage card per app (what · core · actions · how-to) ──
APP_DOCS: dict[str, dict] = {
    "agentic-billing": {"g": "Money", "core": "Lago", "what": "Checkout → invoicing → reconciliation → dunning, with humans in the loop when money moves.", "actions": ["checkout", "reconciliation", "dunning (approval)", "refund (approval)"], "settings": "Point it at your Lago core (LAGO_API_URL/KEY); set dunning cadence + refund limits."},
    "agentic-books": {"g": "Money", "core": "ERPNext", "what": "Bookkeeping & close: categorize, reconcile, and close the books, learning which ledgers/reports to pull per question.", "actions": ["categorize", "reconcile", "close (approval)"], "settings": "Connect the ERPNext core; set the fiscal calendar + close checklist."},
    "agentic-support": {"g": "Customers", "core": "Chatwoot", "what": "Front-line support that answers, escalates, and never sleeps — learns which KB/ticket/account context to retrieve.", "actions": ["responder", "escalation"], "settings": "Connect Chatwoot; set escalation rules + tone."},
    "social-autopilot": {"g": "Customers", "core": "Postiz", "what": "Organic social presence — content calendar, posting, engagement; learns channel/timing strategy per goal.", "actions": ["content", "engagement", "publish (approval)"], "settings": "Connect Postiz + channels; set the posting cadence."},
    "edge-sentinel": {"g": "Security & Compliance", "core": "CrowdSec", "what": "Your firewall, triaged and explained by an agent; learns which alert sources to pull per incident.", "actions": ["triage", "remediation (approval)"], "settings": "Connect CrowdSec + threat-intel/EDR feeds; set auto-block thresholds."},
    "agentic-compliance": {"g": "Security & Compliance", "core": "OpenSCAP", "what": "Continuous compliance with audit-ready evidence; learns which rule-family evidence to pull per finding.", "actions": ["monitor", "evidence", "policy_change (approval)"], "settings": "Connect OpenSCAP; select the benchmarks (CIS/PCI…)."},
    "control-tower": {"g": "Growth & Intelligence", "core": "Metabase", "what": "Ask your business anything in plain language; learns which Metabase query set to run per question.", "actions": ["analyst"], "settings": "Connect Metabase; map the core question set."},
    "market-radar": {"g": "Growth & Intelligence", "core": "changedetection", "what": "Watch every competitor + buying signal and get briefed before they move; sweeps sources per question.", "actions": ["watcher", "briefer"], "settings": "Add the watches (competitor pages, GitHub/HN/jobs feeds)."},
    "growth-engine": {"g": "Growth & Intelligence", "core": "Umami", "what": "Know what's working and put spend where it pays — lead-source attribution; learns which attribution sources to use per query.", "actions": ["attribution", "channel_optimizer", "budget_change (approval)"], "settings": "Connect Umami; set the attribution windows."},
    "outreach-engine": {"g": "Growth & Intelligence", "core": "Twenty CRM", "what": "Lands pilots: sources buying signals, scores accounts to the ICP, picks the outreach play, drafts the EXPLAIN teardown, syncs approved pilots to Twenty CRM. Nothing sends without approval.", "actions": ["sourcer", "personalizer", "sequencer", "send_sequence (approval)"], "settings": "Set TWENTY_API_KEY (Twenty → Settings → APIs) + GITHUB_TOKEN for live signals."},
    "agentic-crm": {"g": "Other", "core": "ERPNext", "what": "A pipeline that scores, researches, and drafts outreach on a real CRM.", "actions": ["score", "research", "draft", "qualify", "send (approval)"], "settings": "Connect the ERPNext CRM core."},
    "lifecycle": {"g": "Other", "core": "Listmonk", "what": "Klaviyo-style lifecycle email/SMS on a self-hosted Listmonk core.", "actions": ["segment", "campaign", "automation", "send (approval)"], "settings": "Connect Listmonk; define segments + journeys."},
    "agentic-privacy": {"g": "Other", "core": "DSAR/GDPR", "what": "GDPR/CCPA data-subject requests: intake, fulfillment, and a tamper-evident audit trail.", "actions": ["intake", "access", "delete (approval)", "retention"], "settings": "Map the data sources; set SLA + retention policy."},
    "growth-assistant": {"g": "Other", "core": "LLM", "what": "An AI marketing/growth strategist for first-time founders — conversational.", "actions": ["chat"], "settings": "Set REDEVOPS_LLM_BASE_URL/MODEL."},
    "sidekick": {"g": "Build & Platform", "core": "CLI", "what": "Auto-approved coding agents that ship and deploy for you (CLI tool — no dashboard).", "actions": ["coder"], "settings": "Install the CLI; run `sidekick run \"<task>\"`."},
}

# ── RBAC (the v-next demo hook): role → the apps that role may see ──
ROLES: dict[str, list[str]] = {
    "admin": list(APP_DOCS.keys()),
    "sales": ["outreach-engine", "market-radar", "growth-engine", "agentic-crm", "lifecycle", "social-autopilot", "control-tower"],
    "finance": ["agentic-billing", "agentic-books", "control-tower", "agentic-compliance"],
    "security": ["edge-sentinel", "agentic-compliance", "agentic-privacy"],
    "viewer": ["control-tower", "market-radar", "growth-engine"],
}


def visible_apps(role: str) -> list[str]:
    """The apps this principal may see — v-next RBAC plugs its grants in here."""
    return ROLES.get(role, ROLES["admin"])


def doc_text(name: str) -> str:
    d = APP_DOCS[name]
    return (f"{name} ({d['g']}, core {d['core']}). {d['what']} "
            f"Actions: {', '.join(d['actions'])}. Settings: {d['settings']}")


# ── retrieval (the redevops-rag pattern: score docs by query overlap, RBAC-filtered) ──
def _tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def retrieve(query: str, role: str, k: int = 4) -> list[tuple[str, float]]:
    q = set(t for t in _tokens(query) if len(t) > 2)
    scored = []
    for name in visible_apps(role):
        text = _tokens(doc_text(name) + " " + name)
        overlap = sum(1 for t in text if t in q)
        name_hit = 3.0 if name.replace("-", " ") in query.lower() or name in query.lower() else 0.0
        s = overlap + name_hit
        if s > 0:
            scored.append((name, round(s, 2)))
    scored.sort(key=lambda x: -x[1])
    return scored[:k] or [(n, 0.0) for n in visible_apps(role)[:k]]


def walkthrough(name: str) -> dict:
    d = APP_DOCS[name]
    approvals = [a for a in d["actions"] if "approval" in a]
    steps = [
        f"**What it is** — {d['what']}",
        f"**Open it** — go to {DEMO} → {d['g']} → {name} → *Open live dashboard* (`{DEMO}/m/{name}`).",
        f"**Settings** — {d['settings']}",
        f"**First action** — try `{d['actions'][0]}` from the dashboard (or POST /agent/run).",
        (f"**Approval-gated** — {', '.join(approvals)} pause for a human sign-off."
         if approvals else "**No irreversible actions** — read-only/safe by default."),
    ]
    return {"app": name, "core": d["core"], "group": d["g"], "steps": steps}


def answer(question: str, role: str, llm: Callable[[str], str | None] | None = None) -> dict:
    """Retrieve the RBAC-scoped app cards for `question` and answer over them.

    `llm` is an optional narration callback (the urllib LLM call lives in app.py); the
    action itself is fully deterministic and works with llm=None — it falls back to a
    retrieved summary of the top app.
    """
    hits = retrieve(question, role, k=4)
    cited = [n for n, _ in hits]
    context = "\n".join(f"- {doc_text(n)}" for n in cited)
    prompt = (f"You are the onboarding guide for the redevops agentic-apps stack. Answer the user's "
              f"question using ONLY the app cards below; be concrete and point them to the right app + "
              f"its dashboard. Cite app names.\n\nAPP CARDS:\n{context}\n\nQUESTION: {question}\n\nANSWER:")
    text = llm(prompt) if llm else None
    if not text:   # deterministic fallback (no LLM configured): summarize the top app
        top = cited[0]
        d = APP_DOCS[top]
        text = (f"For that, use **{top}** ({d['g']}, core {d['core']}). {d['what']} "
                f"Open it at {DEMO}/m/{top}. Related: {', '.join(cited[1:3])}.")
    return {"question": question, "role": role, "answer": text, "cited": cited}

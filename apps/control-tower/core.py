"""agentic-control-tower core — the pure Metabase REST client + agentic analytics actions.

No web framework, no context-runtime/LLM: just httpx against a real Metabase core. This is
the layer the FastAPI app renders from AND the Mission Runtime operator invokes, so the
capability handlers can be tested against a fake Metabase without booting the whole console.

`ask(question, pick=..., blurb=...)` takes optional callbacks — the LLM template-picker and
the narration blurb live in `app.py` (they call out to a model). Keeping them injected leaves
this module dependency-light and deterministic: routing falls back to keyword matching and the
action itself works fully with `pick=None, blurb=None`. Only ever runs the pre-written template
SQL — never model-authored SQL.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

import httpx

# ── config (env; seed.py writes agents/control-tower/.env) ───────────────────
# Idempotent .env load so this module is self-sufficient when imported by the operator
# (i.e. without the FastAPI app having run its own loader first).
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

METABASE_API_URL = os.environ.get("METABASE_API_URL", "http://localhost:3001").rstrip("/")
METABASE_SESSION = os.environ.get("METABASE_SESSION", "")
# Admin creds let the agent self-heal an expired session (Metabase sessions expire ~14 days),
# so the demo never goes dark waiting for a fresh token to be pasted in.
METABASE_ADMIN_EMAIL = os.environ.get("METABASE_ADMIN_EMAIL", "")
METABASE_ADMIN_PASSWORD = os.environ.get("METABASE_ADMIN_PASSWORD", "")
_session = {"token": METABASE_SESSION}
METABASE_DB_NAME = os.environ.get("METABASE_DB_NAME", "")
_FALLBACK_DB_ID = int(os.environ.get("METABASE_DB_ID", "1"))
METABASE_FRONT_URL = os.environ.get("METABASE_FRONT_URL", "http://localhost:3001").rstrip("/")
_DB_ID_CACHE: dict = {"id": None}   # resolved Metabase datasource id (looked up by name), cached

TENANT = "Meridian Wealth Management"
SUBTITLE = "The owner's single pane — ask your business anything, in plain language, on a real Metabase core."


# --- Metabase REST client ----------------------------------------------------
def _headers() -> dict:
    return {"X-Metabase-Session": _session["token"], "Content-Type": "application/json"}


def _login() -> bool:
    """Get a fresh X-Metabase-Session with the admin creds (self-heals an expired token)."""
    if not (METABASE_ADMIN_EMAIL and METABASE_ADMIN_PASSWORD):
        return False
    try:
        r = httpx.post(f"{METABASE_API_URL}/api/session", timeout=8.0,
                       json={"username": METABASE_ADMIN_EMAIL, "password": METABASE_ADMIN_PASSWORD})
        if r.status_code == 200 and r.json().get("id"):
            _session["token"] = r.json()["id"]
            _DB_ID_CACHE["id"] = None    # re-resolve the datasource under the new session
            return True
    except Exception:
        pass
    return False


def _ensure_session() -> None:
    """Ensure a usable session before querying: log in if we have no token or it's rejected."""
    if not _session["token"]:
        _login()
        return
    try:
        r = httpx.get(f"{METABASE_API_URL}/api/user/current", headers=_headers(), timeout=5.0)
        if r.status_code == 401:
            _login()
    except Exception:
        pass


def metabase_connected() -> bool:
    """True iff Metabase's health endpoint returns 200 with {"status":"ok"}."""
    try:
        r = httpx.get(f"{METABASE_API_URL}/api/health", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False


def resolve_db_id() -> int:
    """Resolve the Metabase datasource id by name (METABASE_DB_NAME), cached; fall back to
    METABASE_DB_ID. Robust to the datasource being re-registered with a new numeric id."""
    if _DB_ID_CACHE["id"] is not None:
        return _DB_ID_CACHE["id"]
    if METABASE_DB_NAME and _session["token"]:
        try:
            r = httpx.get(f"{METABASE_API_URL}/api/database", headers=_headers(), timeout=8.0)
            body = r.json()
            dbs = body.get("data", body) if isinstance(body, dict) else body
            for d in (dbs or []):
                if d.get("name") == METABASE_DB_NAME:
                    _DB_ID_CACHE["id"] = d["id"]
                    return d["id"]
        except Exception:
            pass
    _DB_ID_CACHE["id"] = _FALLBACK_DB_ID
    return _FALLBACK_DB_ID


def run_query(sql: str) -> dict:
    """Run native SQL against the Meridian Wealth datasource via POST /api/dataset.

    Returns {"cols": [...], "rows": [[...]], "error": <str|None>} — the REAL query
    result from Metabase. The single place that talks to the core, errors surfaced
    (never crashing the page).
    """
    try:
        payload = {"database": resolve_db_id(), "type": "native", "native": {"query": sql}}
        r = httpx.post(f"{METABASE_API_URL}/api/dataset", headers=_headers(), json=payload, timeout=20.0)
        if r.status_code == 401 and _login():
            payload["database"] = resolve_db_id()   # re-resolve under the fresh session
            r = httpx.post(f"{METABASE_API_URL}/api/dataset", headers=_headers(), json=payload, timeout=20.0)
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "completed":
            return {"cols": [], "rows": [], "error": str(body.get("error") or "query failed")}
        data = body.get("data", {})
        return {
            "cols": [c.get("display_name") or c.get("name") for c in data.get("cols", [])],
            "rows": data.get("rows", []),
            "error": None,
        }
    except Exception as e:  # network / auth hiccup — surface, don't crash
        return {"cols": [], "rows": [], "error": str(e)}


# --- query templates (deterministic; the "ask" router maps NL -> one of these) ---
# Every query is REAL native Postgres SQL against the Meridian Wealth operational DB
# (engagements / clients / invoices), relative to CURRENT_DATE so the demo is always current.
def _q_revenue_by_month() -> str:
    return ("SELECT to_char(completed_date,'YYYY-MM') AS month, count(*) AS jobs, "
            "round(sum(invoiced_amount))::float AS revenue FROM jobs "
            "WHERE status='Completed' AND completed_date >= (CURRENT_DATE - INTERVAL '12 months') "
            "GROUP BY 1 ORDER BY 1")


def _q_revenue_by_service() -> str:
    return ("SELECT service_type AS service_line, count(*) AS jobs, "
            "round(sum(invoiced_amount))::float AS revenue "
            "FROM jobs WHERE status='Completed' GROUP BY 1 ORDER BY revenue DESC")


def _q_margin_by_service() -> str:
    return ("SELECT service_type AS service_line, round(sum(invoiced_amount))::float AS revenue, "
            "round(100*(sum(invoiced_amount)-sum(material_cost)-sum(labor_cost))"
            "/nullif(sum(invoiced_amount),0)) AS margin_pct "
            "FROM jobs WHERE status='Completed' GROUP BY 1 ORDER BY margin_pct DESC")


def _q_lead_source() -> str:
    return ("SELECT lead_source, count(*) FILTER (WHERE status!='Lost') AS won, count(*) AS quotes, "
            "round(100.0*count(*) FILTER (WHERE status!='Lost')/count(*)) AS win_pct, "
            "round(sum(invoiced_amount))::float AS revenue FROM jobs GROUP BY 1 ORDER BY revenue DESC")


def _q_conversion_by_month() -> str:
    return ("SELECT to_char(quote_date,'YYYY-MM') AS month, "
            "round(100.0*count(*) FILTER (WHERE status!='Lost')/count(*)) AS conversion_pct, "
            "count(*) AS quotes FROM jobs "
            "WHERE quote_date >= (CURRENT_DATE - INTERVAL '12 months') GROUP BY 1 ORDER BY 1")


def _q_ar_aging() -> str:
    return ("SELECT CASE WHEN CURRENT_DATE<=due_date THEN '0 current' "
            "WHEN CURRENT_DATE-due_date<=30 THEN '1-30 days' WHEN CURRENT_DATE-due_date<=60 THEN '31-60 days' "
            "WHEN CURRENT_DATE-due_date<=90 THEN '61-90 days' ELSE '90+ days' END AS bucket, "
            "count(*) AS invoices, round(sum(amount))::float AS outstanding "
            "FROM invoices WHERE status='Open' GROUP BY 1 ORDER BY 1")


def _q_revenue_by_region() -> str:
    return ("SELECT region, count(*) AS jobs, round(sum(invoiced_amount))::float AS revenue "
            "FROM jobs WHERE status='Completed' GROUP BY 1 ORDER BY revenue DESC")


def _q_top_customers() -> str:
    return ("SELECT c.name AS customer, count(*) AS jobs, round(sum(j.invoiced_amount))::float AS revenue "
            "FROM jobs j JOIN customers c ON j.customer_id=c.id WHERE j.status='Completed' "
            "GROUP BY 1 ORDER BY revenue DESC LIMIT 10")


def _q_by_crew() -> str:
    return ("SELECT crew, count(*) AS jobs, round(sum(invoiced_amount))::float AS revenue, "
            "round(avg(invoiced_amount))::float AS avg_job FROM jobs WHERE status='Completed' "
            "GROUP BY 1 ORDER BY revenue DESC")


def _q_backlog() -> str:
    return ("SELECT status, count(*) AS jobs, round(sum(quoted_amount))::float AS pipeline_value "
            "FROM jobs WHERE status IN ('Scheduled','In Progress') GROUP BY 1 ORDER BY pipeline_value DESC")


def _q_avg_job_by_month() -> str:
    return ("SELECT to_char(completed_date,'YYYY-MM') AS month, "
            "round(avg(invoiced_amount))::float AS avg_job FROM jobs "
            "WHERE status='Completed' AND completed_date >= (CURRENT_DATE - INTERVAL '12 months') "
            "GROUP BY 1 ORDER BY 1")


def _q_kpi_extras() -> str:
    """One row of headline KPIs the scorecards need beyond the monthly series."""
    return ("SELECT "
            "(SELECT round(100*(sum(invoiced_amount)-sum(material_cost)-sum(labor_cost))"
            "/nullif(sum(invoiced_amount),0)) FROM jobs WHERE status='Completed' "
            "AND completed_date>=CURRENT_DATE-INTERVAL '6 months') AS margin_pct, "
            "(SELECT round(sum(amount))::float FROM invoices WHERE status='Open') AS ar_outstanding, "
            "(SELECT round(100.0*count(*) FILTER (WHERE status!='Lost')/count(*)) FROM jobs "
            "WHERE quote_date>=CURRENT_DATE-INTERVAL '6 months') AS conversion_pct, "
            "(SELECT round(sum(quoted_amount))::float FROM jobs WHERE status IN ('Scheduled','In Progress')) "
            "AS backlog")


# Ordered so the most specific patterns win; first match is used. Real owner questions.
QUESTION_TEMPLATES = [
    {
        "key": "margin_by_service",
        "match": ["margin", "profit", "profitable", "most money", "makes money", "gross margin",
                  "most profitable service", "which service line makes"],
        "title": "Gross margin by service line",
        "sql": _q_margin_by_service,
    },
    {
        "key": "lead_source",
        "match": ["lead source", "leads", "where do", "marketing", "channel", "referral",
                  "seminar", "webinar", "which source", "roi", "advertising"],
        "title": "Revenue & win-rate by referral source",
        "sql": _q_lead_source,
    },
    {
        "key": "conversion",
        "match": ["conversion", "win rate", "win-rate", "close rate", "proposals", "new clients",
                  "proposal to client", "winning", "convert"],
        "title": "Proposal-to-client conversion by month",
        "sql": _q_conversion_by_month,
    },
    {
        "key": "ar_aging",
        "match": ["receivable", "owe", "owed", "unpaid", "outstanding", "aging", "collect",
                  "overdue", "invoices open", "who owes", "fees outstanding"],
        "title": "Advisory-fee receivable aging",
        "sql": _q_ar_aging,
    },
    {
        "key": "revenue_by_service",
        "match": ["service line", "service type", "by service", "revenue by service",
                  "portfolio management", "planning vs", "breakdown by service"],
        "title": "Revenue by service line",
        "sql": _q_revenue_by_service,
    },
    {
        "key": "revenue_by_region",
        "match": ["region", "area", "territory", "by location", "which region", "geography", "by office"],
        "title": "Revenue by region",
        "sql": _q_revenue_by_region,
    },
    {
        "key": "top_customers",
        "match": ["top client", "best client", "biggest client", "top customer", "best customer",
                  "who pays the most", "which clients", "which households", "who spends"],
        "title": "Top clients by revenue",
        "sql": _q_top_customers,
    },
    {
        "key": "by_crew",
        "match": ["advisor", "team", "by advisor", "advisor productivity", "which advisor",
                  "advisory team"],
        "title": "Revenue by advisor",
        "sql": _q_by_crew,
    },
    {
        "key": "backlog",
        "match": ["backlog", "pipeline", "scheduled", "upcoming", "booked", "in progress", "future work"],
        "title": "Pipeline (scheduled / in-progress)",
        "sql": _q_backlog,
    },
    {
        "key": "avg_job",
        "match": ["average engagement", "avg engagement", "engagement value", "fee per client",
                  "average fee", "average revenue per client"],
        "title": "Average engagement value by month",
        "sql": _q_avg_job_by_month,
    },
    {
        "key": "revenue_by_month",
        "match": ["revenue by month", "revenue trend", "monthly revenue", "sales by month",
                  "revenue over time", "engagements by month", "how much revenue", "how are we doing"],
        "title": "Revenue by month",
        "sql": _q_revenue_by_month,
    },
]


def route_question(question: str, pick: Callable[[str], str | None] | None = None) -> dict:
    """Map a natural-language question to a query template (deterministic keyword routing).

    Optional `pick(question) -> key|None` is an injected LLM assist (the model lives in
    app.py): if it returns a known template key we use it — but we ALWAYS fall back to
    keyword routing, and we only ever run the pre-written SQL, never model-authored SQL.
    Defaults to revenue_by_month.
    """
    q = (question or "").lower().strip()

    if pick is not None:
        try:
            llm_key = pick(q)
        except Exception:  # noqa: BLE001 — the picker is best-effort; never break routing
            llm_key = None
        if llm_key:
            for t in QUESTION_TEMPLATES:
                if t["key"] == llm_key:
                    return t

    for t in QUESTION_TEMPLATES:
        if any(m in q for m in t["match"]):
            return t
    return QUESTION_TEMPLATES[0]  # revenue_by_month


# --- live data + KPIs (cached briefly) ---------------------------------------
_CACHE: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 15.0  # seconds — keep the dashboard snappy without re-querying every hit


def _money(v) -> str:
    try:
        return "${:,.0f}".format(float(v or 0))
    except Exception:
        return "$0"


def _pct_delta(curr: float, prev: float) -> str:
    if not prev:
        return ""
    d = round(100 * (curr - prev) / prev)
    arrow = "↑" if d > 0 else ("↓" if d < 0 else "→")
    sign = "+" if d > 0 else ""
    return f"{arrow} {sign}{d}% vs prev month"


def fetch_activity(force: bool = False) -> dict:
    """Run the REAL Metabase queries and compute the control-tower KPIs/series."""
    now = time.time()
    if not force and _CACHE["data"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["data"]

    connected = metabase_connected()
    if connected:
        _ensure_session()                      # self-heal an expired/empty token before querying
    ok = connected and bool(_session["token"])
    empty = {"cols": [], "rows": [], "error": "not connected"}

    rev = run_query(_q_revenue_by_month()) if ok else empty
    svc = run_query(_q_revenue_by_service()) if ok else empty
    extra = run_query(_q_kpi_extras()) if ok else empty
    error = rev.get("error") or svc.get("error") or extra.get("error")

    # revenue-by-month is ascending: [ym 'YYYY-MM', jobs, revenue]
    series = [{"ym": r[0], "label": r[0][5:], "jobs": int(r[1]), "revenue": float(r[2])}
              for r in rev["rows"]]
    revenues = [s["revenue"] for s in series]
    max_rev = max(revenues) if revenues else 1.0
    bars = [{"label": s["label"], "pct": int(round(100 * s["revenue"] / max_rev)) if max_rev else 0,
             "value": _money(s["revenue"])} for s in series]

    curr = series[-1] if series else {"revenue": 0, "jobs": 0}
    prev = series[-2] if len(series) >= 2 else {"revenue": 0, "jobs": 0}
    last6 = series[-6:]
    rev6 = sum(s["revenue"] for s in last6)
    jobs6 = sum(s["jobs"] for s in last6)
    avg_job = (rev6 / jobs6) if jobs6 else 0

    ex = (extra["rows"][0] if extra.get("rows") else [None, None, None, None])
    margin_pct, ar_out, conv_pct, backlog = (list(ex) + [None] * 4)[:4]

    kpis = [
        {"label": "Advisory-fee revenue (this month)", "value": _money(curr["revenue"]),
         "note": "month to date", "spark": revenues},
        {"label": "Reviews completed (MTD)", "value": f"{curr['jobs']:,}",
         "note": "completed this month", "spark": [s["jobs"] for s in series]},
        {"label": "Advisory-fee revenue (6 mo)", "value": _money(rev6),
         "note": f"{jobs6:,} reviews completed", "spark": [s["revenue"] for s in last6]},
        {"label": "Avg engagement value", "value": _money(avg_job),
         "note": "completed, 6 mo", "spark": [s["revenue"] / max(s["jobs"], 1) for s in series]},
        {"label": "Gross margin", "value": (f"{int(margin_pct)}%" if margin_pct is not None else "—"),
         "note": "after servicing + platform cost, 6 mo", "spark": revenues},
        {"label": "Outstanding fees", "value": _money(ar_out or 0),
         "note": (f"{int(conv_pct)}% proposal win-rate" if conv_pct is not None else "open fee invoices"),
         "spark": revenues},
    ]

    svc_total = sum(float(r[2]) for r in svc["rows"]) if svc["rows"] else 0.0
    breakdown = {
        "title": "Revenue by service line (live)",
        "head": ["Service line", "Engagements", "Revenue", "Share"],
        "rows": [[r[0], f"{int(r[1]):,}", _money(r[2]),
                  f"{round(100 * float(r[2]) / svc_total) if svc_total else 0}%"] for r in svc["rows"]],
    }

    data = {
        "tenant": TENANT,
        "core": "metabase",
        "connected": connected,
        "error": error,
        "front_url": METABASE_FRONT_URL,
        "as_of": time.strftime("%Y-%m"),
        "ask": "Which service line makes the most margin, and where are my best leads coming from?",
        "kpis": kpis,
        "bars": {"title": f"Advisory-fee revenue, last {len(bars)} months (live query)", "items": bars},
        "table": breakdown,
        "series": series,
        "counts": {"months": len(series), "service_lines": len(svc["rows"]),
                   "backlog": _money(backlog or 0)},
    }
    _CACHE.update(ts=now, data=data)
    return data


# --- agentic actions ----------------------------------------------------------
# How each answer visualizes: (chart, x-column, y-column, y-format). Multi-metric answers
# render as a table; single-metric ones as a bar (categorical) or line (monthly).
VIZ = {
    "revenue_by_month":   ("line", "month", "revenue", "money"),
    "avg_job":            ("line", "month", "avg_job", "money"),
    "conversion":         ("line", "month", "conversion_pct", "pct"),
    "revenue_by_service": ("bar", "service_line", "revenue", "money"),
    "margin_by_service":  ("bar", "service_line", "margin_pct", "pct"),
    "revenue_by_region":  ("bar", "region", "revenue", "money"),
    "top_customers":      ("bar", "customer", "revenue", "money"),
    "by_crew":            ("bar", "crew", "revenue", "money"),
    "backlog":            ("bar", "status", "pipeline_value", "money"),
    "ar_aging":           ("bar", "bucket", "outstanding", "money"),
    "lead_source":        ("table", "lead_source", "revenue", "money"),
}


def _fmt_val(v, yfmt: str) -> str:
    try:
        v = float(v)
    except Exception:
        return str(v)
    if yfmt == "money":
        return _money(v)
    if yfmt == "pct":
        return f"{v:.0f}%"
    return f"{v:,.0f}"


def _col_index(cols: list, name: str | None, default: int) -> int:
    if name:
        def norm(s):
            return str(s).lower().replace(" ", "").replace("_", "")
        target = norm(name)
        for i, c in enumerate(cols):
            if norm(c) == target:
                return i
    return default


def ask(question: str, pick: Callable[[str], str | None] | None = None,
        blurb: Callable[[str], str | None] | None = None) -> dict:
    """NL question -> SQL template -> REAL /api/dataset query -> answer + rows + a viz spec.

    Read-only analytics: nothing here moves money or mutates the core, so there is NO
    approval gate. Only ever runs the pre-written template SQL — never model-authored SQL.
    `pick` (LLM template chooser) and `blurb` (LLM one-liner) are optional injected callbacks;
    the action is fully deterministic with both set to None.
    """
    tpl = route_question(question, pick=pick)
    sql = tpl["sql"]()
    result = run_query(sql)
    rows, cols = result["rows"], result["cols"]
    chart, xcol, ycol, yfmt = VIZ.get(tpl["key"], ("table", None, None, "num"))
    x_i = _col_index(cols, xcol, 0)
    y_i = _col_index(cols, ycol, len(cols) - 1 if cols else 0)

    answer = ""
    if rows and not result["error"]:
        xlabel = str(cols[x_i]).replace("_", " ") if cols else "row"
        if chart == "line":
            last = rows[-1]
            answer = f"Most recent {xlabel} ({last[x_i]}): {_fmt_val(last[y_i], yfmt)}."
        else:
            first = rows[0]
            answer = f"Top {xlabel}: {first[x_i]} — {_fmt_val(first[y_i], yfmt)}."
    elif result["error"]:
        answer = f"Query error: {result['error']}"

    reasoning = blurb(
        f"You are a BI analyst for a wealth-management firm (RIA). In ONE sentence, summarize this "
        f"'{tpl['title']}' result for the owner: {rows[:6]}. Be concrete. Final answer only."
    ) if (blurb and rows) else None

    out = {
        "status": "done", "action": "ask", "question": question,
        "matched_report": tpl["key"], "title": tpl["title"], "sql": sql,
        "answer": answer, "columns": cols, "rows": rows,
        "chart": chart, "x_i": x_i, "y_i": y_i, "yfmt": yfmt,
        "approval": "not required — read-only analytics (no destructive action)",
    }
    if reasoning:
        out["reasoning"] = reasoning
    return out


def refresh() -> dict:
    """Re-run the dashboard queries (bust the cache) and report fresh KPIs."""
    data = fetch_activity(force=True)
    return {
        "status": "done",
        "action": "refresh",
        "kpis": data["kpis"],
        "months": data["counts"]["months"],
        "service_lines": data["counts"]["service_lines"],
        "summary": f"Re-ran the dashboard queries against Metabase — {data['counts']['months']} months "
                   f"of revenue and {data['counts']['service_lines']} service lines refreshed.",
        "approval": "not required — read-only analytics (no destructive action)",
    }

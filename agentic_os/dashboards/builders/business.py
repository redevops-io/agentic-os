"""Business dashboards over the connected applications: revenue (Twenty, Control Tower), finance (ERPNext, Lago),
market and conversion (Umami, Twenty), support (Chatwoot), procurement (ERPNext) and marketing (Postiz, Listmonk).

Money is summed per currency and never across currencies: the headline is the currency most records use, and the note
names what is left out.
"""
from __future__ import annotations

from statistics import median
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..build import Build, col, panel, pct, row, tone_of
from ..contracts import Record
from ..store import DAY, day_of


def _days_past(date: Any, now: float) -> int:
    """Whole calendar days (UTC) since a DATE such as a due or expected date, which sources give as its first instant.
    0 on the day itself: something due today is not overdue until tomorrow, as the applications themselves count it."""
    return int(now // DAY) - int(float(date) // DAY)


def _money(recs: Sequence[Record], field: str = "amount") -> Tuple[Optional[float], str, str]:
    """(sum in the main currency, that currency, a note naming what was left out)."""
    by: Dict[str, List[float]] = {}
    missing = 0
    for r in recs:
        v = r.get(field, None)
        if v is None:
            missing += 1
            continue
        by.setdefault(str(r.get("currency") or ""), []).append(float(v))
    if not by:
        return None, "", f"{missing} record(s) carry no amount" if missing else ""
    cur = max(by, key=lambda k: len(by[k]))
    notes = [f"{len(v)} in {k or 'an unstated currency'} not included" for k, v in by.items() if k != cur]
    if missing:
        notes.append(f"{missing} without an amount")
    return round(sum(by[cur]), 2), cur or "", "; ".join(notes)


def _months(b: Build) -> List[str]:
    out, t = [], b.since
    while t <= b.until + 31 * DAY and len(out) < 37:
        m = day_of(t)[:7]
        if m not in out and m <= day_of(b.until)[:7]:
            out.append(m)
        t += 15 * DAY
    return out


def _unavailable(b: Build, sids: Sequence[str], what: str, mid: str, label: str) -> bool:
    if b.any_usable(*sids):
        return False
    b.down(f"{what} cannot be read right now.", mid, label, tuple(sids), any_of=True)
    return True


def _advanced(b: Build) -> None:
    opts = b.cache.ctx.config.sources.get("metabase") or {}
    pub = str(opts.get("public_url") or "").rstrip("/")
    for d in opts.get("dashboards") or []:
        if isinstance(d, dict) and isinstance(d.get("id"), int) and pub and (not d.get("for") or b.d.id in d.get("for")):
            b.d.advanced.append({"label": str(d.get("title") or "Open in Control Tower"), "url": f"{pub}/dashboard/{d['id']}", "app": "metabase"})


def _card_series(b: Build, series: str) -> List[Record]:
    return [r for r in b.records("metabase", "card_row", timed=False) if r.status == series] if b.cache.enabled("metabase") else []


# ── revenue, CRM and sales ──────────────────────────────────────────────────────────────────────────────────────────
def revenue(b: Build) -> None:
    d, S = b.d, ("twenty",)
    _advanced(b)
    if _unavailable(b, ("twenty", "metabase"), "The CRM", "pipeline", "Open pipeline"):
        return
    deals = b.records("twenty", "deal", timed=False)
    open_ = [x for x in deals if not x.get("won") and not x.get("lost")]
    won = [x for x in deals if x.get("won") and b.since <= (x.get("updated_at") or x.at or b.now) <= b.until]
    lost = [x for x in deals if x.get("lost") and b.since <= (x.get("updated_at") or x.at or b.now) <= b.until]
    stalled = [x for x in open_ if x.get("updated_at") and b.now - float(x.get("updated_at")) > 14 * DAY]
    cols = ("company", "amount", "currency")
    for key, title, recs in (("deals:open", "Open deals", open_), ("deals:won", "Deals won in range", won), ("deals:lost", "Deals lost in range", lost),
                             ("deals:stalled", "Open deals with no change for 14 days", stalled)):
        b.drill(key, title, recs, columns=cols)
    pipe, cur, note = _money(open_)
    won_v, wcur, wnote = _money(won)
    per = b.group(open_, lambda r: r.project)
    m_pipe = b.track(b.metric("pipeline", "Open pipeline", needs=S, value=pipe, unit=cur, drill="deals:open", good="up", note=note or f"{len(open_)} open deal(s)",
                              unknown="" if pipe is not None or not open_ else "no_data"),
                     {p: (_money(v)[0], None) for p, v in per.items()})
    d.panels.append(panel("summary", "Sales", "stats", sources=S, span=2, metrics=[
        m_pipe, b.metric("open", "Open deals", needs=S, value=len(open_), drill="deals:open"),
        b.metric("won", "Won in range", needs=S, value=won_v if won else 0, unit=wcur or cur, drill="deals:won", good="up", note=wnote or f"{len(won)} deal(s)"),
        b.metric("win_rate", "Win rate", needs=S, ratio=True, num=len(won), den=len(won) + len(lost), unit="%", good="up", drill="deals:lost" if lost else "deals:won",
                 tone=tone_of(pct(len(won), len(won) + len(lost)), good=40, warn=20), note=f"{len(won)} won, {len(lost)} lost, by last change in the range"),
        b.metric("stalled", "Stalled", needs=S, value=len(stalled), drill="deals:stalled", tone="warn" if stalled else "ok", good="down", note="open, unchanged for 14 days"),
        b.metric("accounts", "Accounts", needs=S, value=len(b.records("twenty", "account", timed=False)))]))
    stages = b.group(open_, lambda r: r.status or "—")
    d.panels.append(panel("by_stage", "Open pipeline by stage", "bars", sources=S, unit=cur, empty="" if open_ else "No deal is open.",
                          items=[{"label": k.replace("_", " ").title(), "value": _money(v)[0] or 0, "tone": "run", "note": f"{len(v)} deal(s)",
                                  "drill": b.drill(f"stage:{k}", f"Open deals: {k.replace('_', ' ').lower()}", v, columns=cols)}
                                 for k, v in sorted(stages.items(), key=lambda kv: -(_money(kv[1])[0] or 0))]))
    rev = _card_series(b, "revenue_by_month")
    if rev:
        xs = [str(r.title) for r in rev]
        key = next((c for c in (rev[0].get("columns") or []) if c.lower() in ("revenue", "amount", "total")), (rev[0].get("columns") or ["", ""])[-1])
        d.panels.append(panel("revenue_month", "Revenue by month", "timeseries", sources=("metabase",), x=xs, unit="",
                              series=[{"id": "revenue", "label": "Revenue", "tone": "ok", "values": [r.get("row", {}).get(key) for r in rev]}],
                              note="Computed by Control Tower's saved question; open it there for the query."))
    else:
        months = _months(b)
        by = {m: [] for m in months}
        for x in [y for y in deals if y.get("won")]:
            m = day_of(x.get("updated_at") or x.at or 0)[:7]
            if m in by:
                by[m].append(x)
        d.panels.append(panel("won_month", "Won by month", "timeseries", sources=S, x=months, unit=cur,
                              series=[{"id": "won", "label": "Won", "tone": "ok", "values": [_money(by[m])[0] or 0 for m in months]}],
                              empty="" if any(by.values()) else "No deal was won in this range.", note="By the date the deal last changed."))
    rows = [row({"deal": x.title, "company": x.get("company"), "stage": x.status.replace("_", " ").title(), "amount": x.get("amount"), "currency": x.get("currency"),
                 "idle_days": int((b.now - float(x.get("updated_at"))) / DAY) if x.get("updated_at") else None},
                tone="warn" if x in stalled else "", ref=x.ref, url=x.url) for x in sorted(open_, key=lambda r: -(r.get("amount") or 0))[:50]]
    d.panels.append(panel("deals", "Largest open deals", "table", sources=S, span=2, rows=rows, empty="" if rows else "No deal is open.",
                          columns=[col("deal", "Deal"), col("company", "Company"), col("stage", "Stage"), col("amount", "Amount", "right"), col("currency", "Cur."),
                                   col("idle_days", "Days idle", "right")]))
    if b.usable("twenty"):
        d.summary = f"{len(open_)} open deal(s) worth {pipe if pipe is not None else 'an unknown amount'} {cur}; {len(stalled)} stalled.".replace("  ", " ")
    else:
        d.summary = (f"Revenue by month from Control Tower ({len(rev)} month(s)). " if rev else "") + "The CRM is not connected, so pipeline and deals are not known."


# ── financial health ────────────────────────────────────────────────────────────────────────────────────────────────
def finance(b: Build) -> None:
    d = b.d
    _advanced(b)
    if _unavailable(b, ("erpnext", "lago"), "The accounting and billing systems", "receivables", "Receivables"):
        return
    inv = b.records("erpnext", "invoice", timed=False)
    bills = b.records("erpnext", "bill", timed=False)
    ar = [x for x in inv if (x.get("outstanding") or 0) > 0]
    overdue = [x for x in ar if x.get("due") and _days_past(x.get("due"), b.now) > 0]
    ap = [x for x in bills if (x.get("outstanding") or 0) > 0]
    invoiced = [x for x in inv if x.at and b.since <= x.at <= b.until and x.status not in ("Cancelled", "Draft")]
    cols = ("customer", "outstanding", "currency", "amount")
    b.drill("ar", "Unpaid sales invoices", ar, columns=cols)
    b.drill("ar:overdue", "Overdue sales invoices", overdue, columns=cols)
    b.drill("ap", "Unpaid supplier bills", ap, columns=("supplier", "outstanding", "currency"))
    b.drill("invoiced", "Invoiced in range", invoiced, columns=cols)
    ar_v, cur, n1 = _money(ar, "outstanding")
    od_v, _, n2 = _money(overdue, "outstanding")
    ap_v, acur, n3 = _money(ap, "outstanding")
    iv_v, _, n4 = _money(invoiced)
    E = ("erpnext",)
    metrics = [
        b.track(b.metric("receivables", "Receivables outstanding", needs=E, value=ar_v if ar else 0, unit=cur, drill="ar", good="down", note=n1 or f"{len(ar)} unpaid invoice(s)"),
                {x: (_money(v, "outstanding")[0], None) for x, v in b.group(ar, lambda r: r.project).items()} or {"": (0.0, None)}),
        b.metric("overdue", "Overdue", needs=E, value=od_v if overdue else 0, unit=cur, drill="ar:overdue", tone="bad" if overdue else "ok", good="down",
                 note=n2 or f"{len(overdue)} invoice(s) past due"),
        b.metric("overdue_share", "Overdue share of receivables", needs=E, ratio=True, num=od_v if overdue else 0, den=ar_v, unit="%", good="down",
                 tone=tone_of(pct(od_v or 0, ar_v), good=10, warn=30, higher_is_better=False)),
        b.metric("payables", "Payables outstanding", needs=E, value=ap_v if ap else 0, unit=acur or cur, drill="ap", note=n3 or f"{len(ap)} unpaid bill(s)"),
        b.metric("invoiced", "Invoiced in range", needs=E, value=iv_v if invoiced else 0, unit=cur, drill="invoiced", good="up", note=n4 or f"{len(invoiced)} invoice(s)"),
    ]
    if b.cache.enabled("lago"):
        subs = b.records("lago", "subscription", timed=False)
        linv = b.records("lago", "invoice")
        failed = [x for x in linv if x.status == "failed" or x.get("overdue")]
        metrics += [b.metric("subscriptions", "Active subscriptions", needs=("lago",), value=len(subs), drill=b.drill("subs", "Active subscriptions", subs, columns=("plan", "customer"))),
                    b.metric("billing_failed", "Failed or overdue billing", needs=("lago",), value=len(failed), tone="bad" if failed else "ok", good="down",
                             drill=b.drill("billing:failed", "Billing invoices that failed or are overdue", failed, columns=("customer", "amount", "currency")))]
    d.panels.append(panel("summary", "Cash position", "stats", sources=("erpnext", "lago") if b.cache.enabled("lago") else E, span=2, metrics=metrics))
    buckets = (("Not due", -1e9, 0), ("1–30 days", 0, 30), ("31–60 days", 30, 60), ("61–90 days", 60, 90), ("Over 90 days", 90, 1e9))
    items = []
    for label, lo, hi in buckets:
        recs = [x for x in ar if lo < (_days_past(x.get("due"), b.now) if x.get("due") else -1) <= hi]
        items.append({"label": label, "value": _money(recs, "outstanding")[0] or 0, "note": f"{len(recs)} invoice(s)", "tone": "ok" if label == "Not due" else "warn" if hi <= 60 else "bad",
                      "drill": b.drill(f"aging:{label}", f"Receivables: {label.lower()}", recs, columns=cols) if recs else ""})
    d.panels.append(panel("aging", "Receivables aging", "bars", sources=E, unit=cur, items=items, empty="" if ar else "Nothing is outstanding."))
    months = _months(b)
    by = {m: [] for m in months}
    for x in invoiced:
        if day_of(x.at)[:7] in by:
            by[day_of(x.at)[:7]].append(x)
    d.panels.append(panel("invoiced_month", "Invoiced by month", "timeseries", sources=E, x=months, unit=cur,
                          series=[{"id": "invoiced", "label": "Invoiced", "tone": "ok", "values": [_money(by[m])[0] or 0 for m in months]}],
                          empty="" if invoiced else "No invoice was posted in this range."))
    rows = [row({"invoice": x.id, "customer": x.get("customer"), "outstanding": x.get("outstanding"), "currency": x.get("currency"),
                 "days_overdue": _days_past(x.get("due"), b.now), "status": x.status}, tone="bad", ref=x.ref, url=x.url)
            for x in sorted(overdue, key=lambda r: float(r.get("due")))[:50]]
    d.panels.append(panel("overdue", "Overdue invoices", "table", sources=E, span=2, rows=rows, empty="" if rows else "No invoice is overdue.",
                          columns=[col("invoice", "Invoice"), col("customer", "Customer"), col("outstanding", "Outstanding", "right"), col("currency", "Cur."),
                                   col("days_overdue", "Days overdue", "right"), col("status", "Status")]))
    d.summary = f"{ar_v or 0} {cur} outstanding, {od_v or 0} overdue across {len(overdue)} invoice(s)."


# ── market intelligence, acquisition and conversion ─────────────────────────────────────────────────────────────────
def market(b: Build) -> None:
    d, U = b.d, ("umami",)
    if _unavailable(b, ("umami", "twenty"), "Web analytics and the CRM", "visitors", "Visitors"):
        return
    sites = b.records("umami", "site_traffic", timed=False)
    pages = b.records("umami", "page", timed=False)

    def tot(k: str) -> Optional[float]:
        vals = [float(s.get(k)) for s in sites if s.get(k) is not None]
        return sum(vals) if vals else None
    visitors, views, visits, bounces = tot("visitors"), tot("pageviews"), tot("visits"), tot("bounces")
    b.drill("sites", "Tracked sites", sites, columns=("domain", "visitors", "pageviews"))
    window = f"last {int(sites[0].get('days'))} days, as the analytics server reports it" if sites else ""
    d.panels.append(panel("summary", "Acquisition", "stats", sources=U, span=2, note="Umami reports a fixed 30-day window; the date filter does not change these figures.", metrics=[
        b.track(b.metric("visitors", "Visitors", needs=U, value=visitors, drill="sites", good="up", note=window, unknown="" if visitors is not None else "no_data"),
                {"": (visitors, None)}),
        b.metric("pageviews", "Page views", needs=U, value=views, drill="sites", good="up", unknown="" if views is not None else "no_data"),
        b.metric("bounce", "Bounce rate", needs=U, ratio=True, num=bounces, den=visits, unit="%", good="down",
                 tone=tone_of(pct(bounces, visits), good=45, warn=70, higher_is_better=False)),
        b.metric("sites", "Tracked sites", needs=U, value=len(sites), drill="sites")]))
    d.panels.append(panel("sites", "Traffic by site", "bars", sources=U, unit="visitors", empty="" if sites else "No site is tracked.",
                          items=[{"label": s.title, "value": s.get("visitors") or 0, "tone": "run", "note": f"{int(s.get('pageviews') or 0)} views"}
                                 for s in sorted(sites, key=lambda s: -(s.get("visitors") or 0))[:12]]))
    d.panels.append(panel("pages", "Top pages", "table", sources=U, rows=[row({"page": p.title, "site": p.get("site"), "views": p.get("views")}, ref=p.ref)
                                                                         for p in sorted(pages, key=lambda p: -(p.get("views") or 0))[:15]],
                          columns=[col("page", "Page"), col("site", "Site"), col("views", "Views", "right")], empty="" if pages else "No page view is recorded."))
    if b.cache.enabled("twenty"):
        accounts = [a for a in b.records("twenty", "account") if a.at]
        deals = [x for x in b.records("twenty", "deal") if x.at]
        won = [x for x in deals if x.get("won")]
        b.drill("funnel:accounts", "Accounts created in range", accounts)
        b.drill("funnel:deals", "Deals created in range", deals, columns=("company", "amount"))
        b.drill("funnel:won", "Deals created in range and won", won, columns=("company", "amount"))
        d.panels.append(panel("funnel", "Acquisition to revenue", "funnel", sources=("umami", "twenty"), span=2, items=[
            {"label": "Visitors", "value": visitors, "tone": "run", "drill": "sites", "note": "30-day window"},
            {"label": "New accounts", "value": len(accounts) if b.usable("twenty") else None, "tone": "run", "drill": "funnel:accounts"},
            {"label": "Deals created", "value": len(deals) if b.usable("twenty") else None, "tone": "run", "drill": "funnel:deals"},
            {"label": "Won", "value": len(won) if b.usable("twenty") else None, "tone": "ok", "drill": "funnel:won"}],
            note="The stages come from two systems and are not joined visitor by visitor: read it as volumes side by side, not as a tracked conversion path."))
    d.summary = f"{int(visitors or 0)} visitor(s) and {int(views or 0)} page view(s) across {len(sites)} site(s)."


# ── customer support ────────────────────────────────────────────────────────────────────────────────────────────────
def support(b: Build) -> None:
    d, S = b.d, ("chatwoot",)
    if _unavailable(b, S, "The support desk", "open", "Open conversations"):
        return
    conv = b.records("chatwoot", "conversation", timed=False)
    open_ = [c for c in conv if c.status == "open"]
    pending = [c for c in conv if c.status == "pending"]
    unassigned = [c for c in open_ if not c.get("assigned")]
    created = [c for c in conv if c.at and b.since <= c.at <= b.until]
    resolved = [c for c in created if c.status == "resolved"]
    replied = [float(c.get("first_reply_s")) for c in created if c.get("first_reply_s") is not None]
    waiting = [c for c in open_ if c.get("waiting_since") and b.now - float(c.get("waiting_since")) > DAY]
    cols = ("priority", "channel", "unread")
    for key, title, recs in (("open", "Open conversations", open_), ("pending", "Pending conversations", pending), ("unassigned", "Open and unassigned", unassigned),
                             ("created", "Conversations opened in range", created), ("waiting", "Customer waiting more than a day", waiting)):
        b.drill(key, title, recs, columns=cols)
    d.panels.append(panel("summary", "Support", "stats", sources=S, span=2, metrics=[
        b.track(b.metric("open", "Open conversations", needs=S, value=len(open_), drill="open", good="down", tone="warn" if len(open_) > 20 else "ok"),
                {p: (len(v), None) for p, v in b.group(open_, lambda r: r.project).items()} or {"": (0, None)}),
        b.metric("unassigned", "Unassigned", needs=S, value=len(unassigned), drill="unassigned", tone="warn" if unassigned else "ok", good="down"),
        b.metric("waiting", "Waiting over a day", needs=S, value=len(waiting), drill="waiting", tone="bad" if waiting else "ok", good="down"),
        b.metric("first_reply", "Median first reply", needs=S, value=round(median(replied) / 60.0, 1) if replied else None, unit="min", good="down",
                 unknown="" if replied else "no_data", note=f"over {len(replied)} conversation(s) opened in the range that got a reply"),
        b.metric("resolved", "Resolved, of opened in range", needs=S, ratio=True, num=len(resolved), den=len(created), unit="%", good="up", drill="created",
                 tone=tone_of(pct(len(resolved), len(created)), good=80, warn=50)),
        b.metric("pending", "Pending", needs=S, value=len(pending), drill="pending")]))
    pr = b.group(open_, lambda c: str(c.get("priority") or "none"))
    order = ("urgent", "high", "medium", "low", "none")
    d.panels.append(panel("priority", "Open by priority", "bars", sources=S, empty="" if open_ else "No conversation is open.",
                          items=[{"label": k.title(), "value": len(pr[k]), "tone": "bad" if k == "urgent" else "warn" if k == "high" else "mut",
                                  "drill": b.drill(f"prio:{k}", f"Open conversations: {k}", pr[k], columns=cols)} for k in order if k in pr]))
    days = b.days()
    by = {x: 0 for x in days}
    for c in created:
        if day_of(c.at) in by:
            by[day_of(c.at)] += 1
    d.panels.append(panel("volume", "Conversations opened per day", "timeseries", sources=S, x=days, unit="conversations",
                          series=[{"id": "opened", "label": "Opened", "tone": "run", "values": [by[x] for x in days]}],
                          empty="" if created else "No conversation was opened in this range."))
    rows = [row({"customer": c.title, "priority": str(c.get("priority")).title(), "channel": str(c.get("channel")).replace("Channel::", ""),
                 "waiting_h": round((b.now - float(c.get("waiting_since"))) / 3600.0, 1) if c.get("waiting_since") else None,
                 "assigned": "Yes" if c.get("assigned") else "No"}, tone="bad" if c in waiting else "warn" if not c.get("assigned") else "", ref=c.ref, url=c.url)
            for c in sorted(open_, key=lambda c: float(c.get("waiting_since") or b.now))[:50]]
    d.panels.append(panel("backlog", "Oldest open conversations", "table", sources=S, span=2, rows=rows, empty="" if rows else "No conversation is open.",
                          columns=[col("customer", "Customer"), col("priority", "Priority"), col("channel", "Channel"), col("waiting_h", "Waiting (h)", "right"), col("assigned", "Assigned")]))
    d.summary = f"{len(open_)} open, {len(unassigned)} unassigned, {len(waiting)} waiting over a day."


# ── supplier and procurement intelligence ───────────────────────────────────────────────────────────────────────────
def procurement(b: Build) -> None:
    d, S = b.d, ("erpnext",)
    if _unavailable(b, S, "The purchasing system", "open_pos", "Open purchase orders"):
        return
    pos = b.records("erpnext", "purchase_order", timed=False)
    sup = b.records("erpnext", "supplier", timed=False)
    bills = b.records("erpnext", "bill", timed=False)
    open_ = [p for p in pos if p.status not in ("Completed", "Closed", "Cancelled", "Draft")]
    late = [p for p in open_ if p.get("expected") and _days_past(p.get("expected"), b.now) > 0 and (p.get("received_pct") or 0) < 100]
    placed = [p for p in pos if p.at and b.since <= p.at <= b.until and p.status not in ("Cancelled", "Draft")]
    hold = [s for s in sup if s.status == "on_hold"]
    due = [x for x in bills if (x.get("outstanding") or 0) > 0 and x.get("due") and float(x.get("due")) < b.now + 7 * DAY]
    cols = ("supplier", "amount", "currency")
    for key, title, recs in (("pos:open", "Open purchase orders", open_), ("pos:late", "Purchase orders past their expected date", late),
                             ("pos:placed", "Purchase orders placed in range", placed), ("sup:hold", "Suppliers on hold", hold),
                             ("bills:due", "Supplier bills due within 7 days or overdue", due)):
        b.drill(key, title, recs, columns=cols)
    spend, cur, note = _money(placed)
    d.panels.append(panel("summary", "Purchasing", "stats", sources=S, span=2, metrics=[
        b.metric("open_pos", "Open purchase orders", needs=S, value=len(open_), drill="pos:open"),
        b.track(b.metric("late", "Late deliveries", needs=S, value=len(late), drill="pos:late", tone="bad" if late else "ok", good="down", note="past the expected date, not fully received"),
                {p: (len(v), None) for p, v in b.group(late, lambda r: r.project).items()} or {"": (0, None)}),
        b.metric("on_time", "On schedule, of open", needs=S, ratio=True, num=len(open_) - len(late), den=len(open_), unit="%", good="up", drill="pos:late" if late else "pos:open",
                 tone=tone_of(pct(len(open_) - len(late), len(open_)), good=95, warn=80)),
        b.metric("spend", "Ordered in range", needs=S, value=spend if placed else 0, unit=cur, drill="pos:placed", note=note or f"{len(placed)} order(s)"),
        b.metric("suppliers", "Suppliers", needs=S, value=len(sup), note=f"{len(hold)} on hold", drill="sup:hold" if hold else ""),
        b.metric("bills_due", "Bills due in 7 days", needs=S, value=len(due), drill="bills:due", tone="warn" if due else "ok", good="down")]))
    by = b.group(placed, lambda p: str(p.get("supplier") or "—"))
    d.panels.append(panel("by_supplier", "Ordered by supplier", "bars", sources=S, unit=cur, empty="" if placed else "No order was placed in this range.",
                          items=[{"label": k, "value": _money(v)[0] or 0, "tone": "run", "note": f"{len(v)} order(s)",
                                  "drill": b.drill(f"supplier:{k}", f"Orders from {k}", v, columns=cols)} for k, v in sorted(by.items(), key=lambda kv: -(_money(kv[1])[0] or 0))[:12]]))
    late_by = b.group(late, lambda p: str(p.get("supplier") or "—"))
    d.panels.append(panel("late_by_supplier", "Late orders by supplier", "bars", sources=S, empty="" if late else "No open order is late.",
                          items=[{"label": k, "value": len(v), "tone": "bad", "drill": b.drill(f"late:{k}", f"Late orders from {k}", v, columns=cols)}
                                 for k, v in sorted(late_by.items(), key=lambda kv: -len(kv[1]))[:12]]))
    rows = [row({"order": p.id, "supplier": p.get("supplier"), "amount": p.get("amount"), "currency": p.get("currency"), "status": p.status,
                 "days_late": _days_past(p.get("expected"), b.now), "received": p.get("received_pct")}, tone="bad", ref=p.ref, url=p.url)
            for p in sorted(late, key=lambda p: float(p.get("expected")))[:50]]
    d.panels.append(panel("late", "Late purchase orders", "table", sources=S, span=2, rows=rows, empty="" if rows else "No open order is late.",
                          columns=[col("order", "Order"), col("supplier", "Supplier"), col("amount", "Amount", "right"), col("currency", "Cur."), col("status", "Status"),
                                   col("days_late", "Days late", "right"), col("received", "Received %", "right")]))
    d.summary = f"{len(open_)} open order(s), {len(late)} late; {len(hold)} supplier(s) on hold."


# ── marketing and social media ──────────────────────────────────────────────────────────────────────────────────────
def marketing(b: Build) -> None:
    d = b.d
    if _unavailable(b, ("postiz", "listmonk"), "The publishing and email tools", "published", "Posts published"):
        return
    posts = b.records("postiz", "post")
    camps = b.records("listmonk", "campaign")
    lists = b.records("listmonk", "list", timed=False)
    published = [p for p in posts if p.status in ("published", "success") or p.get("release_url")]
    scheduled = [p for p in b.records("postiz", "post", timed=False) if p.at > b.now]
    failed = [p for p in posts if p.status in ("error", "failed")]
    b.drill("posts:published", "Posts published in range", published, columns=("channel", "account"))
    b.drill("posts:scheduled", "Posts scheduled", scheduled, columns=("channel", "account"))
    b.drill("posts:failed", "Posts that failed to publish", failed, columns=("channel", "account"))
    b.drill("campaigns", "Email campaigns in range", camps, columns=("sent", "views", "clicks"))

    def s(k: str) -> float:
        return sum(float(c.get(k) or 0) for c in camps)
    subs = sum(float(x.get("subscribers") or 0) for x in lists)
    P, L = ("postiz",), ("listmonk",)
    d.panels.append(panel("summary", "Publishing and email", "stats", sources=("postiz", "listmonk"), span=2, metrics=[
        b.track(b.metric("published", "Posts published", needs=P, value=len(published), drill="posts:published", good="up"),
                {p: (len(v), None) for p, v in b.group(published, lambda r: r.project).items()} or {"": (0, None)}),
        b.metric("scheduled", "Scheduled", needs=P, value=len(scheduled), drill="posts:scheduled"),
        b.metric("post_failed", "Failed to publish", needs=P, value=len(failed), drill="posts:failed", tone="bad" if failed else "ok", good="down"),
        b.metric("emails", "Emails sent", needs=L, value=s("sent"), drill="campaigns", good="up", note=f"{len(camps)} campaign(s)"),
        b.metric("open_rate", "Email open rate", needs=L, ratio=True, num=s("views"), den=s("sent"), unit="%", good="up", drill="campaigns",
                 tone=tone_of(pct(s("views"), s("sent")), good=25, warn=12), note="views per email sent, as Listmonk counts them"),
        b.metric("click_rate", "Email click rate", needs=L, ratio=True, num=s("clicks"), den=s("sent"), unit="%", good="up", drill="campaigns",
                 tone=tone_of(pct(s("clicks"), s("sent")), good=3, warn=1)),
        b.metric("subscribers", "Subscribers", needs=L, value=subs, drill=b.drill("lists", "Mailing lists", lists, columns=("subscribers",)))]))
    ch = b.group(published, lambda p: str(p.get("channel") or "—"))
    d.panels.append(panel("by_channel", "Published by channel", "bars", sources=P, empty="" if published else "Nothing was published in this range.",
                          items=[{"label": k.title(), "value": len(v), "tone": "run", "drill": b.drill(f"channel:{k}", f"Published on {k}", v, columns=("account",))}
                                 for k, v in sorted(ch.items(), key=lambda kv: -len(kv[1]))]))
    days = b.days()
    by = {x: 0 for x in days}
    for p in published:
        if day_of(p.at) in by:
            by[day_of(p.at)] += 1
    d.panels.append(panel("cadence", "Posts per day", "timeseries", sources=P, x=days, unit="posts", empty="" if published else "Nothing was published in this range.",
                          series=[{"id": "posts", "label": "Published", "tone": "run", "values": [by[x] for x in days]}]))
    d.panels.append(panel("campaigns", "Email campaigns", "table", sources=L, span=2, empty="" if camps else "No campaign ran in this range.",
                          rows=[row({"campaign": c.title, "status": c.status.title(), "sent": c.get("sent"), "open_rate": pct(c.get("views") or 0, c.get("sent")),
                                     "click_rate": pct(c.get("clicks") or 0, c.get("sent")), "bounces": c.get("bounces")}, ref=c.ref, url=c.url) for c in camps[:50]],
                          columns=[col("campaign", "Campaign"), col("status", "Status"), col("sent", "Sent", "right"), col("open_rate", "Open %", "right"),
                                   col("click_rate", "Click %", "right"), col("bounces", "Bounces", "right")]))
    d.summary = f"{len(published)} post(s) published, {len(scheduled)} scheduled; {int(s('sent'))} email(s) sent."


__all__ = ["revenue", "finance", "market", "support", "procurement", "marketing"]

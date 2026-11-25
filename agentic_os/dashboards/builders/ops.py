"""Operational dashboards read from the runtime and the registered products: portfolio, security, missions,
infrastructure, AI usage and knowledge."""
from __future__ import annotations

from statistics import median
from typing import Dict, List

from ..build import Build, col, panel, pct, row, tone_of
from ..contracts import Record
from ..store import DAY, day_of
from .qa import QaModel, SRC as QA_SRC

SEVERITIES = ("critical", "high", "medium", "low", "unknown")
SEV_TONE = {"critical": "bad", "high": "bad", "medium": "warn", "low": "mut", "unknown": "mut"}


def _open_cases(b: Build) -> List[Record]:
    return [r for r in b.records("sentinel", "security_case", timed=False) if r.get("open")]


# ── security, compliance and remediation ────────────────────────────────────────────────────────────────────────────
def security(b: Build) -> None:
    d, S = b.d, ("sentinel",)
    if not b.usable("sentinel"):
        b.down("Edge Sentinel is not answering, so security findings are not known right now.", "open_cases", "Open cases", S)
        return
    cases_all = b.records("sentinel", "security_case", timed=False)
    opened = [r for r in cases_all if r.get("open")]
    in_range = b.records("sentinel", "security_case")
    actions = b.records("sentinel", "security_action", timed=False)
    cols = ("severity", "case_type", "asset")
    b.drill("cases:open", "Open security cases", opened, columns=cols)
    crit = [r for r in opened if r.get("severity") in ("critical", "high")]
    b.drill("cases:critical", "Open critical and high cases", crit, columns=cols)
    waiting = [a for a in actions if a.status == "AWAITING_APPROVAL"]
    b.drill("actions:waiting", "Actions waiting for a person", waiting, columns=("capability", "case_id"))
    executed = [a for a in actions if a.status == "EXECUTED"]
    verified = [a for a in executed if a.get("verification") == "verified"]
    unverified = [a for a in executed if a.get("verification") != "verified"]
    b.drill("actions:verified", "Executed and verified by read-back", verified, columns=("capability", "verification"))
    b.drill("actions:unverified", "Executed, not verified", unverified, columns=("capability", "verification"),
            note="The action ran, and the read-back did not confirm it. Treat it as not done.")
    # A case is closed when the product says so (its status). WHEN it closed is a separate fact that some products do not
    # report: such a case is still counted and listed as closed (placed in the range by its opening time), and only the
    # time-to-close is "not known" for it. Counting it as zero closed cases would hide real remediation.
    timed = [r for r in cases_all if not r.get("open") and r.get("closed_at") and r.at and b.since <= float(r.get("closed_at")) <= b.until]
    untimed = [r for r in cases_all if not r.get("open") and not r.get("closed_at") and (not r.at or b.since <= r.at <= b.until)]
    closed = timed + untimed
    hours = [(float(r.get("closed_at")) - r.at) / 3600.0 for r in timed if float(r.get("closed_at")) >= r.at]
    b.drill("cases:closed", "Cases closed in this range", closed, columns=cols,
            note=(f"{len(untimed)} of {len(closed)} carry no closing time from the product, so they are listed by when they opened "
                  "(or always, when that is not reported either) and are left out of the time-to-close.") if untimed else "")
    per = b.group(opened, lambda r: r.project)
    m_open = b.track(b.metric("open_cases", "Open cases", needs=S, value=len(opened), drill="cases:open", good="down", tone="warn" if opened else "ok"),
                     {p: (len(v), None) for p, v in per.items()} or {"": (0, None)})
    m_crit = b.track(b.metric("critical_open", "Critical and high, open", needs=S, value=len(crit), drill="cases:critical", good="down", tone="bad" if crit else "ok"),
                     {p: (len([r for r in v if r.get("severity") in ("critical", "high")]), None) for p, v in per.items()} or {"": (0, None)})
    d.panels.append(panel("summary", "Findings and remediation", "stats", sources=S, span=2, metrics=[
        m_open, m_crit,
        b.metric("awaiting", "Actions awaiting approval", needs=S, value=len(waiting), drill="actions:waiting", tone="warn" if waiting else "ok", good="down",
                 note="a person decides in Today"),
        b.metric("verified", "Executed actions verified", needs=S, ratio=True, num=len(verified), den=len(executed), unit="%", good="up",
                 drill="actions:unverified" if unverified else "actions:verified", tone=tone_of(pct(len(verified), len(executed)), good=100, warn=90),
                 note=f"{len(verified)} of {len(executed)} executed actions were confirmed by reading the system back"),
        b.metric("mttr", "Median time to close", needs=S, value=round(median(hours), 1) if hours else None, unit="h", drill="cases:closed", good="down",
                 unknown="" if hours else "no_data",
                 note=(f"from {len(hours)} of {len(closed)} closed case(s)" + (f"; {len(untimed)} carry no closing time" if untimed else "")) if hours
                 else (f"{len(closed)} case(s) closed, but the product reports no closing time for them" if closed else "no case was closed in this range")),
        b.metric("closed_cases", "Closed in range", needs=S, value=len(closed), drill="cases:closed", good="up"),
        b.metric("new_cases", "Opened in range", needs=S, value=len([r for r in in_range if r.at]), note="cases with a known opening time"),
    ]))
    by_sev = b.group(opened, lambda r: r.get("severity") or "unknown")
    d.panels.append(panel("by_severity", "Open cases by severity", "bars", sources=S, empty="" if opened else "No case is open.", items=[
        {"label": s.title(), "value": len(by_sev[s]), "tone": SEV_TONE.get(s, "mut"), "drill": b.drill(f"sev:{s}", f"Open cases: {s}", by_sev[s], columns=cols)}
        for s in SEVERITIES if s in by_sev]))
    order = ("proposed", "approved", "executed", "verified")
    counts = {"proposed": len(actions), "approved": len([a for a in actions if a.status in ("APPROVED", "EXECUTED")]),
              "executed": len(executed), "verified": len(verified)}
    b.drill("actions:all", "Remediation actions", actions, columns=("capability", "verification"))
    d.panels.append(panel("funnel", "Remediation funnel", "funnel", sources=S, empty="" if actions else "No remediation action has been proposed.",
                          items=[{"label": k.title(), "value": counts[k], "tone": "ok" if k == "verified" else "run",
                                  "drill": {"proposed": "actions:all", "verified": "actions:verified"}.get(k, "")} for k in order],
                          note="An action counts as done only when the read-back verified it."))
    days = b.days()
    opened_by = {dd: 0 for dd in days}
    closed_by = {dd: 0 for dd in days}
    for r in cases_all:
        if r.at and day_of(r.at) in opened_by:
            opened_by[day_of(r.at)] += 1
        ca = float(r.get("closed_at") or 0)
        if ca and day_of(ca) in closed_by:
            closed_by[day_of(ca)] += 1
    dated = any(opened_by.values()) or any(closed_by.values())
    d.panels.append(panel("flow", "Cases opened and closed", "timeseries", sources=S, x=days, unit="cases",
                          series=[{"id": "opened", "label": "Opened", "tone": "warn", "values": [opened_by[x] for x in days]},
                                  {"id": "closed", "label": "Closed", "tone": "ok", "values": [closed_by[x] for x in days]}],
                          empty="" if dated else "The product reports no opening or closing times for its cases in this range."))
    controls = b.group([r for r in cases_all if r.get("control")], lambda r: r.get("control"))
    d.panels.append(panel("compliance", "Open findings by control", "bars", sources=S,
                          items=[{"label": k, "value": len([r for r in v if r.get("open")]), "tone": "bad" if any(r.get("open") for r in v) else "ok",
                                  "drill": b.drill(f"control:{k}", f"Findings for {k}", v, columns=cols)} for k, v in sorted(controls.items())],
                          empty="" if controls else "No finding names a compliance control, so control coverage cannot be shown.",
                          note="Only findings the product tagged with a control appear here."))
    rows = [row({"case": r.title, "severity": str(r.get("severity")).title(), "status": r.status.replace("_", " ").capitalize(), "project": b.project_name(r.project), "evidence": r.get("evidence")},
                tone=SEV_TONE.get(str(r.get("severity")), "mut"), ref=r.ref, url=r.url,
                drill=b.drill(f"case:{r.id}", r.title, [r] + [a for a in actions if a.get("case_id") == r.id], columns=("severity", "capability", "verification")))
            for r in sorted(opened, key=lambda r: SEVERITIES.index(r.get("severity")) if r.get("severity") in SEVERITIES else 9)[:50]]
    d.panels.append(panel("open_cases", "Open cases", "table", sources=S, span=2, rows=rows, empty="" if rows else "No case is open.",
                          columns=[col("case", "Case"), col("severity", "Severity"), col("status", "Status"), col("project", "Project"), col("evidence", "Evidence", "right")]))
    d.summary = f"{len(opened)} open case(s), {len(crit)} critical or high; {len(waiting)} action(s) waiting for approval."


# ── mission and workflow performance ────────────────────────────────────────────────────────────────────────────────
def missions(b: Build) -> None:
    d, S = b.d, ("missions",)
    if not b.usable("missions"):
        b.down("Missions cannot be read right now.", "missions", "Missions", S)
        return
    ms = b.records("missions", "mission")
    att = b.records("attention", "attention")
    done = [r for r in ms if r.status == "completed"]
    failed = [r for r in ms if r.status == "failed"]
    needs = [r for r in ms if r.status == "needs"]
    running = [r for r in ms if r.status in ("running", "scheduled")]
    finished = done + failed
    cols = ("workflow", "progress")
    for key, title, recs in (("m:all", "Missions", ms), ("m:failed", "Failed missions", failed), ("m:needs", "Missions waiting for a person", needs),
                             ("m:running", "Running and scheduled missions", running), ("m:done", "Completed missions", done)):
        b.drill(key, title, recs, columns=cols)
    durs = [float(r.get("duration_ms")) for r in ms if r.get("duration_ms")]
    per = b.group(finished, lambda r: r.project)
    rate = b.track(b.metric("success_rate", "Success rate", needs=S, ratio=True, num=len(done), den=len(finished), unit="%", good="up", drill="m:failed" if failed else "m:done",
                            tone=tone_of(pct(len(done), len(finished)), good=95, warn=80), note=f"{len(done)} of {len(finished)} finished missions succeeded"),
                   {p: (len([r for r in v if r.status == "completed"]), len(v)) for p, v in per.items()})
    d.panels.append(panel("summary", "Missions", "stats", sources=("missions", "attention"), span=2, metrics=[
        b.metric("missions", "Missions", needs=S, value=len(ms), drill="m:all"), rate,
        b.metric("failed", "Failed", needs=S, value=len(failed), drill="m:failed", tone="bad" if failed else "ok", good="down"),
        b.metric("needs", "Waiting for a person", needs=S, value=len(needs), drill="m:needs", tone="warn" if needs else "ok", good="down"),
        b.metric("running", "Running or scheduled", needs=S, value=len(running), drill="m:running", tone="run"),
        b.metric("p50", "Median duration", needs=S, value=round(median(durs) / 1000.0, 1) if durs else None, unit="s", good="down",
                 unknown="" if durs else "no_data", note="" if durs else "this deployment's missions carry no timing"),
        b.metric("attention", "Attention items", needs=("attention",), value=len(att), drill=b.drill("att:all", "Attention items", att, columns=("reason", "consequence")),
                 tone="warn" if att else "ok", good="down"),
    ]))
    wfs = sorted({str(r.get("workflow") or "—") for r in ms})
    states = (("completed", "Completed", "ok"), ("running", "Running", "run"), ("scheduled", "Scheduled", "mut"), ("needs", "Needs a person", "warn"), ("failed", "Failed", "bad"))
    series = []
    for st, label, tone in states:
        vals, drills = [], []
        for w in wfs:
            recs = [r for r in ms if r.status == st and str(r.get("workflow") or "—") == w]
            vals.append(len(recs))
            drills.append(b.drill(f"wf:{w}:{st}", f"{w}: {label.lower()}", recs, columns=cols) if recs else "")
        series.append({"id": st, "label": label, "tone": tone, "values": vals, "drills": drills})
    d.panels.append(panel("by_workflow", "Missions by workflow", "stacked", sources=S, x=wfs, series=series, span=2, empty="" if ms else "No mission has run."))
    kinds = b.group(att, lambda r: r.status)
    d.panels.append(panel("attention", "What needs a person", "bars", sources=("attention",), empty="" if att else "Nothing needs a person.",
                          items=[{"label": k.replace("_", " ").title(), "value": len(v), "tone": "warn",
                                  "drill": b.drill(f"att:{k}", f"Attention: {k.replace('_', ' ')}", v, columns=("reason", "consequence"))} for k, v in sorted(kinds.items())]))
    days = b.days()
    timed = [r for r in finished if r.at]
    ok_by = {x: 0 for x in days}
    bad_by = {x: 0 for x in days}
    for r in timed:
        k = day_of(r.at)
        if k in ok_by:
            (ok_by if r.status == "completed" else bad_by)[k] += 1
    d.panels.append(panel("outcomes", "Mission outcomes per day", "timeseries", sources=S, x=days, unit="missions",
                          series=[{"id": "ok", "label": "Succeeded", "tone": "ok", "values": [ok_by[x] for x in days]},
                                  {"id": "bad", "label": "Failed", "tone": "bad", "values": [bad_by[x] for x in days]}],
                          empty="" if timed else "These missions carry no finish time, so a daily history cannot be drawn. The trend under Success rate builds from today on."))
    rows = [row({"mission": r.title, "workflow": r.get("workflow"), "state": r.status.title(), "progress": r.get("progress"), "project": b.project_name(r.project)},
                tone="bad" if r.status == "failed" else "warn", ref=r.ref, url=r.url) for r in (failed + needs)[:50]]
    d.panels.append(panel("trouble", "Failed or waiting", "table", sources=S, span=2, rows=rows, empty="" if rows else "No mission is failed or waiting.",
                          columns=[col("mission", "Mission"), col("workflow", "Workflow"), col("state", "State"), col("progress", "Progress"), col("project", "Project")]))
    d.summary = f"{len(ms)} mission(s): {len(done)} completed, {len(failed)} failed, {len(needs)} waiting for a person."


# ── portfolio health and executive overview ─────────────────────────────────────────────────────────────────────────
def portfolio(b: Build) -> None:
    d = b.d
    qa = QaModel(b)
    sec_ok, mis_ok = b.usable("sentinel"), b.usable("missions")
    opened = _open_cases(b) if sec_ok else []
    sec_all = b.records("sentinel", timed=False) if sec_ok else []
    ms = b.records("missions", "mission") if mis_ok else []
    att = b.records("attention", "attention")
    cols = ("suite_name", "platform", "reason")
    rows, ready, known = [], 0, 0
    health_pts: Dict[str, tuple] = {}
    for p in b.projects:
        pid = str(p["id"])
        if b.f.project and pid != b.f.project:
            continue
        rd = qa.readiness(pid) if qa.available else {"state": "unknown", "tone": "mut", "label": "Not known", "reasons": [], "counts": {}}
        k = rd["counts"]
        crit = [r for r in opened if r.project == pid and r.get("severity") in ("critical", "high")]
        pm = [r for r in ms if r.project == pid]
        pfail = [r for r in pm if r.status == "failed"]
        pneeds = [r for r in att if r.project == pid]
        # a signal exists only where the project has records in that source: a project nothing reports on is "not known", not healthy
        has_sec = sec_ok and any(r.project == pid for r in sec_all)
        signals = [rd["tone"] if rd["state"] != "unknown" else "", "bad" if crit else "ok" if has_sec else "", "bad" if pfail else "ok" if pm else ""]
        signals = [s for s in signals if s]
        tone = "mut" if not signals else "bad" if "bad" in signals else "warn" if "warn" in signals or pneeds else "ok"
        if signals:
            known += 1
            ready += tone == "ok"
            health_pts[pid] = (1.0 if tone == "ok" else 0.0, 1.0)
        recs = ([qa.cell_record(x) for st in ("failed", "blocked", "unexecuted") for x in qa.cells(st, pid)] if qa.available else []) + crit + pfail + pneeds
        rows.append(row({"project": b.project_name(pid), "health": {"ok": "Healthy", "warn": "Needs attention", "bad": "At risk", "mut": "Not known"}[tone],
                         "release": rd["label"], "coverage": pct(k.get("executed"), k.get("planned")) if k else None,
                         "failed_tests": k.get("failed") if k and k.get("planned") else None, "critical": len(crit) if has_sec else None,
                         "missions_failed": len(pfail) if pm else None, "needs_you": len(pneeds)},
                        tone=tone, drill=b.drill(f"project:{pid}", f"{b.project_name(pid)}: what blocks the release and needs attention", recs, columns=cols + ("severity", "workflow")) if recs else ""))
    c = qa.counts() if qa.available else {}
    crit_all = [r for r in opened if r.get("severity") in ("critical", "high")]
    failed_m = [r for r in ms if r.status == "failed"]
    finished = [r for r in ms if r.status in ("completed", "failed")]
    healthy = b.track(b.metric("healthy", "Healthy projects", needs=("missions", "sentinel") + QA_SRC, any_of=True, ratio=True, num=ready, den=known, unit="%", good="up",
                               tone=tone_of(pct(ready, known), good=100, warn=60), note=f"{ready} of {known} projects with data have no failing signal",
                               unknown="" if known else "no_data"), health_pts)
    d.panels.append(panel("summary", "Portfolio", "stats", sources=("missions", "attention", "sentinel") + QA_SRC, span=2, metrics=[
        b.metric("projects", "Projects", needs=("missions",), any_of=True, value=len(rows)), healthy,
        b.metric("coverage", "Test execution coverage", needs=QA_SRC, any_of=True, ratio=True, num=c.get("executed"), den=c.get("planned"), unit="%", good="up",
                 tone=tone_of(pct(c.get("executed"), c.get("planned")), good=95, warn=80),
                 note=f"{c.get('executed', 0)} of {c.get('planned', 0)} planned tests produced a verdict; open the QA dashboard for the breakdown"),
        b.metric("critical", "Critical and high findings", needs=("sentinel",), value=len(crit_all), tone="bad" if crit_all else "ok", good="down",
                 drill=b.drill("critical", "Open critical and high cases", crit_all, columns=("severity", "case_type"))),
        b.metric("mission_success", "Mission success rate", needs=("missions",), ratio=True, num=len([r for r in finished if r.status == "completed"]), den=len(finished),
                 unit="%", good="up", tone=tone_of(pct(len(finished) - len(failed_m), len(finished)), good=95, warn=80),
                 drill=b.drill("missions_failed", "Failed missions", failed_m, columns=("workflow", "progress")) if failed_m else ""),
        b.metric("needs_you", "Waiting for a person", needs=("attention",), value=len(att), tone="warn" if att else "ok", good="down",
                 drill=b.drill("needs_you", "What is waiting for a person", att, columns=("reason", "consequence"))),
    ]))
    d.panels.append(panel("projects", "Projects", "table", sources=("missions", "attention", "sentinel") + QA_SRC, span=2, rows=rows,
                          empty="" if rows else "No project is in scope.",
                          note="A project is healthy when its release is ready, it has no open critical or high finding and no failed mission. A blank cell means that source is not answering.",
                          columns=[col("project", "Project"), col("health", "Health"), col("release", "Release"), col("coverage", "Coverage %", "right"),
                                   col("failed_tests", "Failed tests", "right"), col("critical", "Critical/high", "right"),
                                   col("missions_failed", "Failed missions", "right"), col("needs_you", "Needs you", "right")]))
    risks = sorted(crit_all, key=lambda r: SEVERITIES.index(r.get("severity")))[:5] + failed_m[:5]
    d.panels.append(panel("risks", "Top risks", "table", sources=("sentinel", "missions"), span=2,
                          rows=[row({"what": r.title, "kind": "Security case" if r.kind == "security_case" else "Failed mission",
                                     "detail": str(r.get("severity") or r.get("workflow") or ""), "project": b.project_name(r.project)},
                                    tone="bad", ref=r.ref, url=r.url) for r in risks],
                          columns=[col("what", "What"), col("kind", "Kind"), col("detail", "Detail"), col("project", "Project")],
                          empty="" if risks else "No open critical finding and no failed mission."))
    d.summary = f"{ready} of {known} project(s) healthy; {len(crit_all)} critical or high finding(s); {len(att)} item(s) waiting for a person."


# ── infrastructure and deployments ──────────────────────────────────────────────────────────────────────────────────
def infrastructure(b: Build) -> None:
    d = b.d
    units = b.records("runtime", timed=False)
    finds = b.records("deployment", "deployment_finding", timed=False)
    if not b.any_usable("runtime", "deployment"):
        b.down("Runtime health cannot be read right now.", "units", "Components", ("runtime",))
        return
    bad = [u for u in units if u.status == "bad"]
    warn = [u for u in units if u.status == "warn"]
    cols = ("group", "detail", "version")
    b.drill("units", "Components", units, columns=cols)
    b.drill("units:bad", "Components that are down or degraded", bad + warn, columns=cols)
    high = [f for f in finds if f.status in ("critical", "high")]
    ok = len([u for u in units if u.status == "ok"])
    avail = b.track(b.metric("available", "Components healthy", needs=("runtime",), ratio=True, num=ok, den=len(units), unit="%", good="up", drill="units:bad" if bad or warn else "units",
                             tone=tone_of(pct(ok, len(units)), good=100, warn=80), note=f"{ok} of {len(units)} components report healthy"), {"": (ok, len(units))})
    d.panels.append(panel("summary", "Deployment", "stats", sources=("runtime", "deployment"), span=2, metrics=[
        b.metric("units", "Components", needs=("runtime",), value=len(units), drill="units"), avail,
        b.metric("down", "Down", needs=("runtime",), value=len(bad), drill="units:bad", tone="bad" if bad else "ok", good="down"),
        b.metric("findings", "Deployment findings", needs=("deployment",), value=len(finds), tone="warn" if finds else "ok", good="down",
                 drill=b.drill("findings", "Deployment findings", finds, columns=("plane", "finding"))),
        b.metric("findings_high", "Critical and high", needs=("deployment",), value=len(high), tone="bad" if high else "ok", good="down",
                 drill=b.drill("findings:high", "Critical and high deployment findings", high, columns=("plane", "finding"))),
    ]))
    d.panels.append(panel("components", "Components", "table", sources=("runtime",), span=2, empty="" if units else "No component is reported.",
                          rows=[row({"component": u.title, "kind": str(u.get("group")).title(), "state": {"ok": "Healthy", "warn": "Degraded", "bad": "Down"}.get(u.status, u.status.title()),
                                     "detail": u.get("detail") or u.get("role"), "version": u.get("version")}, tone=u.status if u.status in ("ok", "warn", "bad") else "mut", ref=u.ref, url=u.url)
                                for u in sorted(units, key=lambda u: ("bad", "warn", "run", "mut", "ok").index(u.status) if u.status in ("bad", "warn", "run", "mut", "ok") else 3)],
                          columns=[col("component", "Component"), col("kind", "Kind"), col("state", "State"), col("detail", "Detail"), col("version", "Version")]))
    by = b.group(finds, lambda f: f.status)
    d.panels.append(panel("findings", "Deployment findings by severity", "bars", sources=("deployment",), empty="" if finds else "The inspection reports no finding.",
                          items=[{"label": s.title(), "value": len(by[s]), "tone": SEV_TONE.get(s, "mut"),
                                  "drill": b.drill(f"findings:{s}", f"Deployment findings: {s}", by[s], columns=("plane", "finding"))} for s in SEVERITIES if s in by]))
    d.summary = f"{ok} of {len(units)} component(s) healthy; {len(finds)} deployment finding(s)."


# ── AI resource utilisation and cost ────────────────────────────────────────────────────────────────────────────────
def ai_costs(b: Build) -> None:
    """Runtime work as it was recorded. Actual spend and tokens are shown only when step events carry them; when they
    do not, the figure is "not known" and the planner's projection is shown beside it, labelled as a projection."""
    d, S = b.d, ("ai_usage",)
    if not b.usable("ai_usage"):
        b.down("No Mission Runtime usage is recorded on this deployment.", "steps", "Steps executed", S)
        return
    use = b.records("ai_usage", "usage")
    plans = b.records("ai_usage", "plan")
    costed = [r for r in use if r.get("cost_usd", None) is not None]
    tokened = [r for r in use if r.get("tokens", None) is not None]
    cost = round(sum(float(r.get("cost_usd")) for r in costed), 4) if costed else None
    tokens = sum(int(r.get("tokens")) for r in tokened) if tokened else None
    n_m = len({r.get("mission") for r in use})
    durs = [float(r.get("duration_ms")) for r in use if r.get("duration_ms") is not None]
    failed = [r for r in use if r.status == "failed"]
    proj = [float(r.get("expected_usd")) for r in plans if r.get("expected_usd", None) is not None]
    cols = ("workflow", "capability", "duration_ms", "cost_usd", "tokens")
    b.drill("usage", "Executed steps", use, columns=cols)
    b.drill("usage:failed", "Steps that failed", failed, columns=cols)
    b.drill("plans", "Plans and their projected cost", plans, columns=("workflow", "expected_usd", "expected_success", "expected_approvals"))
    per = b.group(use, lambda r: r.project)
    steps = b.track(b.metric("steps", "Steps executed", needs=S, value=len(use), drill="usage", note=f"across {n_m} mission(s) in the range"),
                    {p: (len(v), None) for p, v in per.items()} or {"": (0, None)})
    missing = "the runtime's step records carry no spend or token figure"
    d.panels.append(panel("summary", "Usage", "stats", sources=S, span=2, metrics=[
        steps, b.metric("missions", "Missions with activity", needs=S, value=n_m),
        b.metric("cost", "Model spend, recorded", needs=S, value=cost, unit="USD", drill="usage", good="down", unknown="" if costed else "no_data",
                 note=f"{len(costed)} of {len(use)} step(s) carry a spend figure" if costed else missing),
        b.metric("tokens", "Tokens, recorded", needs=S, value=tokens, drill="usage", unknown="" if tokened else "no_data",
                 note=f"{len(tokened)} of {len(use)} step(s) carry a token count" if tokened else missing),
        b.metric("projected", "Projected cost of plans", needs=S, value=round(sum(proj), 4) if proj else None, unit="USD", drill="plans", unknown="" if proj else "no_data",
                 note=f"the planner's own estimate for {len(proj)} plan(s) — a projection, not a bill" if proj else "no plan carries a projection"),
        b.metric("step_p50", "Median step duration", needs=S, value=round(median(durs) / 1000.0, 2) if durs else None, unit="s", good="down", unknown="" if durs else "no_data"),
        b.metric("step_failed", "Failed steps", needs=S, value=len(failed), drill="usage:failed", tone="bad" if failed else "ok", good="down")]))
    days = b.days()
    by_day = {x: 0 for x in days}
    for r in use:
        if day_of(r.at) in by_day:
            by_day[day_of(r.at)] += 1
    d.panels.append(panel("daily", "Steps executed per day", "timeseries", sources=S, x=days, unit="steps", empty="" if use else "No step ran in this range.",
                          series=[{"id": "steps", "label": "Steps", "tone": "run", "values": [by_day[x] for x in days]}]))
    for key, title, field_ in (("by_workflow", "Steps by workflow", "workflow"), ("by_operator", "Steps by operator", "operator")):
        g = b.group(use, lambda r, f=field_: str(r.get(f) or "not recorded"))
        d.panels.append(panel(key, title, "bars", sources=S, empty="" if use else "No step ran in this range.",
                              items=[{"label": k, "value": len(v), "tone": "bad" if any(r.status == "failed" for r in v) else "run",
                                      "note": (f"{round(sum(float(r.get('cost_usd')) for r in v if r.get('cost_usd', None) is not None), 4)} USD recorded"
                                               if any(r.get("cost_usd", None) is not None for r in v) else ""),
                                      "drill": b.drill(f"{key}:{k}", f"{title}: {k}", v, columns=cols)}
                                     for k, v in sorted(g.items(), key=lambda kv: -len(kv[1]))[:12]]))
    d.panels.append(panel("plans", "Plans", "table", sources=S, span=2, empty="" if plans else "No plan was created in this range.",
                          rows=[row({"workflow": r.get("workflow"), "plan": r.title, "projected": r.get("expected_usd"), "success": r.get("expected_success"),
                                     "approvals": r.get("expected_approvals"), "budget": "Within" if r.get("within_budget") else "Over" if r.get("within_budget") is False else None},
                                    tone="warn" if r.get("within_budget") is False else "", ref=r.ref) for r in plans[:50]],
                          columns=[col("workflow", "Workflow"), col("plan", "Plan"), col("projected", "Projected USD", "right"), col("success", "Expected success", "right"),
                                   col("approvals", "Approvals", "right"), col("budget", "Budget")],
                          note="Projected figures are the planner's estimates when the plan was made."))
    d.summary = (f"{len(use)} step(s) across {n_m} mission(s); " + (f"{cost} USD recorded." if costed else "actual spend is not recorded by this runtime."))


# ── knowledge, retrieval and data quality ───────────────────────────────────────────────────────────────────────────
def knowledge(b: Build) -> None:
    d, S = b.d, ("context_sources",)
    if not b.usable("context_sources"):
        b.down("No context source is connected.", "sources", "Context sources", S)
        return
    src = b.records("context_sources", "context_source", timed=False)
    healthy = [s for s in src if s.status == "healthy"]
    trouble = [s for s in src if s.status != "healthy"]
    disc = sum(float(s.get("discovered") or 0) for s in src)
    idx = sum(float(s.get("indexed") or 0) for s in src)
    skipped = sum(float(s.get("skipped") or 0) for s in src)
    counted = [s for s in src if s.get("discovered") is not None and s.get("indexed") is not None]
    cols = ("kind", "provider", "detail")
    b.drill("src", "Context sources", src, columns=cols)
    b.drill("src:trouble", "Sources that are stale, degraded or failing", trouble, columns=cols)
    b.drill("src:counted", "Sources that report what they indexed", counted, columns=("discovered", "indexed", "skipped"))
    hl = b.track(b.metric("healthy", "Sources healthy", needs=S, ratio=True, num=len(healthy), den=len(src), unit="%", good="up", drill="src:trouble" if trouble else "src",
                          tone=tone_of(pct(len(healthy), len(src)), good=100, warn=75)), {"": (len(healthy), len(src))})
    d.panels.append(panel("summary", "Context", "stats", sources=S, span=2, metrics=[
        b.metric("sources", "Context sources", needs=S, value=len(src), drill="src"), hl,
        b.metric("indexed", "Indexed, of discovered", needs=S, ratio=True, num=idx if counted else None, den=disc if counted else None, unit="%", good="up", drill="src:counted",
                 tone=tone_of(pct(idx, disc), good=98, warn=90), unknown="" if counted else "no_data",
                 note=f"{int(idx)} of {int(disc)} items, over the {len(counted)} source(s) that report counts" if counted else "no source reports what it indexed"),
        b.metric("skipped", "Skipped items", needs=S, value=skipped if counted else None, tone="warn" if skipped else "ok", good="down", unknown="" if counted else "no_data",
                 drill="src:counted"),
        b.metric("stale", "Stale or failing", needs=S, value=len(trouble), drill="src:trouble", tone="warn" if trouble else "ok", good="down")]))
    d.panels.append(panel("sources", "Sources", "table", sources=S, span=2,
                          rows=[row({"source": s.title, "kind": str(s.get("kind")).replace("_", " "), "state": s.status.title(), "detail": s.get("detail"),
                                     "indexed": s.get("indexed"), "discovered": s.get("discovered"), "skipped": s.get("skipped"), "verified": s.get("last_verified")},
                                    tone={"healthy": "ok", "error": "bad"}.get(s.status, "warn"), ref=s.ref) for s in src],
                          columns=[col("source", "Source"), col("kind", "Kind"), col("state", "State"), col("detail", "Detail"), col("indexed", "Indexed", "right"),
                                   col("discovered", "Discovered", "right"), col("skipped", "Skipped", "right"), col("verified", "Last verified")]))
    kinds = b.group(src, lambda s: str(s.get("kind") or "other"))
    d.panels.append(panel("by_kind", "Sources by kind", "bars", sources=S, items=[
        {"label": k.replace("_", " ").title(), "value": len(v), "tone": "bad" if any(s.status == "error" for s in v) else "warn" if any(s.status != "healthy" for s in v) else "ok",
         "drill": b.drill(f"kind:{k}", f"Sources: {k}", v, columns=cols)} for k, v in sorted(kinds.items())]))
    d.panels.append(panel("retrieval", "Retrieval quality", "stats", sources=S, metrics=[
        b.metric("retrieval", "Answer grounding", needs=S, unknown="no_data",
                 note="This deployment does not record retrieval evaluations, so no quality figure is shown.")]))
    d.summary = f"{len(healthy)} of {len(src)} context source(s) healthy; {int(skipped)} item(s) skipped."


__all__ = ["security", "missions", "portfolio", "infrastructure", "ai_costs", "knowledge"]

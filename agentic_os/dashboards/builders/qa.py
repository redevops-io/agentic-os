"""QA, test coverage and release readiness — across projects.

The unit is a CELL: one test of one suite on one platform of one project. The PLANNED inventory is every cell the
catalog declares plus every cell that reported a result inside the inventory window, and, for a suite whose catalog entry
names its platforms, every such test on every named platform; a test that once ran is expected
to run again. For each (project, suite, platform) the LATEST run inside the selected range decides each cell:

    passed | failed          the run produced a verdict for the test
    unsupported              the engine/platform could not run it (it never counts as executed)
    blocked                  it was in the run but no verdict came out (error, timeout, skipped, refused)
    unexecuted               it is planned and the latest run did not include it, or no run happened in the range

Coverage is ``(passed + failed) / planned`` and the pass rate is ``passed / planned``. Neither is ever computed from the
executed tests alone, so a suite that ran three of forty tests cannot read as 100 %.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..build import Build, col, panel, pct, row, tone_of
from ..contracts import Record
from ..store import DAY, day_of

STATUSES = ("passed", "failed", "blocked", "unsupported", "unexecuted")
TONE = {"passed": "ok", "failed": "bad", "blocked": "warn", "unsupported": "mut", "unexecuted": "mut"}
LABEL = {"passed": "Passed", "failed": "Failed", "blocked": "Blocked", "unsupported": "Unsupported", "unexecuted": "Not executed"}
WORST = ("failed", "blocked", "unexecuted", "unsupported", "passed")
SRC = ("qa", "qa_ingest")
Cell = Tuple[str, str, str, str]          # project, suite, platform, test


class QaModel:
    """The cells, their latest status and their history, computed once and shared with the portfolio dashboard."""

    def __init__(self, b: Build) -> None:
        self.b = b
        cfg = b.cache.ctx.config
        self.available = b.any_usable(*SRC)
        inv_since = b.now - cfg.inventory_days * DAY
        results: List[Record] = []
        runs: List[Record] = []
        for sid in SRC:
            results += b.records(sid, "qa_result", all_time=True)
            runs += b.records(sid, "qa_run", all_time=True)
        self.runs_in_range = [r for r in runs if r.at and b.since <= r.at <= b.until]
        self.suite_names: Dict[Tuple[str, str], str] = {}
        self.history: Dict[Cell, List[Record]] = {}
        self.planned: Dict[Cell, Dict[str, Any]] = {}
        req_of: Dict[Cell, set] = {}
        platforms_seen: Dict[Tuple[str, str], set] = {}
        for r in results:
            cell = (r.project, str(r.get("suite")), str(r.get("platform") or "default"), str(r.get("test_id")))
            self.history.setdefault(cell, []).append(r)
            self.suite_names[(cell[0], cell[1])] = str(r.get("suite_name") or cell[1])
            platforms_seen.setdefault((cell[0], cell[1]), set()).add(cell[2])
            if r.at >= inv_since:
                self.planned.setdefault(cell, {"declared": False})
            for q in r.get("requirements") or []:
                req_of.setdefault(cell, set()).add(str(q))
        for s in cfg.suites.values():
            if not b.access.project(s.project) or (b.f.project and s.project != b.f.project):
                continue
            if any(getattr(b.f, k) and v and v != getattr(b.f, k) for k, v in (("environment", s.environment), ("product", s.product))):
                continue
            self.suite_names[(s.project, s.id)] = s.name
            plats = s.platforms or tuple(sorted(platforms_seen.get((s.project, s.id), ()))) or ("default",)
            for t in s.tests:
                for p in (t.platforms or plats):
                    if b.f.platform and p != b.f.platform:
                        continue
                    cell = (s.project, s.id, p, t.id)
                    self.planned.setdefault(cell, {})["declared"] = True
                    if t.requirements:
                        req_of.setdefault(cell, set()).update(t.requirements)
            # A suite that names its platforms is expected on every one of them. A test that reported on any of them is therefore
            # planned on all of them: a platform nobody ran reads "not executed" instead of silently not existing.
            if s.platforms:
                pinned = {t.id for t in s.tests if t.platforms}                 # a test declared for specific platforms keeps them
                seen = {c[3] for c in self.planned if c[0] == s.project and c[1] == s.id} - pinned
                for tid in seen:
                    for p in s.platforms:
                        if b.f.platform and p != b.f.platform:
                            continue
                        self.planned.setdefault((s.project, s.id, p, tid), {"declared": False})
        for h in self.history.values():
            h.sort(key=lambda r: r.at)
        # the latest run in range per (project, suite, platform)
        self.latest_run: Dict[Tuple[str, str, str], Tuple[float, str]] = {}
        for cell, h in self.history.items():
            for r in h:
                if b.since <= r.at <= b.until:
                    k = cell[:3]
                    if k not in self.latest_run or r.at > self.latest_run[k][0]:
                        self.latest_run[k] = (r.at, str(r.get("run_id")))
        self.status: Dict[Cell, str] = {}
        self.latest: Dict[Cell, Optional[Record]] = {}
        for cell in self.planned:
            lr = self.latest_run.get(cell[:3])
            hit = next((r for r in reversed(self.history.get(cell, [])) if lr and str(r.get("run_id")) == lr[1]), None)
            self.latest[cell] = hit
            self.status[cell] = hit.status if hit is not None else "unexecuted"
        self.requirements_of = req_of
        self.cfg = cfg

    # ── records for drill-downs: an unexecuted cell has no result, so it gets a row saying so ────────────────
    def cell_record(self, cell: Cell) -> Record:
        hit = self.latest.get(cell)
        if hit is not None:
            return hit
        prev = (self.history.get(cell) or [None])[-1]
        return Record(source=prev.source if prev else "qa", kind="qa_result", id="/".join(cell), title=cell[3], status="unexecuted", project=cell[0],
                      at=prev.at if prev else 0.0,
                      fields={"suite": cell[1], "suite_name": self.suite_names.get(cell[:2], cell[1]), "platform": cell[2], "test_id": cell[3],
                              "reason": "last result is outside the selected range" if prev else "declared in the catalog; never reported",
                              "last_status": prev.status if prev else ""},
                      ref=prev.ref if prev else f"catalog:{cell[1]}", url=prev.url if prev else "")

    def cells(self, status: str = "", project: str = "") -> List[Cell]:
        return [c for c, s in self.status.items() if (not status or s == status) and (not project or c[0] == project)]

    def counts(self, project: str = "") -> Dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for c, s in self.status.items():
            if not project or c[0] == project:
                out[s] += 1
        out["planned"] = sum(out[s] for s in STATUSES)
        out["executed"] = out["passed"] + out["failed"]
        return out

    def transitions(self, cell: Cell) -> int:
        seq = [r.status for r in self.history.get(cell, []) if self.b.since <= r.at <= self.b.until and r.status in ("passed", "failed")]
        return sum(1 for a, c in zip(seq, seq[1:]) if a != c)

    def flaky(self) -> List[Cell]:
        return [c for c in self.planned if self.transitions(c) >= 2]

    def regressions(self) -> List[Cell]:
        out = []
        for c in self.cells("failed"):
            h = [r for r in self.history.get(c, []) if r.status in ("passed", "failed")]
            if len(h) >= 2 and h[-2].status == "passed":
                out.append(c)
        return out

    def fixed(self) -> List[Cell]:
        out = []
        for c in self.cells("passed"):
            h = [r for r in self.history.get(c, []) if self.b.since <= r.at <= self.b.until and r.status in ("passed", "failed")]
            if any(r.status == "failed" for r in h[:-1]):
                out.append(c)
        return out

    def latest_runs(self, project: str = "") -> List[Record]:
        want = {(k[0], k[1], k[2], v[1]) for k, v in self.latest_run.items() if not project or k[0] == project}
        out, seen = [], set()
        for r in self.runs_in_range:
            rid = str(r.get("run_id") or r.id)
            for p in r.get("platforms") or [r.get("platform") or "default"]:
                k = (r.project, str(r.get("suite")), str(p), rid)
                if k in want and r.id not in seen:
                    seen.add(r.id)
                    out.append(r)
        return out

    def readiness(self, project: str) -> Dict[str, Any]:
        c = self.counts(project)
        reasons = []
        if not c["planned"]:
            return {"state": "unknown", "tone": "mut", "label": "No test inventory", "reasons": ["no test is planned or reported for this project"], "counts": c}
        if c["failed"]:
            reasons.append(f"{c['failed']} failed")
        if c["blocked"]:
            reasons.append(f"{c['blocked']} blocked")
        if c["unexecuted"]:
            reasons.append(f"{c['unexecuted']} not executed")
        rejected = [r for r in self.latest_runs(project) if r.get("evidence_verified") is False]
        if rejected:
            reasons.append(f"evidence rejected for {len(rejected)} run(s)")
        if reasons:
            hard = bool(c["failed"] or rejected)
            return {"state": "not_ready", "tone": "bad" if hard else "warn", "label": "Not ready", "reasons": reasons, "counts": c}
        note = [f"{c['unsupported']} unsupported on their platform"] if c["unsupported"] else []
        return {"state": "ready", "tone": "ok", "label": "Ready", "reasons": note, "counts": c}

    def requirement_states(self) -> Dict[str, Dict[str, Any]]:
        """requirement id -> {state, cells}. ``covered`` needs every linked cell passed; a requirement with no linked
        test is ``untested`` (it counts against coverage — a requirement nobody tests is not covered)."""
        linked: Dict[str, List[Cell]] = {}
        for cell, reqs in self.requirements_of.items():
            if cell in self.planned:
                for q in reqs:
                    linked.setdefault(q, []).append(cell)
        out: Dict[str, Dict[str, Any]] = {}
        declared = {rid: r for rid, r in self.cfg.requirements.items()
                    if (not r.project or self.b.access.project(r.project)) and (not self.b.f.project or not r.project or r.project == self.b.f.project)}
        for rid in sorted(set(linked) | set(declared)):
            cells = linked.get(rid, [])
            sts = [self.status[c] for c in cells]
            state = ("untested" if not cells else "failing" if "failed" in sts else "covered" if all(s == "passed" for s in sts)
                     else "partial")
            spec = declared.get(rid)
            out[rid] = {"state": state, "cells": cells, "title": spec.title if spec else rid, "priority": spec.priority if spec else "",
                        "project": spec.project if spec else (cells[0][0] if cells else "")}
        return out


def build(b: Build) -> None:
    m = QaModel(b)
    d = b.d
    if not m.available:
        b.down("No QA source is answering. Connect redevops-qa or report test results to see coverage.", "planned", "Planned tests", SRC, any_of=True)
        return
    c = m.counts()
    cells_of = lambda st: [m.cell_record(x) for x in m.cells(st)]  # noqa: E731
    cols = ("suite_name", "platform", "reason")
    for st in STATUSES:
        b.drill(f"cells:{st}", f"{LABEL[st]} tests", cells_of(st), columns=cols,
                note="A planned test the latest run did not include, or whose last result is older than the selected range." if st == "unexecuted" else "")
    b.drill("cells:planned", "Planned tests", [m.cell_record(x) for x in m.planned], columns=cols)
    b.drill("cells:executed", "Executed tests (passed or failed)", cells_of("passed") + cells_of("failed"), columns=cols)

    per_proj = {p: m.counts(p) for p in {x[0] for x in m.planned}}
    cov = b.metric("coverage", "Execution coverage", needs=SRC, any_of=True, ratio=True, num=c["executed"], den=c["planned"], unit="%", good="up",
                   drill="cells:executed", tone=tone_of(pct(c["executed"], c["planned"]), good=95, warn=80),
                   note=f"{c['executed']} of {c['planned']} planned tests produced a verdict")
    b.track(cov, {p: (v["executed"], v["planned"]) for p, v in per_proj.items()})
    passed = b.metric("pass_rate", "Passed, of planned", needs=SRC, any_of=True, ratio=True, num=c["passed"], den=c["planned"], unit="%", good="up",
                      drill="cells:passed", tone=tone_of(pct(c["passed"], c["planned"]), good=95, warn=80),
                      note=(f"{pct(c['passed'], c['executed'])} % of the tests that ran" if c["executed"] else "nothing ran") + " — the planned figure is the one to rely on")
    b.track(passed, {p: (v["passed"], v["planned"]) for p, v in per_proj.items()})
    failed = b.metric("failed", "Failed", needs=SRC, any_of=True, value=c["failed"], drill="cells:failed", tone="bad" if c["failed"] else "ok", good="down")
    b.track(failed, {p: (v["failed"], None) for p, v in per_proj.items()})
    stats = [
        b.metric("planned", "Planned tests", needs=SRC, any_of=True, value=c["planned"], drill="cells:planned",
                 note=f"{len({x[:2] for x in m.planned})} suites, {len({x[2] for x in m.planned})} platforms"),
        cov, passed, failed,
        b.metric("blocked", "Blocked", needs=SRC, any_of=True, value=c["blocked"], drill="cells:blocked", tone="warn" if c["blocked"] else "ok", good="down",
                 note="in the run, but no verdict"),
        b.metric("unexecuted", "Not executed", needs=SRC, any_of=True, value=c["unexecuted"], drill="cells:unexecuted", tone="warn" if c["unexecuted"] else "ok", good="down",
                 note="planned, absent from the latest run"),
        b.metric("unsupported", "Unsupported", needs=SRC, any_of=True, value=c["unsupported"], drill="cells:unsupported", note="the platform cannot run them"),
    ]

    reqs = m.requirement_states()
    for st in ("covered", "failing", "partial", "untested"):
        b.drill(f"req:{st}", f"Requirements: {st}", [Record(source="qa", kind="requirement", id=rid, title=v["title"], status=st, project=v["project"],
                                                              fields={"priority": v["priority"], "tests": len(v["cells"])}, ref=f"requirement:{rid}")
                                                       for rid, v in reqs.items() if v["state"] == st], columns=("priority", "tests"))
    n_cov = sum(1 for v in reqs.values() if v["state"] == "covered")
    stats.append(b.metric("requirement_coverage", "Requirement coverage", needs=SRC, any_of=True, ratio=True, num=n_cov, den=len(reqs), unit="%", good="up",
                          drill="req:covered" if reqs else "", tone=tone_of(pct(n_cov, len(reqs)), good=95, warn=75),
                          unknown="" if reqs else "no_traceability",
                          note=f"{n_cov} of {len(reqs)} requirements have every linked test passing" if reqs
                          else "no test is linked to a requirement yet (link them in the dashboards file or in reported results)"))

    latest = m.latest_runs()
    verified = [r for r in latest if r.get("evidence_verified") is True]
    b.drill("evidence:verified", "Runs with independently verified evidence", verified, columns=("suite_name", "origin"))
    b.drill("evidence:unverified", "Runs without verified evidence", [r for r in latest if r.get("evidence_verified") is not True],
            columns=("suite_name", "origin"), note="Either the evidence was rejected, or the reporter did not state that it was verified.")
    stats.append(b.metric("evidence_verified", "Evidence verified", needs=SRC, any_of=True, ratio=True, num=len(verified), den=len(latest), unit="%", good="up",
                          drill="evidence:unverified" if len(verified) < len(latest) else "evidence:verified",
                          tone=tone_of(pct(len(verified), len(latest)), good=100, warn=50),
                          note=f"{len(verified)} of {len(latest)} latest runs carry evidence a second process re-verified"))
    flaky = m.flaky()
    b.drill("flaky", "Flaky tests", [m.cell_record(x) for x in flaky], columns=cols, note="Changed between passed and failed at least twice in the range.")
    stats.append(b.metric("flaky", "Flaky", needs=SRC, any_of=True, value=len(flaky), drill="flaky", tone="warn" if flaky else "ok", good="down",
                          note="passed and failed alternately"))
    d.panels.append(panel("summary", "Coverage and results", "stats", sources=SRC, span=2, metrics=stats))

    # ── release readiness, per project ──────────────────────────────────────────────────────────────
    rows = []
    for p in b.projects:
        pid = str(p["id"])
        if b.f.project and pid != b.f.project:
            continue
        rd = m.readiness(pid)
        k = rd["counts"]
        b.drill(f"project:{pid}:open", f"{b.project_name(pid)}: what blocks the release",
                [m.cell_record(x) for st in ("failed", "blocked", "unexecuted") for x in m.cells(st, pid)], columns=cols)
        rows.append(row({"project": b.project_name(pid), "readiness": rd["label"], "why": "; ".join(rd["reasons"]) or "every planned test passed",
                         "planned": k["planned"], "coverage": pct(k["executed"], k["planned"]), "passed": k["passed"], "failed": k["failed"],
                         "blocked": k["blocked"], "unexecuted": k["unexecuted"], "unsupported": k["unsupported"]},
                        tone=rd["tone"], drill=f"project:{pid}:open" if rd["state"] == "not_ready" else ""))
    d.panels.append(panel("readiness", "Release readiness by project", "table", sources=SRC, span=2, rows=rows,
                          note="Ready means every planned test passed in its latest run and no evidence was rejected.",
                          columns=[col("project", "Project"), col("readiness", "Readiness"), col("why", "Why"), col("planned", "Planned", "right"),
                                   col("coverage", "Coverage %", "right"), col("passed", "Passed", "right"), col("failed", "Failed", "right"),
                                   col("blocked", "Blocked", "right"), col("unexecuted", "Not executed", "right"), col("unsupported", "Unsupported", "right")],
                          empty="" if rows else "No project is in scope."))

    # ── status by suite (stacked) ───────────────────────────────────────────────────────────────────
    suites = sorted({x[:2] for x in m.planned})
    x_labels = [f"{b.project_name(pj)} · {m.suite_names.get((pj, su), su)}" for pj, su in suites]
    series = []
    for st in STATUSES:
        vals, drills = [], []
        for pj, su in suites:
            cs = [x for x in m.cells(st) if x[:2] == (pj, su)]
            vals.append(len(cs))
            drills.append(b.drill(f"suite:{pj}:{su}:{st}", f"{m.suite_names.get((pj, su), su)}: {LABEL[st].lower()}", [m.cell_record(x) for x in cs], columns=cols) if cs else "")
        series.append({"id": st, "label": LABEL[st], "tone": TONE[st], "values": vals, "drills": drills})
    d.panels.append(panel("by_suite", "Results by suite", "stacked", sources=SRC, x=x_labels, series=series, empty="" if suites else "No suite has reported yet."))

    # ── platform matrix ─────────────────────────────────────────────────────────────────────────────
    platforms = sorted({x[2] for x in m.planned})
    mrows = []
    for pj, su in suites:
        cells = []
        for pl in platforms:
            cs = [x for x in m.planned if x[:3] == (pj, su, pl)]
            if not cs:
                cells.append({"status": "none", "label": "", "drill": ""})
                continue
            sts = [m.status[x] for x in cs]
            worst = next(s for s in WORST if s in sts)
            n_ok = sts.count("passed")
            cells.append({"status": worst, "label": f"{n_ok}/{len(cs)}",
                          "drill": b.drill(f"matrix:{pj}:{su}:{pl}", f"{m.suite_names.get((pj, su), su)} on {pl}", [m.cell_record(x) for x in cs], columns=cols)})
        mrows.append({"label": f"{b.project_name(pj)} · {m.suite_names.get((pj, su), su)}", "cells": cells})
    d.panels.append(panel("matrix", "Platform matrix", "matrix", sources=SRC, span=2, columns=[col(p, p) for p in platforms], rows=mrows,
                          note="Each cell is passed/planned on that platform, coloured by its worst result. An empty cell means the suite is not planned there.",
                          empty="" if mrows else "No platform has reported yet."))

    # ── regression history ──────────────────────────────────────────────────────────────────────────
    days = b.days()
    by_day: Dict[str, Dict[str, int]] = {dd: {s: 0 for s in STATUSES[:4]} for dd in days}
    for h in m.history.values():
        for r in h:
            dd = day_of(r.at)
            if dd in by_day and r.status in by_day[dd] and (not b.f.platform or str(r.get("platform")) == b.f.platform):
                by_day[dd][r.status] += 1
    # runs older than the ones loaded in full contribute their counts (taken by the database), so the chart keeps its whole range
    older = 0
    for r in m.runs_in_range:
        rc = r.get("counts", None)
        if r.get("results_loaded", True) is False and isinstance(rc, dict) and (not b.f.platform or str(r.get("platform")) == b.f.platform):
            dd = day_of(r.at)
            if dd in by_day:
                older += 1
                for st, n in rc.items():
                    if st in by_day[dd]:
                        by_day[dd][st] += int(n)
    any_hist = any(sum(v.values()) for v in by_day.values())
    d.panels.append(panel("history", "Regression history", "timeseries", sources=SRC, x=days, unit="results",
                          series=[{"id": s, "label": LABEL[s], "tone": TONE[s], "values": [by_day[dd][s] for dd in days]} for s in STATUSES[:4]],
                          note="Results reported per day. A day with no run has no bar; it is not a day of passes."
                               + (f" {older} older run(s) are counted here in total only; regressions and flakiness are read from the latest runs of each lane." if older else ""),
                          empty="" if any_hist else "No run reported a result in this range."))

    # ── defects by severity, remediation ────────────────────────────────────────────────────────────
    sev: Dict[str, List[Record]] = {}
    for x in m.cells("failed"):
        r = m.cell_record(x)
        sev.setdefault(str(r.get("severity") or "unrated").lower(), []).append(r)
    order = ["critical", "high", "medium", "low", "unrated"]
    d.panels.append(panel("severity", "Failed tests by severity", "bars", sources=SRC,
                          items=[{"label": s.title(), "value": len(v), "tone": "bad" if s in ("critical", "high") else "warn" if s == "medium" else "mut",
                                  "drill": b.drill(f"severity:{s}", f"Failed tests: {s}", v, columns=cols)}
                                 for s, v in sorted(sev.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 9)],
                          empty="" if sev else "No test is failing.",
                          note="Severity is what the reporter stated; a failure with no stated severity is unrated."))
    regress, fixed, still = m.regressions(), m.fixed(), m.cells("failed")
    b.drill("remediation:regressed", "Regressions: passed before, failing now", [m.cell_record(x) for x in regress], columns=cols)
    b.drill("remediation:fixed", "Fixed in this range: failed, passing now", [m.cell_record(x) for x in fixed], columns=cols)
    b.drill("remediation:open", "Still failing", [m.cell_record(x) for x in still], columns=cols)
    d.panels.append(panel("remediation", "Remediation status", "bars", sources=SRC, items=[
        {"label": "Still failing", "value": len(still), "tone": "bad" if still else "ok", "drill": "remediation:open"},
        {"label": "Regressed", "value": len(regress), "tone": "bad" if regress else "ok", "drill": "remediation:regressed", "note": "passed in the previous run"},
        {"label": "Fixed", "value": len(fixed), "tone": "ok", "drill": "remediation:fixed", "note": "failed earlier in the range"},
        {"label": "Flaky", "value": len(flaky), "tone": "warn" if flaky else "ok", "drill": "flaky"}]))

    if reqs:
        rrows = [row({"requirement": rid, "title": v["title"], "priority": v["priority"], "project": b.project_name(v["project"]) if v["project"] else "",
                      "state": v["state"].title(), "tests": len(v["cells"])},
                     tone={"covered": "ok", "failing": "bad", "partial": "warn", "untested": "mut"}[v["state"]],
                     drill=b.drill(f"reqcells:{rid}", f"Tests for {rid}", [m.cell_record(x) for x in v["cells"]], columns=cols) if v["cells"] else "")
                 for rid, v in sorted(reqs.items(), key=lambda kv: ("covered", "partial", "untested", "failing").index(kv[1]["state"]), reverse=True)]
        d.panels.append(panel("requirements", "Requirement traceability", "table", sources=SRC, span=2, rows=rrows[:200],
                              columns=[col("requirement", "Requirement"), col("title", "Title"), col("priority", "Priority"), col("project", "Project"),
                                       col("state", "State"), col("tests", "Tests", "right")]))
    d.summary = (f"{c['planned']} planned tests across {len(per_proj)} project(s): {c['passed']} passed, {c['failed']} failed, {c['blocked']} blocked, "
                 f"{c['unexecuted']} not executed, {c['unsupported']} unsupported.")

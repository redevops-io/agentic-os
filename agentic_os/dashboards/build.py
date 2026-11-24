"""The shared builder every dashboard uses: reads sources through the cache, applies the caller's access and filters
once, keeps the drill-down records next to each number, and records/reads metric history.

One aggregation rule everywhere: a metric is ``numerator / denominator`` over the records the caller may read. Ratios
are never averaged across projects; a portfolio ratio is the ratio of the summed parts.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .access import Access
from .contracts import NOT_CONNECTED, OK, STALE, UNAUTHORIZED, Dashboard, Drill, Filters, Metric, Panel, Record, SourceStatus
from .sources import SourceCache
from .store import DAY, DashboardStore, day_of

STALE_AFTER_S = 900.0


def pct(n: Optional[float], d: Optional[float]) -> Optional[float]:
    if n is None or not d:
        return None
    return round(100.0 * n / d, 1)


def tone_of(value: Optional[float], *, good: float, warn: float, higher_is_better: bool = True) -> str:
    if value is None:
        return "mut"
    if higher_is_better:
        return "ok" if value >= good else "warn" if value >= warn else "bad"
    return "ok" if value <= good else "warn" if value <= warn else "bad"


class Build:
    def __init__(self, *, dashboard: Dashboard, cache: SourceCache, access: Access, store: DashboardStore, filters: Filters,
                 projects: Sequence[Mapping[str, Any]], now: float) -> None:
        self.d, self.cache, self.access, self.store, self.f, self.now = dashboard, cache, access, store, filters, now
        self.projects = [p for p in projects if access.project(str(p["id"]))]
        self.project_ids = [str(p["id"]) for p in self.projects]
        self.since = filters.since or (now - max(1, filters.days) * DAY)
        self.until = filters.until or now
        self._status: Dict[str, SourceStatus] = {}
        self._facets: Dict[str, set] = {"product": set(), "environment": set(), "platform": set()}
        self.d.filters, self.d.generated_at = filters, now
        self.d.scope = "project" if filters.project else "portfolio"
        self.d.projects = [dict(p) for p in self.projects if not filters.project or p["id"] == filters.project]
        if access.scoped:
            self.d.withheld = {"partial": True, "detail": "You see the projects and sources you have access to; totals cover only those."}

    # ── reading ───────────────────────────────────────────────────────────────────────────────────
    def status(self, sid: str) -> SourceStatus:
        if sid in self._status:
            return self._status[sid]
        if not self.access.source(sid):
            from .sources import SOURCE_META  # noqa: PLC0415
            label, app, _ = SOURCE_META[sid]
            st = SourceStatus(id=sid, label=label, app=app, state=UNAUTHORIZED, detail="You do not have access to this source.")
        else:
            st = self.cache.get(sid).status
            if st.state == OK and st.observed_at and self.now - st.observed_at > STALE_AFTER_S:
                st = SourceStatus(**{**st.__dict__, "state": STALE, "detail": st.detail or "the last successful read is older than 15 minutes"})
        self._status[sid] = st
        if all(x.id != sid for x in self.d.sources):
            self.d.sources.append(st)
        return st

    def usable(self, *sids: str) -> bool:
        return all(self.status(s).usable for s in sids)

    def any_usable(self, *sids: str) -> bool:
        return any(self.status(s).usable for s in sids)

    def records(self, sid: str, kind: str = "", *, timed: bool = True, all_time: bool = False) -> List[Record]:
        """Records of one source the caller may read, inside the filters. ``timed=False`` keeps records whatever their
        date (state that is true now, e.g. an open case); a record with an unknown date is always kept."""
        if not self.status(sid).usable:
            return []
        out = []
        for r in self.cache.get(sid).records:
            if kind and r.kind != kind:
                continue
            if not self.access.project(r.project):
                continue
            for k in ("product", "environment", "platform"):
                v = r.fields.get(k)
                if v:
                    self._facets[k].add(str(v))
            if self.f.project and r.project != self.f.project:
                continue
            # a dimension filter applies to records that carry the dimension; a record without it is not excluded by it
            if any(getattr(self.f, k) and r.fields.get(k) not in (None, "") and str(r.fields[k]) != getattr(self.f, k)
                   for k in ("product", "environment", "platform")):
                continue
            if timed and not all_time and r.at and not (self.since <= r.at <= self.until):
                continue
            out.append(r)
        return out

    # ── drill-downs ───────────────────────────────────────────────────────────────────────────────
    def drill(self, key: str, title: str, records: Iterable[Record], *, note: str = "", columns: Sequence[str] = ()) -> str:
        recs = list(records)
        self.d.drills[key] = Drill(key=key, title=title, records=recs, note=note, columns=tuple(columns))
        return key

    # ── metrics ───────────────────────────────────────────────────────────────────────────────────
    def metric(self, mid: str, label: str, *, needs: Sequence[str], value: Optional[float] = None, num: Optional[float] = None,
               den: Optional[float] = None, unit: str = "", tone: str = "mut", note: str = "", drill: str = "", good: str = "",
               ratio: bool = False, unknown: str = "", any_of: bool = False) -> Metric:
        srcs = tuple(needs)
        ok = self.any_usable(*srcs) if any_of else self.usable(*srcs)
        m = Metric(id=mid, label=label, unit=unit, note=note, sources=srcs, drill=drill, good=good)
        if not ok:
            states = {self.status(s).state for s in srcs}
            m.unknown = "unauthorized" if UNAUTHORIZED in states else "not_connected" if states == {NOT_CONNECTED} else "source_unavailable"
            m.drill = ""
            return m
        if unknown:
            m.unknown = unknown
            return m
        if ratio:
            m.numerator, m.denominator = num, den
            m.value = pct(num, den)
            if m.value is None:
                m.unknown = "no_data"
        else:
            m.value, m.numerator, m.denominator = (value if value is not None else num), num, den
        m.tone = tone if m.value is not None else "mut"
        return m

    def down(self, text: str, mid: str, label: str, needs: Sequence[str], *, any_of: bool = False) -> None:
        """The whole dashboard has no usable source: say why, and show its headline number as not known (never zero)."""
        self.d.empty = text
        self.d.panels.append(Panel(id="summary", title="Not available", kind="stats", sources=tuple(needs), span=2,
                                   metrics=[self.metric(mid, label, needs=needs, any_of=any_of)]))

    def track(self, m: Metric, per_project: Mapping[str, Tuple[Optional[float], Optional[float]]]) -> Metric:
        """Remember today's value per project (only for the unfiltered default view, so history means one thing) and
        attach the trend over the projects in scope. ``per_project`` maps project id -> (numerator, denominator|None)."""
        neutral = not (self.f.product or self.f.environment or self.f.platform or self.f.since or self.f.until)
        if neutral and m.value is not None and not m.unknown:
            self.store.record_points(self.d.id, [(m.id, pid or "_", n, dn) for pid, (n, dn) in per_project.items()], now=self.now)
        scope = [self.f.project] if self.f.project else (list(self.project_ids) + ["_"] if self.access.grant.projects is None else list(self.project_ids))
        pts = self.store.series(self.d.id, m.id, scope, since=self.since, until=self.now)
        m.trend = [(day, (pct(n, dn) if m.unit == "%" else n) if n is not None else None) for day, n, dn in pts]
        vals = [v for _, v in m.trend if v is not None]
        if len(vals) >= 2:
            m.delta = round(vals[-1] - vals[0], 1)
        return m

    # ── grouping helpers ──────────────────────────────────────────────────────────────────────────
    @staticmethod
    def group(records: Iterable[Record], key: Callable[[Record], Any]) -> Dict[Any, List[Record]]:
        out: Dict[Any, List[Record]] = {}
        for r in records:
            out.setdefault(key(r), []).append(r)
        return out

    def days(self) -> List[str]:
        n = int(max(1, min(366, round((self.until - self.since) / DAY))))
        return [day_of(self.until - (n - 1 - i) * DAY) for i in range(n)]

    def project_name(self, pid: str) -> str:
        for p in self.projects:
            if p["id"] == pid:
                return str(p.get("name") or pid)
        return pid or "Unassigned"

    def finish(self) -> Dashboard:
        self.d.facets = {k: sorted(v) for k, v in self._facets.items() if v}
        if not self.d.panels and not self.d.empty:
            self.d.empty = "There is nothing to show for these filters."
        return self.d


def panel(pid: str, title: str, kind: str, *, sources: Sequence[str] = (), span: int = 1, note: str = "", **kw: Any) -> Panel:
    return Panel(id=pid, title=title, kind=kind, sources=tuple(sources), span=span, note=note, **kw)


def row(cells: Mapping[str, Any], *, tone: str = "", drill: str = "", ref: str = "", url: str = "") -> Dict[str, Any]:
    return {"cells": dict(cells), "tone": tone, "drill": drill, "ref": ref, "url": url}


def col(cid: str, label: str, align: str = "left") -> Dict[str, Any]:
    return {"id": cid, "label": label, "align": align}


__all__ = ["Build", "pct", "tone_of", "panel", "row", "col", "STALE_AFTER_S"]

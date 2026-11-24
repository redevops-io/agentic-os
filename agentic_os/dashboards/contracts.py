"""Typed analytical projections for the Projects dashboards.

One vocabulary for every dashboard, so the UI renders them with one set of components and a number can always be
traced to the records behind it:

* ``Record``        one fact read from a connected system, carrying that system's own id (``ref``) — never a copy that
                    Projects owns. Records are what a drill-down lists.
* ``SourceStatus``  whether a source answered, when its data was observed, and why it did not (not connected, down,
                    not authorised, gated). A dashboard never hides a source it depends on.
* ``Metric``        a value WITH its numerator, denominator, contributing sources and the drill key that lists the
                    records it counted. ``value is None`` means "not known" (a source is down, or the caller may not
                    see part of the data) and is never rendered as zero.
* ``Panel``         one visual block (stats, timeseries, bars, stacked, matrix, table, funnel).
* ``Dashboard``     the projection the UI renders verbatim.

Everything serialises with ``to_dict``; the TypeScript mirror is ``src/data/dashboards.ts`` in redevops-projects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

SCHEMA = "aos.dashboard/v1"

# source states, most to least usable
OK, STALE, UNAVAILABLE, NOT_CONNECTED, UNAUTHORIZED, GATED = "ok", "stale", "unavailable", "not_connected", "unauthorized", "gated"
SOURCE_STATES = (OK, STALE, UNAVAILABLE, NOT_CONNECTED, UNAUTHORIZED, GATED)
USABLE = (OK, STALE)

TONES = ("ok", "warn", "bad", "run", "mut")
PANEL_KINDS = ("stats", "timeseries", "bars", "stacked", "matrix", "table", "funnel")


@dataclass(frozen=True)
class Record:
    source: str                      # the source id that produced it ("qa", "sentinel", "missions", "twenty", ...)
    kind: str                        # "qa_result" | "qa_run" | "security_case" | "security_action" | "mission" | ...
    id: str                          # the system's own id
    title: str = ""
    status: str = ""
    project: str = ""                # project id; "" = not attributed to a project
    at: float = 0.0                  # epoch seconds the fact refers to (0 = unknown)
    fields: Mapping[str, Any] = field(default_factory=dict)   # product, environment, platform, severity, amount, ...
    ref: str = ""                    # "<system>:<id>" — what to quote back to the system of record
    url: str = ""                    # a deep link into the system of record, when it has one

    def get(self, key: str, default: Any = "") -> Any:
        return self.fields.get(key, default)

    def to_dict(self) -> Dict[str, Any]:
        return {"source": self.source, "kind": self.kind, "id": self.id, "title": self.title, "status": self.status,
                "project": self.project, "at": self.at, "fields": dict(self.fields), "ref": self.ref or f"{self.source}:{self.id}",
                "url": self.url}


@dataclass(frozen=True)
class SourceStatus:
    id: str
    label: str
    app: str = ""                    # the connected application this source reads ("" = the runtime itself)
    state: str = OK
    detail: str = ""
    observed_at: float = 0.0         # when the data was read from the system (epoch seconds)
    records: int = 0
    basis: str = "live"              # "live" | "sample" (a demo provider's rows) | "ingested"
    remedy: str = ""

    @property
    def usable(self) -> bool:
        return self.state in USABLE

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "label": self.label, "app": self.app, "state": self.state, "detail": self.detail,
                "observed_at": self.observed_at, "records": self.records, "basis": self.basis, "remedy": self.remedy}


@dataclass(frozen=True)
class Filters:
    project: str = ""                # "" = portfolio (every project the caller may see)
    since: float = 0.0
    until: float = 0.0
    product: str = ""
    environment: str = ""
    platform: str = ""
    days: int = 30

    def to_dict(self) -> Dict[str, Any]:
        return {"project": self.project, "since": self.since, "until": self.until, "product": self.product,
                "environment": self.environment, "platform": self.platform, "days": self.days}


@dataclass
class Metric:
    id: str
    label: str
    value: Optional[float] = None
    unit: str = ""                   # "" | "%" | "USD" | "ms" | "h" | "d"
    tone: str = "mut"
    note: str = ""
    numerator: Optional[float] = None
    denominator: Optional[float] = None
    sources: Tuple[str, ...] = ()
    drill: str = ""                  # drill key ("" = no records behind it)
    trend: List[Tuple[str, Optional[float]]] = field(default_factory=list)   # [(day, value)] oldest first
    delta: Optional[float] = None    # change against the start of the range, in the metric's own unit
    good: str = ""                   # "up" | "down" | "" — which direction is an improvement
    unknown: str = ""                # why value is None ("source_unavailable" | "unauthorized" | "no_data" | "no_traceability")

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "label": self.label, "value": self.value, "unit": self.unit, "tone": self.tone,
                "note": self.note, "numerator": self.numerator, "denominator": self.denominator,
                "sources": list(self.sources), "drill": self.drill, "trend": [[d, v] for d, v in self.trend],
                "delta": self.delta, "good": self.good, "unknown": self.unknown}


@dataclass
class Panel:
    id: str
    title: str
    kind: str
    sources: Tuple[str, ...] = ()
    note: str = ""
    empty: str = ""                  # set when there is nothing to draw: the reason, in plain words
    metrics: List[Metric] = field(default_factory=list)            # stats
    x: List[str] = field(default_factory=list)                     # timeseries / stacked categories
    series: List[Dict[str, Any]] = field(default_factory=list)     # [{id,label,tone,values:[...],drills?:[...]}]
    items: List[Dict[str, Any]] = field(default_factory=list)      # bars / funnel: [{label,value,tone,drill,note}]
    columns: List[Dict[str, Any]] = field(default_factory=list)    # table / matrix columns: [{id,label,align}]
    rows: List[Dict[str, Any]] = field(default_factory=list)       # table: [{cells:{},tone,drill,ref,url}] / matrix: [{label,cells:[{status,label,drill}]}]
    unit: str = ""
    span: int = 1                    # 1 = half width, 2 = full width (desktop)

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "title": self.title, "kind": self.kind, "sources": list(self.sources), "note": self.note,
                "empty": self.empty, "metrics": [m.to_dict() for m in self.metrics], "x": list(self.x),
                "series": list(self.series), "items": list(self.items), "columns": list(self.columns),
                "rows": list(self.rows), "unit": self.unit, "span": self.span}


@dataclass
class Drill:
    key: str
    title: str
    records: List[Record]
    note: str = ""
    columns: Sequence[str] = ()      # extra field names worth showing as columns


@dataclass
class Dashboard:
    id: str
    title: str
    priority: str
    summary: str = ""
    scope: str = "portfolio"         # "portfolio" | "project"
    filters: Filters = field(default_factory=Filters)
    generated_at: float = 0.0
    sources: List[SourceStatus] = field(default_factory=list)
    panels: List[Panel] = field(default_factory=list)
    facets: Dict[str, List[str]] = field(default_factory=dict)     # values available for each filter
    projects: List[Dict[str, Any]] = field(default_factory=list)   # the projects this projection covers
    withheld: Dict[str, Any] = field(default_factory=dict)         # what the caller may not see (counts only, no values)
    empty: str = ""
    advanced: List[Dict[str, Any]] = field(default_factory=list)   # links into Control Tower / Metabase, when configured
    drills: Dict[str, Drill] = field(default_factory=dict, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {"schema": SCHEMA, "id": self.id, "title": self.title, "priority": self.priority, "summary": self.summary,
                "scope": self.scope, "filters": self.filters.to_dict(), "generated_at": self.generated_at,
                "sources": [s.to_dict() for s in self.sources], "panels": [p.to_dict() for p in self.panels],
                "facets": {k: list(v) for k, v in self.facets.items()}, "projects": list(self.projects),
                "withheld": dict(self.withheld), "empty": self.empty, "advanced": list(self.advanced)}


__all__ = ["SCHEMA", "OK", "STALE", "UNAVAILABLE", "NOT_CONNECTED", "UNAUTHORIZED", "GATED", "SOURCE_STATES", "USABLE",
           "TONES", "PANEL_KINDS", "Record", "SourceStatus", "Filters", "Metric", "Panel", "Drill", "Dashboard"]

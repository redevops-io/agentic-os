"""Visualization contracts — a reviewable description of a Metabase chart to create.

The design keeps the request HONEST and GOVERNABLE: a :class:`VizSpec` carries the EXACT native SQL the card will
run, so a human approver (and the audit trail) sees precisely what will execute — the system never hides query
text behind an opaque "card". Natural-language requests are mapped to vetted SQL templates (see ``nl``), not
free-form LLM-authored SQL, and any card creation is approval-gated (see ``governed``).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class DisplayType(str, Enum):
    """Metabase ``display`` values we support (the string is the exact Metabase token)."""
    TABLE = "table"
    BAR = "bar"
    LINE = "line"
    AREA = "area"
    ROW = "row"
    PIE = "pie"
    SCATTER = "scatter"
    NUMBER = "scalar"
    PIVOT = "pivot"


# natural-language words → DisplayType (used by nl.interpret)
DISPLAY_ALIASES: Mapping[str, DisplayType] = {
    "table": DisplayType.TABLE, "grid": DisplayType.TABLE,
    "bar": DisplayType.BAR, "column": DisplayType.BAR, "bar chart": DisplayType.BAR,
    "line": DisplayType.LINE, "trend": DisplayType.LINE, "over time": DisplayType.LINE, "time series": DisplayType.LINE,
    "area": DisplayType.AREA,
    "row": DisplayType.ROW,
    "pie": DisplayType.PIE, "donut": DisplayType.PIE, "breakdown": DisplayType.PIE,
    "scatter": DisplayType.SCATTER,
    "number": DisplayType.NUMBER, "scalar": DisplayType.NUMBER, "single value": DisplayType.NUMBER, "kpi": DisplayType.NUMBER,
    "pivot": DisplayType.PIVOT,
}


@dataclass(frozen=True)
class VizSpec:
    """A structured, reviewable spec for one Metabase card. ``sql`` is explicit native SQL so approval shows
    exactly what runs; ``source`` records how the spec was formed (a vetted template, an NL match, or an LLM
    proposal — the latter still passes the same human gate)."""
    title: str
    sql: str
    display: DisplayType = DisplayType.TABLE
    database_id: int = 0
    visualization_settings: Mapping[str, Any] = field(default_factory=dict)
    description: str = ""
    source: str = "template"             # template | nl | llm

    def fingerprint(self) -> str:
        """Content hash over exactly the fields a human reviews — so an approval binds to this exact chart and
        can't be swapped for a different query after the fact."""
        canonical = "\n".join((self.title.strip(), self.display.value, str(self.database_id), self.sql.strip()))
        return "viz_" + hashlib.sha256(canonical.encode()).hexdigest()[:24]


@dataclass(frozen=True)
class VizCard:
    """A created Metabase card."""
    card_id: int
    name: str
    display: str = ""
    url: str = ""


@dataclass(frozen=True)
class VizResult:
    """The outcome of a governed create: whether it was created AND independently verified (read back)."""
    created: bool
    verified: bool
    card: VizCard | None = None
    detail: str = ""


__all__ = ["DisplayType", "DISPLAY_ALIASES", "VizSpec", "VizCard", "VizResult"]

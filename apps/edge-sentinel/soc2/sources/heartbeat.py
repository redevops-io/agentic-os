"""Monitoring liveness: a small durable log the monitor writes to, and the collector that turns it into `heartbeat` evidence.

The monitor (the loop that pulls the sentinel feed) calls `HeartbeatLog.beat()` every time it completes a pass. A window is evidence of monitoring ONLY if beats arrived throughout it: if the
longest silence inside the window (including from its start to the first beat and from the last beat to its end) exceeds `max_gap_s`, collection FAILS and the window is a gap. A monitor that
was down for part of an hour therefore does not produce an hour of coverage, and the assessor's coverage rule shows it."""
from __future__ import annotations

import sqlite3
import time
from typing import Callable

from ..pipeline import Collected, CollectionFailed


class HeartbeatLog:
    def __init__(self, path: str, *, clock: Callable[[], float] = time.time) -> None:
        self.path, self.clock = path, clock
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS beats (at REAL NOT NULL)")
            c.execute("CREATE INDEX IF NOT EXISTS beats_at ON beats (at)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30, isolation_level=None)

    def beat(self, at: "float | None" = None) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO beats (at) VALUES (?)", (self.clock() if at is None else float(at),))

    def beat_many(self, times: list) -> None:
        with self._conn() as c:
            c.executemany("INSERT INTO beats (at) VALUES (?)", [(float(t),) for t in times])

    def between(self, lo: float, hi: float) -> list:
        with self._conn() as c:
            return [r[0] for r in c.execute("SELECT at FROM beats WHERE at >= ? AND at < ? ORDER BY at", (lo, hi))]


class HeartbeatCollector:
    kind = "heartbeat"
    source = "edge sentinel monitor heartbeat log"

    def __init__(self, log: HeartbeatLog, *, name: str = "monitor-heartbeat", max_gap_s: float = 300.0) -> None:
        self.log, self.name, self.max_gap_s = log, name, float(max_gap_s)

    def collect(self, window_from: float, window_to: float) -> Collected:
        beats = self.log.between(window_from, window_to)
        if not beats:
            raise CollectionFailed("the monitor reported no heartbeat in this window")
        edges = [window_from] + beats + [window_to]
        gap = max(b - a for a, b in zip(edges, edges[1:]))
        if gap > self.max_gap_s:
            raise CollectionFailed(f"the monitor was silent for {int(gap)}s inside this window (limit {int(self.max_gap_s)}s)")
        return Collected({"beats": len(beats), "max_gap_s": gap, "population_complete": True})

"""Process Intelligence — deterministic event-log analytics (Intelligence-APIs plan §8, Table 4).

Business question: why is work stuck or abnormal, and what should happen next? Computed over the tenant's
own event log (:class:`ProcessEvent`), all pure functions, leakage-safe as-of a decision time.

`bottlenecks`      — where time is spent: mean waiting per activity across cases.
`cycle_benchmark`  — a case's cycle time vs the internal cohort (percentile + decomposition).
`next_event`       — the most likely next activity + expected time, from observed transitions.
`why_stuck`        — the case's current activity, how long it has sat, the expected-but-missing next event.
`anomaly`          — duration and unseen-transition deviations for a case.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from ...integrations.business.process import ProcessEvent


# ── result types ────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Bottleneck:
    activity: str
    mean_wait_hours: float
    n: int
    total_wait_hours: float


@dataclass(frozen=True)
class CycleBenchmark:
    case_ref: str
    cycle_hours: float
    percentile: float          # fraction of cohort cases at least as fast (higher = slower than peers)
    cohort_n: int
    median_cohort_hours: float


@dataclass(frozen=True)
class NextEvent:
    from_activity: str
    expected_activity: str
    expected_hours: float       # median observed duration of that transition
    confidence: float           # share of transitions from `from_activity` that go to `expected_activity`


@dataclass(frozen=True)
class WhyStuck:
    case_ref: str
    stuck: bool
    current_activity: str
    elapsed_hours: float
    typical_hours: float        # typical time in the current activity before it moves on
    missing_event: str          # the expected-but-absent next activity
    owner: str


@dataclass(frozen=True)
class Anomaly:
    case_ref: str
    is_anomalous: bool
    findings: tuple[tuple[str, str, str], ...]   # (type, detail, severity: low|medium|high)


# ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────
def _dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _knowable(o, as_of_ms: int) -> bool:
    return not as_of_ms or (o.prov.known_at or o.prov.observed_at) <= as_of_ms


def _by_case(events: Iterable[ProcessEvent], as_of_ms: int) -> dict[str, list[ProcessEvent]]:
    cases: dict[str, list[ProcessEvent]] = defaultdict(list)
    for e in events:
        if _knowable(e, as_of_ms) and _dt(e.at) is not None:
            cases[e.case_ref].append(e)
    for evs in cases.values():
        evs.sort(key=lambda e: e.at)
    return cases


def _transitions(cases: dict[str, list[ProcessEvent]]):
    """Yield (from_activity, to_activity, duration_hours) for each consecutive step across all cases."""
    for evs in cases.values():
        for a, b in zip(evs, evs[1:]):
            da, db = _dt(a.at), _dt(b.at)
            if da and db:
                yield a.activity, b.activity, (db - da).total_seconds() / 3600.0


def _pctile_hours(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    import math
    rank = max(1, math.ceil(q / 100.0 * len(sorted_vals)))
    return float(sorted_vals[min(rank, len(sorted_vals)) - 1])


# ── bottlenecks ─────────────────────────────────────────────────────────────────────────────────────────
def bottlenecks(events: Iterable[ProcessEvent], *, as_of_ms: int = 0) -> list[Bottleneck]:
    """Mean time each activity holds a case before it moves on, ranked by total waiting time contributed."""
    cases = _by_case(events, as_of_ms)
    waits: dict[str, list[float]] = defaultdict(list)
    for frm, _to, hours in _transitions(cases):
        waits[frm].append(hours)
    out = [Bottleneck(a, round(statistics.fmean(v), 4), len(v), round(sum(v), 4)) for a, v in waits.items()]
    return sorted(out, key=lambda b: -b.total_wait_hours)


# ── cycle benchmark ─────────────────────────────────────────────────────────────────────────────────────
def _cycle_hours(evs: list[ProcessEvent]) -> Optional[float]:
    if len(evs) < 2:
        return None
    first, last = _dt(evs[0].at), _dt(evs[-1].at)
    return (last - first).total_seconds() / 3600.0 if first and last else None


def cycle_benchmark(events: Iterable[ProcessEvent], case_ref: str, *, as_of_ms: int = 0) -> CycleBenchmark:
    """The case's end-to-end cycle time vs the distribution of the other cases' cycle times."""
    cases = _by_case(events, as_of_ms)
    target = _cycle_hours(cases.get(case_ref, []))
    cohort = sorted(c for cr, evs in cases.items() if (c := _cycle_hours(evs)) is not None)
    if target is None or not cohort:
        return CycleBenchmark(case_ref, target or 0.0, 0.0, len(cohort), 0.0)
    at_least_as_fast = sum(1 for c in cohort if c <= target)
    return CycleBenchmark(case_ref, round(target, 4), round(at_least_as_fast / len(cohort), 4),
                          len(cohort), round(statistics.median(cohort), 4))


# ── next event ──────────────────────────────────────────────────────────────────────────────────────────
def next_event(events: Iterable[ProcessEvent], from_activity: str, *, as_of_ms: int = 0) -> NextEvent:
    """The most likely next activity after `from_activity`, with its share and median observed duration."""
    cases = _by_case(events, as_of_ms)
    nexts: dict[str, list[float]] = defaultdict(list)
    total = 0
    for frm, to, hours in _transitions(cases):
        if frm == from_activity:
            nexts[to].append(hours)
            total += 1
    if not nexts:
        return NextEvent(from_activity, "", 0.0, 0.0)
    best = max(nexts.items(), key=lambda kv: len(kv[1]))
    return NextEvent(from_activity, best[0], round(statistics.median(best[1]), 4),
                     round(len(best[1]) / total, 4))


# ── why stuck ───────────────────────────────────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def why_stuck(events: Iterable[ProcessEvent], case_ref: str, *, now: str = "", as_of_ms: int = 0) -> WhyStuck:
    """Is the case sitting in its current activity longer than that activity typically takes? If so, name
    the missing next event and the owner. A terminal activity (never seen transitioning) is not 'stuck'."""
    cases = _by_case(events, as_of_ms)
    evs = cases.get(case_ref, [])
    if not evs:
        return WhyStuck(case_ref, False, "", 0.0, 0.0, "", "")
    last = evs[-1]
    now_dt = _dt(now) or _dt(_now_iso())
    last_dt = _dt(last.at)
    elapsed = (now_dt - last_dt).total_seconds() / 3600.0 if now_dt and last_dt else 0.0
    nxt = next_event(events, last.activity, as_of_ms=as_of_ms)
    # typical time in this activity = median duration of its observed transitions.
    durs = [h for frm, _to, h in _transitions(cases) if frm == last.activity]
    typical = round(statistics.median(durs), 4) if durs else 0.0
    stuck = bool(durs) and elapsed > typical           # no observed transition ⇒ terminal ⇒ not stuck
    return WhyStuck(case_ref, stuck, last.activity, round(elapsed, 4), typical,
                    nxt.expected_activity if stuck else "", last.resource if stuck else "")


# ── anomaly ─────────────────────────────────────────────────────────────────────────────────────────────
def anomaly(events: Iterable[ProcessEvent], case_ref: str, *, as_of_ms: int = 0, k: float = 2.0) -> Anomaly:
    """Duration (cycle time beyond mean + k·σ of the cohort) and sequence (a transition unseen in any other
    case) deviations for a case."""
    cases = _by_case(events, as_of_ms)
    evs = cases.get(case_ref, [])
    findings: list[tuple[str, str, str]] = []
    if not evs:
        return Anomaly(case_ref, False, ())

    # duration anomaly
    target = _cycle_hours(evs)
    others = [c for cr, e in cases.items() if cr != case_ref and (c := _cycle_hours(e)) is not None]
    if target is not None and len(others) >= 2:
        mean, sd = statistics.fmean(others), statistics.pstdev(others)
        if sd > 0:
            if target > mean + k * sd:
                z = (target - mean) / sd
                sev = "high" if z > 3 else "medium"
                findings.append(("duration", f"cycle {target:.1f}h vs cohort mean {mean:.1f}h (z={z:.1f})", sev))
        elif target > mean:
            # a perfectly consistent cohort — any excess is anomalous.
            findings.append(("duration", f"cycle {target:.1f}h vs a uniform cohort at {mean:.1f}h", "high"))

    # sequence anomaly: a transition in this case that appears in no other case
    other_pairs = {(a.activity, b.activity)
                   for cr, e in cases.items() if cr != case_ref for a, b in zip(e, e[1:])}
    for a, b in zip(evs, evs[1:]):
        if (a.activity, b.activity) not in other_pairs:
            findings.append(("sequence", f"unseen transition {a.activity} → {b.activity}", "medium"))

    return Anomaly(case_ref, bool(findings), tuple(findings))

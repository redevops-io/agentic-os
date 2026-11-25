"""Durable state of the dashboards: metric history, saved views and ingested QA results. One sqlite file (stdlib),
so the lightweight desktop and WASM installations carry no extra dependency; without a data directory it is an
in-memory database that lives as long as the process (history then starts empty after a restart, and says so).

History stores the NUMERATOR and DENOMINATOR of every point per project, not the ratio: a portfolio trend is then the
ratio of the sums over the projects the caller may see, never an average of percentages and never a sum that includes a
project the caller may not see.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

DAY = 86400.0
QA_STATUSES = ("passed", "failed", "unsupported", "blocked")       # "unexecuted" is derived, never stored
MAX_VIEWS_PER_OWNER = 50
MAX_DEMO_VIEWS = 2000           # all anonymous demo sessions together; the oldest are dropped beyond this
MAX_RESULTS_PER_BATCH = 20000
_NAME = re.compile(r"^[^\x00-\x1f]{1,80}$")

_DDL = """
CREATE TABLE IF NOT EXISTS metric_points (
  dashboard TEXT NOT NULL, metric TEXT NOT NULL, project TEXT NOT NULL, day TEXT NOT NULL,
  num REAL, den REAL, at REAL NOT NULL,
  PRIMARY KEY (dashboard, metric, project, day));
CREATE TABLE IF NOT EXISTS saved_views (
  id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, dashboard TEXT NOT NULL,
  filters TEXT NOT NULL, layout TEXT NOT NULL, is_default INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS saved_views_owner ON saved_views(owner, dashboard);
CREATE TABLE IF NOT EXISTS qa_runs (
  run_id TEXT NOT NULL, project TEXT NOT NULL, suite TEXT NOT NULL, environment TEXT NOT NULL, platform TEXT NOT NULL,
  product TEXT NOT NULL, at REAL NOT NULL, origin TEXT NOT NULL, ref TEXT NOT NULL, url TEXT NOT NULL,
  evidence_verified INTEGER, ingested_at REAL NOT NULL, ingested_by TEXT NOT NULL,
  PRIMARY KEY (project, suite, platform, run_id));
CREATE TABLE IF NOT EXISTS qa_results (
  run_id TEXT NOT NULL, project TEXT NOT NULL, suite TEXT NOT NULL, platform TEXT NOT NULL, test_id TEXT NOT NULL,
  status TEXT NOT NULL, severity TEXT NOT NULL, detail TEXT NOT NULL, requirements TEXT NOT NULL, duration_ms REAL,
  PRIMARY KEY (project, suite, platform, run_id, test_id));
CREATE INDEX IF NOT EXISTS qa_results_test ON qa_results(project, suite, platform, test_id);
"""


def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


class StoreError(ValueError):
    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail

    def as_dict(self) -> Dict[str, str]:
        return {"error": self.code, "detail": self.detail}


QA_RETENTION_ENV = "AGENTIC_OS_DASHBOARDS_QA_RETENTION_DAYS"
QA_RETENTION_DEFAULT = 400


def _retention_days() -> int:
    """How long a reported run is kept. Default 400 days (a year of history plus a comparison window); 0 keeps everything."""
    import os  # noqa: PLC0415
    raw = os.environ.get(QA_RETENTION_ENV, "").strip()
    try:
        v = int(raw) if raw else QA_RETENTION_DEFAULT
    except ValueError:
        v = QA_RETENTION_DEFAULT
    return max(0, v)


class DashboardStore:
    def __init__(self, data_dir: str = "") -> None:
        self.durable = bool(data_dir)
        if data_dir:
            d = Path(data_dir)
            d.mkdir(parents=True, exist_ok=True)
            self.path = str(d / "dashboards.sqlite")
        else:
            self.path = ":memory:"
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            if self.durable:
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_DDL)
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(qa_runs)")}
            if "release" not in cols:                          # added in 0.4.8: the release a run qualified
                self._db.execute("ALTER TABLE qa_runs ADD COLUMN release TEXT NOT NULL DEFAULT ''")
        self.qa_retention_days = _retention_days()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ── metric history ────────────────────────────────────────────────────────────────────────────
    def record_points(self, dashboard: str, points: Iterable[Tuple[str, str, Optional[float], Optional[float]]],
                      *, now: Optional[float] = None) -> None:
        """Upsert today's ``(metric, project, numerator, denominator)`` points. The last write of a day wins."""
        now = time.time() if now is None else now
        day = day_of(now)
        rows = [(dashboard, m, p, day, n, d, now) for m, p, n, d in points if n is not None]
        if not rows:
            return
        with self._lock:
            self._db.executemany(
                "INSERT INTO metric_points(dashboard,metric,project,day,num,den,at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(dashboard,metric,project,day) DO UPDATE SET num=excluded.num, den=excluded.den, at=excluded.at", rows)

    def series(self, dashboard: str, metric: str, projects: Sequence[str], *, since: float, until: float
               ) -> List[Tuple[str, Optional[float], Optional[float]]]:
        """``[(day, sum numerator, sum denominator)]`` over exactly ``projects``, oldest first."""
        if not projects:
            return []
        q = ",".join("?" for _ in projects)
        with self._lock:
            cur = self._db.execute(
                f"SELECT day, SUM(num) AS n, SUM(den) AS d, COUNT(den) AS dn FROM metric_points WHERE dashboard=? AND metric=? "
                f"AND project IN ({q}) AND day>=? AND day<=? GROUP BY day ORDER BY day",
                (dashboard, metric, *projects, day_of(since), day_of(until)))
            return [(r["day"], r["n"], r["d"] if r["dn"] else None) for r in cur.fetchall()]

    # ── saved views ───────────────────────────────────────────────────────────────────────────────
    def _view(self, r: sqlite3.Row) -> Dict[str, Any]:
        return {"id": r["id"], "name": r["name"], "dashboard": r["dashboard"], "filters": json.loads(r["filters"]),
                "layout": json.loads(r["layout"]), "is_default": bool(r["is_default"]), "created_at": r["created_at"],
                "updated_at": r["updated_at"]}

    def views(self, owner: str, dashboard: str = "") -> List[Dict[str, Any]]:
        with self._lock:
            if dashboard:
                cur = self._db.execute("SELECT * FROM saved_views WHERE owner=? AND dashboard=? ORDER BY name", (owner, dashboard))
            else:
                cur = self._db.execute("SELECT * FROM saved_views WHERE owner=? ORDER BY dashboard, name", (owner,))
            return [self._view(r) for r in cur.fetchall()]

    def save_view(self, owner: str, *, name: str, dashboard: str, filters: Mapping[str, Any], layout: Mapping[str, Any],
                  is_default: bool = False, view_id: str = "", now: Optional[float] = None) -> Dict[str, Any]:
        if not isinstance(name, str) or not _NAME.match(name.strip() or ""):
            raise StoreError(422, "bad_name", "a view needs a name of 1 to 80 characters")
        blob_f, blob_l = json.dumps(dict(filters), sort_keys=True), json.dumps(dict(layout), sort_keys=True)
        if len(blob_f) > 4000 or len(blob_l) > 16000:
            raise StoreError(413, "too_large", "the view is too large to save")
        now = time.time() if now is None else now
        with self._lock:
            if view_id:
                row = self._db.execute("SELECT * FROM saved_views WHERE id=? AND owner=?", (view_id, owner)).fetchone()
                if row is None:
                    raise StoreError(404, "not_found", "no such view")
                created = row["created_at"]
            else:
                n = self._db.execute("SELECT COUNT(*) AS n FROM saved_views WHERE owner=?", (owner,)).fetchone()["n"]
                if n >= MAX_VIEWS_PER_OWNER:
                    raise StoreError(409, "limit", f"a person can keep {MAX_VIEWS_PER_OWNER} views; delete one first")
                view_id, created = "view-" + uuid.uuid4().hex[:16], now
                if owner.startswith("demo:"):          # anonymous sandboxes are bounded as a whole, like the Today demo ledger
                    self._db.execute("DELETE FROM saved_views WHERE id IN (SELECT id FROM saved_views WHERE owner LIKE 'demo:%' "
                                     "ORDER BY updated_at DESC LIMIT -1 OFFSET ?)", (MAX_DEMO_VIEWS - 1,))
            if is_default:
                self._db.execute("UPDATE saved_views SET is_default=0 WHERE owner=? AND dashboard=?", (owner, dashboard))
            self._db.execute(
                "INSERT INTO saved_views(id,owner,name,dashboard,filters,layout,is_default,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, filters=excluded.filters, layout=excluded.layout, "
                "is_default=excluded.is_default, updated_at=excluded.updated_at",
                (view_id, owner, name.strip(), dashboard, blob_f, blob_l, 1 if is_default else 0, created, now))
            return self._view(self._db.execute("SELECT * FROM saved_views WHERE id=?", (view_id,)).fetchone())

    def delete_view(self, owner: str, view_id: str) -> bool:
        with self._lock:
            return self._db.execute("DELETE FROM saved_views WHERE id=? AND owner=?", (view_id, owner)).rowcount > 0

    # ── ingested QA results (CI lanes that do not run through a registered QA product) ───────────────────────
    def ingest_qa(self, batch: Mapping[str, Any], *, by: str, now: Optional[float] = None) -> Dict[str, Any]:
        now = time.time() if now is None else now

        def s(key: str, limit: int = 120, required: bool = True, default: str = "") -> str:
            v = batch.get(key, default)
            if v in (None, "") and not required:
                return default
            if not isinstance(v, str) or not v.strip() or len(v) > limit or any(ord(c) < 32 for c in v):
                raise StoreError(422, "invalid", f"{key} must be a non-empty string of at most {limit} characters")
            return v.strip()
        project, suite, run_id = s("project"), s("suite"), s("run_id", 200)
        platform = s("platform", required=False, default="default")
        environment = s("environment", required=False, default="")
        product = s("product", required=False, default="")
        origin = s("origin", 60, required=False, default="ci")
        ref, url = s("ref", 300, required=False, default=""), s("url", 500, required=False, default="")
        release = s("release", 80, required=False, default="")
        if url and not re.match(r"^https?://[^\s]+$", url):
            raise StoreError(422, "invalid", "url must be an http(s) link")
        at = batch.get("at", now)
        if isinstance(at, bool) or not isinstance(at, (int, float)) or not (0 < float(at) <= now + DAY):
            raise StoreError(422, "invalid", "at must be an epoch time, not in the future")
        ev = batch.get("evidence_verified")
        if ev is not None and not isinstance(ev, bool):
            raise StoreError(422, "invalid", "evidence_verified must be true, false or absent")
        results = batch.get("results")
        if not isinstance(results, list) or not results:
            raise StoreError(422, "invalid", "results must be a non-empty list")
        if len(results) > MAX_RESULTS_PER_BATCH:
            raise StoreError(413, "too_large", f"a batch is limited to {MAX_RESULTS_PER_BATCH} results")
        rows, seen = [], set()
        for r in results:
            if not isinstance(r, Mapping):
                raise StoreError(422, "invalid", "each result must be an object")
            tid, status = r.get("test_id"), r.get("status")
            if not isinstance(tid, str) or not tid or len(tid) > 400:
                raise StoreError(422, "invalid", "each result needs a test_id")
            if status not in QA_STATUSES:
                raise StoreError(422, "invalid", f"status must be one of {', '.join(QA_STATUSES)} (an unexecuted test is simply absent)")
            if tid in seen:
                raise StoreError(422, "invalid", f"test {tid} appears twice in the batch")
            seen.add(tid)
            reqs = r.get("requirements") or []
            if not isinstance(reqs, list) or not all(isinstance(x, str) and 0 < len(x) <= 80 for x in reqs):
                raise StoreError(422, "invalid", "requirements must be a list of ids")
            dur = r.get("duration_ms")
            rows.append((run_id, project, suite, platform, tid, status, str(r.get("severity") or "")[:20],
                         str(r.get("detail") or "")[:500], json.dumps(reqs), float(dur) if isinstance(dur, (int, float)) and not isinstance(dur, bool) else None))
        with self._lock:
            self._db.execute("BEGIN")
            try:
                # One run is one row: the same (project, suite, platform, run_id) reported again REPLACES it (a retried CI step, a
                # re-run of the reporter), so a run is never counted twice. The answer says which happened.
                replaced = self._db.execute("SELECT 1 FROM qa_runs WHERE project=? AND suite=? AND platform=? AND run_id=?",
                                            (project, suite, platform, run_id)).fetchone() is not None
                self._db.execute("DELETE FROM qa_results WHERE project=? AND suite=? AND platform=? AND run_id=?", (project, suite, platform, run_id))
                self._db.execute(
                    "INSERT OR REPLACE INTO qa_runs(run_id,project,suite,environment,platform,product,at,origin,ref,url,evidence_verified,ingested_at,ingested_by,release) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, project, suite, environment, platform, product, float(at), origin, ref, url,
                     None if ev is None else int(ev), now, by, release))
                self._db.executemany("INSERT INTO qa_results VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
                pruned = self._prune_qa(now)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return {"run_id": run_id, "project": project, "suite": suite, "platform": platform, "results": len(rows),
                "duplicate": replaced, "pruned_runs": pruned}

    def _prune_qa(self, now: float) -> int:
        """Retention: drop runs older than the window, EXCEPT the latest run of each (project, suite, platform) — the current
        state of a lane that has gone quiet stays visible (and counts as stale), it does not silently disappear."""
        if not self.qa_retention_days:
            return 0
        cut = now - self.qa_retention_days * DAY
        old = self._db.execute(
            "SELECT r.project, r.suite, r.platform, r.run_id FROM qa_runs r WHERE r.at < ? AND r.at < "
            "(SELECT MAX(x.at) FROM qa_runs x WHERE x.project=r.project AND x.suite=r.suite AND x.platform=r.platform)", (cut,)).fetchall()
        for row in old:
            self._db.execute("DELETE FROM qa_results WHERE project=? AND suite=? AND platform=? AND run_id=?", tuple(row))
            self._db.execute("DELETE FROM qa_runs WHERE project=? AND suite=? AND platform=? AND run_id=?", tuple(row))
        return len(old)

    def qa_runs(self, *, since: float = 0.0) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute("SELECT * FROM qa_runs WHERE at>=? ORDER BY at", (since,))
            return [dict(r) for r in cur.fetchall()]

    def qa_results(self, run_keys: Sequence[Tuple[str, str, str, str]]) -> Dict[Tuple[str, str, str, str], List[Dict[str, Any]]]:
        out: Dict[Tuple[str, str, str, str], List[Dict[str, Any]]] = {}
        with self._lock:
            for project, suite, platform, run_id in run_keys:
                cur = self._db.execute("SELECT * FROM qa_results WHERE project=? AND suite=? AND platform=? AND run_id=?",
                                       (project, suite, platform, run_id))
                out[(project, suite, platform, run_id)] = [{**dict(r), "requirements": json.loads(r["requirements"])} for r in cur.fetchall()]
        return out


    def qa_run_counts(self, *, since: float = 0.0) -> Dict[Tuple[str, str, str, str], Dict[str, int]]:
        """Results per status for every run since ``since``, counted by the database: what a history chart needs of an old run,
        without loading its thousands of results."""
        out: Dict[Tuple[str, str, str, str], Dict[str, int]] = {}
        with self._lock:
            cur = self._db.execute(
                "SELECT x.project, x.suite, x.platform, x.run_id, x.status, COUNT(*) FROM qa_results x JOIN qa_runs r "
                "ON r.project=x.project AND r.suite=x.suite AND r.platform=x.platform AND r.run_id=x.run_id "
                "WHERE r.at>=? GROUP BY x.project, x.suite, x.platform, x.run_id, x.status", (since,))
            for project, suite, platform, run_id, status, n in cur.fetchall():
                out.setdefault((project, suite, platform, run_id), {})[status] = int(n)
        return out


def open_store(data_dir: str = "") -> DashboardStore:
    d = data_dir or os.environ.get("AGENTIC_OS_DASHBOARDS_DIR", "").strip()
    if not d:
        base = os.environ.get("AOS_DATA_DIR", "").strip()
        d = str(Path(base) / "dashboards") if base else ""
    return DashboardStore(d)


__all__ = ["DAY", "QA_STATUSES", "DashboardStore", "StoreError", "open_store", "day_of"]

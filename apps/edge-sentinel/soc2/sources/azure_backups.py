"""AVL-BCK-01 evidence from Azure Database for PostgreSQL (Flexible Server): completed backups, and the restore drills the people who run them record.

What the service shows (read from a real server with `az postgres flexible-server backup list`, 2026-10-24): a list of backups with `backupType`, `completedTime`, `name` and `source` ("Automatic"). Only COMPLETED
backups appear: a backup that failed is not listed, and the list says nothing about restores. Consequences:

  * A window with no completed backup listed is a GAP (collection fails), never a finding and never a pass: the listing can omit a backup (retention), and an absent record cannot tell a failure from an omission.
  * Every backup that IS listed is recorded as `ok: true`, because that is all the service says. The payload does not claim a failed backup would have been seen; a failed backup that Azure never lists is invisible to
    this evidence (see the limits in SOC2_READINESS.md).
  * A successful backup log is not evidence that a restore works, so the control also needs a restore drill in the period. Nothing here performs one (a point-in-time restore creates and bills a second server): a
    person or a job runs the drill and writes `RestoreDrillLog.record(...)`, and the collector reports the drills that fall in each window. With no drill in the period the assessor reports a FAIL, which is the honest state.

The transport is the `az` CLI limited to the one read-only command it needs; credentials are the operator's own `az login`, never read or stored here."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from ..pipeline import Collected, CollectionFailed

Runner = Callable[[list], "tuple[int, str, str]"]          # (az arguments) -> (returncode, stdout, stderr)


def default_runner(timeout: float = 60.0) -> Runner:
    """Runs `az` for exactly one read-only command and refuses anything else."""
    allowed = ("postgres", "flexible-server", "backup", "list")

    def run(args: list) -> tuple:
        if tuple(args[:4]) != allowed:
            raise PermissionError(f"the Azure runner refuses {args[:4]}: only `postgres flexible-server backup list` is a read path")
        r = subprocess.run(["az", *args, "-o", "json"], capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    return run


def _epoch(ts: Any) -> Optional[float]:
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
        except ValueError:
            return None
    return None


class RestoreDrillLog:
    """A durable, append-only record of restore drills, written by whoever performs them: `record(ok=True, target="restored-copy-1", note="point-in-time to 03:00, row counts matched")`."""

    def __init__(self, path: str, *, clock: Callable[[], float] = time.time) -> None:
        self.path, self.clock = path, clock
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS drills (at REAL NOT NULL, ok INTEGER NOT NULL, target TEXT NOT NULL, note TEXT NOT NULL, by TEXT NOT NULL)")
            for op in ("UPDATE", "DELETE"):
                c.execute(f"CREATE TRIGGER IF NOT EXISTS drills_no_{op.lower()} BEFORE {op} ON drills BEGIN SELECT RAISE(ABORT, 'the drill log is append-only'); END")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30, isolation_level=None)

    def record(self, *, ok: bool, target: str, note: str, by: str, at: Optional[float] = None) -> None:
        if not by.strip() or not target.strip() or len(note.strip()) < 10:
            raise ValueError("a drill needs who ran it, what it restored to, and a note of what was checked")
        with self._conn() as c:
            c.execute("INSERT INTO drills (at, ok, target, note, by) VALUES (?,?,?,?,?)", (self.clock() if at is None else float(at), 1 if ok else 0, target.strip(), note.strip(), by.strip()))

    def between(self, lo: float, hi: float) -> list:
        with self._conn() as c:
            return [{"at": r[0], "ok": bool(r[1]), "target": r[2]} for r in c.execute("SELECT at, ok, target FROM drills WHERE at >= ? AND at < ? ORDER BY at", (lo, hi))]


class AzurePostgresBackups:
    kind = "backup_restore"
    source = "azure database for postgresql flexible server: backup list and the restore-drill log"

    def __init__(self, resource_group: str, server: str, runner: Runner, drills: RestoreDrillLog, *, subscription: str = "", name: Optional[str] = None) -> None:
        self.resource_group, self.server, self.runner, self.drills, self.subscription = resource_group, server, runner, drills, subscription
        self.name = name or f"azure-pg-backups:{server}"

    def collect(self, window_from: float, window_to: float) -> Collected:
        args = ["postgres", "flexible-server", "backup", "list", "--resource-group", self.resource_group, "--server-name", self.server]
        if self.subscription:
            args += ["--subscription", self.subscription]
        try:
            rc, out, err = self.runner(args)
        except Exception as e:  # noqa: BLE001
            raise CollectionFailed(f"az could not be run ({type(e).__name__})") from None
        if rc != 0:
            raise CollectionFailed("az postgres flexible-server backup list failed: " + " ".join(err.split())[:160])
        try:
            rows = json.loads(out)
        except ValueError:
            raise CollectionFailed("az returned something that is not JSON") from None
        if not isinstance(rows, list):
            raise CollectionFailed("az returned an unexpected shape for the backup list")
        inside = []
        for r in rows:
            at = _epoch(r.get("completedTime")) if isinstance(r, dict) else None
            if at is not None and window_from <= at < window_to:
                inside.append({"id": str(r.get("name") or r.get("id") or ""), "ok": True, "at": at, "type": str(r.get("backupType", "")), "source": str(r.get("source", ""))})
        if not inside:
            raise CollectionFailed("no completed backup is listed for this window: that is not evidence that none ran (the list omits older backups and shows no failures)")
        return Collected({"backups": inside, "restore_tests": self.drills.between(window_from, window_to), "population_complete": True,
                          "server": self.server, "note": "only completed backups are visible; a failed backup would not appear in this list"})

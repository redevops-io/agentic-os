"""AVL-BCK-01 evidence from Azure PostgreSQL: a window with no listed backup is a gap (never a finding, never a pass), listed backups are recorded as the service shows them, the restore drills people record are
reported by window, nothing but the one read-only `az` command can run, and the stored evidence assesses as PASS / FAIL / INCONCLUSIVE for the right reasons."""
from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone

import pytest

A = importlib.import_module("edge-sentinel.soc2.assess")
P = importlib.import_module("edge-sentinel.soc2.pipeline")
AZ = importlib.import_module("edge-sentinel.soc2.sources.azure_backups")
kit = importlib.import_module("edge-sentinel.soc2._testkit")
NOW, DAY = kit.NOW, kit.DAY
AHI = (NOW // DAY) * DAY
APERIOD = (AHI - 30 * DAY, AHI)


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def listing(*times, source="Automatic", typ="Full"):
    """The shape `az postgres flexible-server backup list` really returns (checked against a live server)."""
    return json.dumps([{"backupType": typ, "completedTime": iso(t), "id": f"/subscriptions/s/resourceGroups/rg/providers/Microsoft.DBforPostgreSQL/flexibleServers/pg/backups/backup_{int(t)}",
                        "name": f"backup_{int(t)}", "resourceGroup": "rg", "source": source, "systemData": None, "type": "Microsoft.DBforPostgreSQL/flexibleServers/backups"} for t in times])


def runner(out="[]", rc=0, err="", seen=None):
    def run(args):
        if seen is not None:
            seen.append(list(args))
        return rc, out, err
    return run


def drills(tmp_path, *entries):
    d = AZ.RestoreDrillLog(str(tmp_path / "drills.db"), clock=kit.Clock())
    for at, ok in entries:
        d.record(ok=ok, target="restored-copy", note="restored to a scratch server, row counts matched", by="ops@acme", at=at)
    return d


def collector(tmp_path, out, *drill_entries, **kw):
    return AZ.AzurePostgresBackups("rg", "pg", runner(out, **kw), drills(tmp_path, *drill_entries))


W0, W1 = AHI - 3 * DAY, AHI - 2 * DAY


# ───────────────────────── the collector ─────────────────────────
def test_a_listed_backup_inside_the_window_is_recorded_as_the_service_shows_it(tmp_path):
    c = collector(tmp_path, listing(W0 + 3600)).collect(W0, W1)
    assert c.payload["backups"] == [{"id": f"backup_{int(W0 + 3600)}", "ok": True, "at": W0 + 3600, "type": "Full", "source": "Automatic"}]
    assert c.payload["population_complete"] is True and "failed backup would not appear" in c.payload["note"]


def test_backups_outside_the_window_are_not_in_it_and_the_window_edges_are_half_open(tmp_path):
    out = listing(W0 - 1, W0, W1 - 1, W1)
    got = [b["at"] for b in collector(tmp_path, out).collect(W0, W1).payload["backups"]]
    assert got == [W0, W1 - 1]


def test_a_window_with_no_listed_backup_is_a_gap_not_a_finding_and_not_a_pass(tmp_path):
    for out in (listing(W0 - DAY), "[]"):
        with pytest.raises(P.CollectionFailed) as e:
            collector(tmp_path, out).collect(W0, W1)
        assert "not evidence that none ran" in str(e.value)


@pytest.mark.parametrize("kw,why", [({"rc": 1, "err": "ERROR: The subscription is not logged in"}, "failed"), ({"out": "not json"}, "not JSON"), ({"out": json.dumps({"value": []})}, "unexpected shape")])
def test_a_failed_or_unreadable_az_call_is_a_gap(tmp_path, kw, why):
    kw = {"out": "[]", **kw}
    with pytest.raises(P.CollectionFailed) as e:
        collector(tmp_path, kw.pop("out"), **kw).collect(W0, W1)
    assert why in str(e.value)


def test_a_runner_that_cannot_run_az_is_a_gap_and_the_error_text_is_not_leaked(tmp_path):
    def boom(args):
        raise FileNotFoundError("az: /usr/local/bin/az with token ey123456789012345678")
    with pytest.raises(P.CollectionFailed) as e:
        AZ.AzurePostgresBackups("rg", "pg", boom, drills(tmp_path)).collect(W0, W1)
    assert "ey123456789012345678" not in str(e.value) and "FileNotFoundError" in str(e.value)


def test_the_command_is_the_one_read_only_listing_with_the_group_server_and_optional_subscription(tmp_path):
    seen: list = []
    AZ.AzurePostgresBackups("rg", "pg", runner(listing(W0 + 5), seen=seen), drills(tmp_path), subscription="sub-1").collect(W0, W1)
    assert seen == [["postgres", "flexible-server", "backup", "list", "--resource-group", "rg", "--server-name", "pg", "--subscription", "sub-1"]]


def test_the_default_runner_refuses_every_az_command_but_the_listing():
    run = AZ.default_runner()
    for bad in (["postgres", "flexible-server", "delete"], ["postgres", "flexible-server", "restore"], ["group", "delete"], ["postgres", "flexible-server", "backup", "create"], []):
        with pytest.raises(PermissionError):
            run(bad)


def test_the_default_runner_runs_az_for_the_listing_and_asks_for_json(monkeypatch):
    import subprocess as sp
    seen = {}

    def fake(cmd, **kw):
        seen["cmd"] = cmd
        return sp.CompletedProcess(cmd, 0, stdout="[]", stderr="")
    monkeypatch.setattr(AZ.subprocess, "run", fake)
    rc, out, err = AZ.default_runner()(["postgres", "flexible-server", "backup", "list", "--resource-group", "rg", "--server-name", "pg"])
    assert (rc, out) == (0, "[]") and seen["cmd"][0] == "az" and seen["cmd"][-2:] == ["-o", "json"] and "list" in seen["cmd"]


def test_a_row_with_a_missing_or_unreadable_completion_time_is_ignored_not_a_crash(tmp_path):
    rows = json.loads(listing(W0 + 60))
    rows += [{"name": "no-time", "backupType": "Full"}, {"name": "bad-time", "completedTime": "yesterday", "backupType": "Full"}, {"name": "null-time", "completedTime": None}, "not-a-dict"]
    got = collector(tmp_path, json.dumps(rows)).collect(W0, W1).payload["backups"]
    assert [b["id"] for b in got] == [f"backup_{int(W0 + 60)}"]


# ───────────────────────── the drill log ─────────────────────────
def test_drills_are_reported_by_window_with_their_outcome(tmp_path):
    c = collector(tmp_path, listing(W0 + 5), (W0 + 100, True), (W1 + 100, True), (W0 + 200, False)).collect(W0, W1)
    assert [(d["at"], d["ok"]) for d in c.payload["restore_tests"]] == [(W0 + 100, True), (W0 + 200, False)]


def test_the_drill_window_is_half_open_like_the_backup_window(tmp_path):
    c = collector(tmp_path, listing(W0 + 5), (W0, True), (W1, True), (W1 - 1, False)).collect(W0, W1)
    assert [d["at"] for d in c.payload["restore_tests"]] == [W0, W1 - 1]


def test_a_drill_needs_who_what_and_a_note_and_the_log_is_append_only(tmp_path):
    d = AZ.RestoreDrillLog(str(tmp_path / "d.db"), clock=kit.Clock())
    for bad in (dict(by="", target="t", note="a long enough note"), dict(by="a", target="", note="a long enough note"), dict(by="a", target="t", note="short")):
        with pytest.raises(ValueError):
            d.record(ok=True, **bad)
    d.record(ok=True, by="a", target="t", note="restored and verified row counts")
    import sqlite3
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(str(tmp_path / "d.db")).execute("UPDATE drills SET ok = 0")
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(str(tmp_path / "d.db")).execute("DELETE FROM drills")


# ───────────────────────── through the pipeline and the same assessor ─────────────────────────
def every_day(skip=()):
    return [AHI - (30 - i) * DAY + 7200 for i in range(30) if i not in skip]


def assess(tmp_path, name, out, *entries):
    d = tmp_path / name
    d.mkdir()
    v = kit.make_vault(d)
    p = P.Pipeline(v, lambda k: kit.SECRET_A, clock=kit.Clock())
    j = P.Job(AZ.AzurePostgresBackups("rg", "pg", runner(out), drills(d, *entries)), "acme", "prod", "collector-a", "key-a", window_s=DAY)
    rep = p.run([j], APERIOD[0], APERIOD[1])
    return rep, A.Assessor(v, clock=kit.Clock()).assess("AVL-BCK-01", "acme", "prod", *APERIOD)


def test_a_backup_every_day_and_a_successful_drill_in_the_period_assesses_as_a_pass(tmp_path):
    rep, a = assess(tmp_path, "pass", listing(*every_day()), (AHI - 10 * DAY, True))
    assert not rep.gaps() and a.result == A.PASS and a.coverage >= 0.99


def test_a_backup_log_with_no_restore_drill_is_a_fail_not_a_pass(tmp_path):
    rep, a = assess(tmp_path, "nodrill", listing(*every_day()))
    assert not rep.gaps() and a.result == A.FAIL and "no restore test" in a.failures[0]


def test_only_failed_drills_is_a_fail(tmp_path):
    _, a = assess(tmp_path, "faileddrill", listing(*every_day()), (AHI - 10 * DAY, False))
    assert a.result == A.FAIL and "every restore test in the period failed" in a.failures[0]


def test_a_missing_day_is_a_gap_and_enough_missing_days_make_the_period_inconclusive(tmp_path):
    rep, a = assess(tmp_path, "gap", listing(*every_day(skip=range(5, 12))), (AHI - 10 * DAY, True))
    assert len(rep.gaps()) == 7 and a.result == A.INCONCLUSIVE and a.coverage < 0.95


def test_one_missing_day_stays_inside_the_coverage_the_control_requires(tmp_path):
    rep, a = assess(tmp_path, "oneday", listing(*every_day(skip=[7])), (AHI - 10 * DAY, True))
    assert len(rep.gaps()) == 1 and a.result == A.PASS and a.coverage >= 0.95

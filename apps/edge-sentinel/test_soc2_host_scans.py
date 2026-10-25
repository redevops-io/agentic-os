"""SEC-CFG-01 evidence from retained host scans: each successful scan becomes a `scanner_result` for a host; a failed scan, a missing report, a window with no scan and a report whose hash no longer matches are all refused (never a clean result);
the worst scan of a window is what the assessor sees; and the stored evidence assesses as PASS / FAIL / INCONCLUSIVE for the right reasons."""
from __future__ import annotations

import hashlib
import importlib
import json
from datetime import datetime, timezone

import pytest

A = importlib.import_module("edge-sentinel.soc2.assess")
P = importlib.import_module("edge-sentinel.soc2.pipeline")
H = importlib.import_module("edge-sentinel.soc2.sources.host_scans")
kit = importlib.import_module("edge-sentinel.soc2._testkit")
DAY = kit.DAY
WEEK = 7 * DAY
W0 = (kit.NOW // WEEK) * WEEK - 8 * WEEK                  # aligned to the seven-day windows the weekly scans use
PERIOD = (W0, W0 + 4 * WEEK)


def iso(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


def write_scan(root, day_name, at, *, kube_crit=0, trivy_crit=0, kube_ok=True, trivy_ok=True, node="aks-n1", tamper=None):
    d = root / day_name
    d.mkdir(parents=True, exist_ok=True)
    scans = {}
    for tool, ok, crit in (("kube-bench", kube_ok, kube_crit), ("trivy", trivy_ok, trivy_crit)):
        raw = d / f"{node}-{tool}.json"
        raw.write_text(json.dumps({"tool": tool, "ok": ok, "crit": crit}))
        e = {"ok": ok, "raw": raw.name, "sha256": hashlib.sha256(raw.read_bytes()).hexdigest(), "image": f"registry/{tool}@sha256:" + "a" * 64, "mapping": f"{tool} mapping"}
        if ok:
            e.update(critical_open=crit, warn_manual=7)
        scans[tool] = e
    (d / "summary.json").write_text(json.dumps({"env": "dev", "nodes": [{"node": node, "os_image": "Ubuntu 24.04.5 LTS", "kernel": "6.8.0-1070-azure", "scanned_at": iso(at), "scans": scans}]}))
    if tamper:
        (d / f"{node}-{tamper}.json").write_text("{}")


def collector(root):
    return H.RetainedHostScans(str(root))


# ───────────────────────── the collector ─────────────────────────
def test_a_clean_scan_in_the_window_is_a_host_scan_with_no_critical_finding(tmp_path):
    write_scan(tmp_path, "d1", W0 + 100)
    p = collector(tmp_path).collect(W0, W0 + WEEK).payload
    assert p["target_type"] == "host" and p["critical_open"] == 0 and p["target"] == "aks-n1" and p["population_complete"] is True and p["scan_count"] == 2
    assert {s["scanner"] for s in p["scans"]} == {"kube-bench", "trivy"} and all(s["tool_image"].endswith("a" * 64) for s in p["scans"])


def test_the_worst_scan_of_the_window_is_what_the_assessor_sees(tmp_path):
    write_scan(tmp_path, "d1", W0 + 100, kube_crit=0, trivy_crit=21)
    p = collector(tmp_path).collect(W0, W0 + WEEK).payload
    assert p["critical_open"] == 21 and p["scanner"] == "trivy" and [s["critical_open"] for s in p["scans"]] == [0, 21]


def test_the_window_is_half_open(tmp_path):
    write_scan(tmp_path, "d1", W0)
    write_scan(tmp_path, "d2", W0 + WEEK)
    assert collector(tmp_path).collect(W0, W0 + WEEK).payload["scan_count"] == 2
    with pytest.raises(P.CollectionFailed):
        collector(tmp_path).collect(W0 - WEEK, W0)


def test_a_window_with_no_scan_is_a_gap(tmp_path):
    write_scan(tmp_path, "d1", W0 + 3 * WEEK)
    with pytest.raises(P.CollectionFailed, match="no retained host scan"):
        collector(tmp_path).collect(W0, W0 + WEEK)


def test_a_scan_that_did_not_complete_is_a_gap_not_zero_findings(tmp_path):
    write_scan(tmp_path, "d1", W0 + 5, trivy_ok=False)
    with pytest.raises(P.CollectionFailed, match="not a clean scan"):
        collector(tmp_path).collect(W0, W0 + WEEK)


def test_a_raw_report_changed_after_it_was_kept_is_refused(tmp_path):
    write_scan(tmp_path, "d1", W0 + 5, tamper="trivy")
    with pytest.raises(P.CollectionFailed, match="does not match its recorded hash"):
        collector(tmp_path).collect(W0, W0 + WEEK)


def test_a_missing_raw_report_and_an_unreadable_summary_are_refused(tmp_path):
    write_scan(tmp_path, "d1", W0 + 5)
    (tmp_path / "d1" / "aks-n1-kube-bench.json").unlink()
    with pytest.raises(P.CollectionFailed, match="missing"):
        collector(tmp_path).collect(W0, W0 + WEEK)
    (tmp_path / "d1" / "summary.json").write_text("not json")
    with pytest.raises(P.CollectionFailed, match="unreadable"):
        collector(tmp_path).collect(W0, W0 + WEEK)
    (tmp_path / "d1" / "summary.json").write_text(json.dumps({"nodes": "x"}))
    with pytest.raises(P.CollectionFailed, match="unexpected shape"):
        collector(tmp_path).collect(W0, W0 + WEEK)


def test_scans_outside_the_window_are_not_checked_so_an_old_edited_report_does_not_block_a_new_window(tmp_path):
    write_scan(tmp_path, "old", W0 - 10 * WEEK, tamper="trivy")
    write_scan(tmp_path, "new", W0 + 5)
    assert collector(tmp_path).collect(W0, W0 + WEEK).payload["critical_open"] == 0


# ───────────────────────── through the pipeline and the same assessor ─────────────────────────
def assess(tmp_path, name, weeks):
    d = tmp_path / name
    d.mkdir()
    root = d / "scans"
    for i, kw in weeks.items():
        write_scan(root, f"w{i}", W0 + i * WEEK + 3600, **kw)
    v = kit.make_vault(d)
    p = P.Pipeline(v, lambda k: kit.SECRET_A, clock=kit.Clock())
    rep = p.run([P.Job(collector(root), "acme", "prod", "collector-a", "key-a", window_s=WEEK)], *PERIOD)
    return rep, A.Assessor(v, clock=kit.Clock()).assess("SEC-CFG-01", "acme", "prod", *PERIOD)


def test_weekly_clean_scans_for_the_period_assess_as_a_pass(tmp_path):
    rep, a = assess(tmp_path, "ok", {i: {} for i in range(4)})
    assert not rep.gaps() and a.result == A.PASS


def test_a_critical_finding_in_any_week_fails_the_control(tmp_path):
    _, a = assess(tmp_path, "crit", {0: {}, 1: {"trivy_crit": 21}, 2: {}, 3: {}})
    assert a.result == A.FAIL and "21 critical finding(s) open" in a.failures[0]


def test_a_week_whose_scan_did_not_complete_is_a_gap_and_the_period_is_inconclusive(tmp_path):
    rep, a = assess(tmp_path, "gap", {0: {}, 1: {"trivy_ok": False}, 2: {}, 3: {}})
    assert len(rep.gaps()) == 1 and a.result == A.INCONCLUSIVE

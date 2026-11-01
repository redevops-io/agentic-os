"""The Edge Sentinel UI must not present a quiet reading, sample data, a simulation or a dry run as a live security
finding, and must not state a capability the app does not have.

These tests fail on the old wording ("All systems normal", "agent active", "data: live from CrowdSec", the
"Scan the billing app" chip, "SIEM", "threat-intel / EDR", "firewall", "tighten", "DRY-RUN" without saying nothing was
blocked) and pass on the current wording. They also check that every element backed by a sample, simulated or dry-run
source carries a visible label. API and JSON field names are unchanged (two additive fields: `read_at`, `data_status`).
"""
from __future__ import annotations

import ast
import importlib
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

core = importlib.import_module("edge-sentinel.core")
netbird = importlib.import_module("edge-sentinel.netbird")
portabase = importlib.import_module("edge-sentinel.portabase")
permissions_ui = importlib.import_module("edge-sentinel.permissions_ui")
live = importlib.import_module("edge-sentinel.live")
seed = importlib.import_module("edge-sentinel.seed")

HERE = pathlib.Path(__file__).resolve().parent

# wording that must never come back in anything a visitor can read in the UI or the chat
FORBIDDEN_UI = [
    "All systems normal", "agent active", "data: live from CrowdSec", "Scan the billing app",
    "Scan a running deployment", "scan any app", "SIEM", "firewall", "learns the cheapest", "Threats blocked",
    "tightened policy", "DRY-RUN",
]
FORBIDDEN_RE = [r"\bEDR\b", r"threat-intel", r"enforcing \d+ block"]

QUIET = {
    "tenant": "T", "core": "crowdsec", "connected": True, "front_url": "http://x", "has_threat": False,
    "read_at": "2026-11-01 12:00:00 UTC", "kpis": [], "decisions": [], "alerts": [], "top_scenarios": [], "bars": [],
    "counts": {"decisions": 0, "alerts": 0, "sources": 0}, "alerts_status": {"ok": True, "note": ""},
}


def _data(**kw):
    d = dict(QUIET)
    d.update(kw)
    return d


@pytest.fixture()
def app(load_app, monkeypatch):
    mod = load_app("edge-sentinel")
    monkeypatch.setattr(mod, "fetch_activity", lambda force=False: _data())
    monkeypatch.setattr(mod.live, "sync_cases", lambda *a, **k: [])
    return mod


def _assert_clean(text: str, where: str):
    for bad in FORBIDDEN_UI:
        assert bad.lower() not in text.lower(), f"{where} still contains {bad!r}"
    for rx in FORBIDDEN_RE:
        assert not re.search(rx, text), f"{where} still matches {rx!r}"


# ── a quiet or empty reading is a reading, not "all clear" ─────────────────────────────────────────────────────────
def test_quiet_reading_says_what_was_read_and_when_not_all_clear(app):
    html = app.render(_data())
    assert "All systems normal" not in html and "agent active" not in html
    assert "0 active decisions and 0 alerts" in html
    assert "2026-11-01 12:00:00 UTC" in html                       # when it was read
    assert "not an assessment of the environment" in html
    assert "read from CrowdSec at 2026-11-01 12:00:00 UTC" in html   # the header badge, not a static "live"


def test_alert_feed_down_is_never_shown_as_a_quiet_reading(app):
    html = app.render(_data(alerts_status={"ok": False, "note": "LAPI machine credentials not configured"}))
    assert "ALERT FEED UNAVAILABLE" in html and "not a reading" in html
    assert "Quiet reading" not in html


def test_unreachable_core_has_no_reading_label(app):
    html = app.render(_data(connected=False))
    assert "UNREACHABLE" in html and "no reading: CrowdSec unreachable" in html
    assert "read from CrowdSec" not in html


def test_active_decisions_are_not_described_as_enforced_by_the_agent(app):
    dec = {"value": "203.0.113.9", "scope": "Ip", "scenario": "crowdsecurity/ssh-bf", "scenario_full": "x", "type": "BAN",
           "duration": "3h", "origin": "cscli", "severity": "critical"}
    html = app.render(_data(has_threat=True, decisions=[dec]))
    assert "enforcing" not in html and "ACTIVE DECISIONS" in html
    assert "manual (cscli)" in html                                  # hand-added entries are tagged
    assert "sample history" in html


def test_engine_detections_are_not_tagged_manual(app):
    dec = {"value": "203.0.113.9", "scope": "Ip", "scenario": "s", "scenario_full": "s", "type": "BAN", "duration": "3h",
           "origin": "crowdsec", "severity": "low"}
    assert "<span class='pill pill--neutral' title=" not in app._decisions_table(_data(decisions=[dec]))


def test_kpis_do_not_claim_enforcement():
    src = (HERE / "core.py").read_text()
    assert '"Threats blocked"' not in src and "active decisions enforced" not in src and "unique blocked sources" not in src
    assert '"Active decisions"' in src


def test_alert_origin_is_read_from_the_attached_decision():
    assert core._alert_origin({"decisions": [{"origin": "cscli"}]}) == "cscli"
    assert core._alert_origin({"decisions": []}) == "" and core._alert_origin({}) == ""


# ── chat console: chips and subtitle, and the capability claims in the tool text ───────────────────────────────────
def test_list_bans_empty_answer_states_the_reading_not_that_nobody_is_banned(app, monkeypatch):
    monkeypatch.setattr(app, "fetch_activity", lambda force=False: _data())
    text = app._t_list_bans({})["text"]
    assert "nobody is currently banned" not in text and "2026-11-01 12:00:00 UTC" in text


def test_wazuh_answer_reports_agent_counts_only(app, monkeypatch):
    monkeypatch.setattr(app, "_wazuh_get", lambda p: {})
    assert "SIEM" not in app._t_wazuh_status({})["text"]
    monkeypatch.setattr(app, "_wazuh_get", lambda p: {"data": {"connection": {"active": 2, "disconnected": 0, "total": 2}}})
    text = app._t_wazuh_status({})["text"]
    assert "SIEM" not in text and "agent counts only" in text and "correlates" not in text


def test_threat_summary_says_read_time_and_decisions_not_enforced_blocks(app, monkeypatch):
    d = _data(kpis=[{"label": "Active decisions", "value": "0"}, {"label": "Alerts (24h)", "value": "0"}])
    monkeypatch.setattr(app, "fetch_activity", lambda force=False: d)
    text = app._t_threat_summary({})["text"]
    assert "active decision(s) held in CrowdSec" in text and "2026-11-01 12:00:00 UTC" in text and "enforced" not in text


def test_home_page_without_console_is_clean(app, monkeypatch):
    monkeypatch.setattr(app, "_SENTINEL_CONSOLE", None)
    html = TestClient(app.app).get("/").text
    _assert_clean(html, "GET /")
    assert "does not run in this app" in html                        # the benchmark sentence says so
    assert "Network access (sample data)" in html and "Backups (sample data)" in html


# ── everything backed by a sample/simulated source carries a visible label ────────────────────────────────────────
@pytest.fixture()
def sample_mode(monkeypatch):
    monkeypatch.setattr(netbird, "NETBIRD_API_TOKEN", "")
    monkeypatch.setattr(portabase, "PORTABASE_API_TOKEN", "")


def test_network_panel_every_card_and_the_json_say_sample(app, sample_mode):
    html = netbird.panel()
    assert html.count("SAMPLE DATA") >= 4                            # subtitle + peers + policies + risks cards
    assert "nothing on this page was read from a NetBird" in html
    assert netbird.access_review({})["data"]["data_status"] == "sample" and netbird.access_review({})["data"]["live"] is False
    assert "SAMPLE DATA" in netbird.access_review({})["text"] and "SAMPLE DATA" in netbird.posture({})["text"]


def test_backup_panel_every_card_and_the_json_say_sample(app, sample_mode):
    html = portabase.panel()
    assert html.count("SAMPLE DATA") >= 3                            # subtitle + databases + posture cards
    assert portabase.backup_status({})["data"]["data_status"] == "sample"
    assert "SAMPLE DATA" in portabase.backup_status({})["text"]


def test_sample_badge_text_is_visible_not_just_a_colour(app):
    assert "SAMPLE DATA" in app._live_badge(False, "set X")
    assert "SAMPLE" not in app._live_badge(True, "set X")


def test_network_tighten_wording_does_not_claim_a_tighter_network(app, sample_mode):
    out = netbird.propose({})
    assert out["status"] == "pending_approval" and out["sample"] is True
    assert out["summary"].startswith("SAMPLE DATA:") and "not removed or disabled" in out["summary"]
    assert "tighten" not in out["summary"].lower()
    src = (HERE / "netbird.py").read_text()
    assert "tightened policy" not in src and "approval-gated tighten" not in src
    assert "The original policy was not changed" in src


def test_permissions_page_states_its_data_source_before_and_after_the_read():
    page = permissions_ui.PERM_PAGE
    assert "SAMPLE DATA" in page                                     # static text, present before any query
    assert "Live Doris data" not in page and "live permissions" not in page
    assert "not reachable. Nothing below was read from a live database" in page


# ── dry run: every place that shows a ban result says nothing was blocked ──────────────────────────────────────────
def test_approve_block_dry_run_says_nothing_was_blocked(app, monkeypatch):
    monkeypatch.delenv("SENTINEL_BLOCK_ENABLED", raising=False)
    out = app._approve_block({"ip": "8.8.4.4"})
    assert out["status"] == "dry_run"
    assert out["summary"].startswith("Dry run: nothing was blocked") and "DRY-RUN" not in out["summary"]
    assert "nothing was blocked" in out["detail"]


def test_network_policy_dry_run_says_nothing_was_changed(app, monkeypatch):
    monkeypatch.delenv("SENTINEL_BLOCK_ENABLED", raising=False)
    c = TestClient(app.app)
    r = c.post("/agent/run", json={"action": "approve_network_policy", "draft": {"name": "x"}},
               headers={"Authorization": "Basic " + __import__("base64").b64encode(b"u:p").decode()})
    if r.status_code in (401, 503):                                  # operator credential not configured in this env
        monkeypatch.setenv("SENTINEL_BASIC_AUTH_USER", "u")
        monkeypatch.setenv("SENTINEL_BASIC_AUTH_PASS", "p")
        r = c.post("/agent/run", json={"action": "approve_network_policy", "draft": {"name": "x"}},
                   headers={"Authorization": "Basic " + __import__("base64").b64encode(b"u:p").decode()})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "dry_run" and r.json()["summary"].startswith("Dry run: nothing was changed in NetBird")


def test_case_execute_dry_run_result_and_receipt_say_nothing_was_blocked():
    src = (HERE / "live.py").read_text()
    assert '"summary": "Dry run: nothing was blocked."' in src
    assert "Dry run: nothing was blocked (SENTINEL_BLOCK_ENABLED not set; edge not touched)" in src
    page = (HERE / "app.py").read_text()
    assert "Dry run: nothing was blocked (SENTINEL_BLOCK_ENABLED unset). Receipt: " in page
    assert "DRY-RUN" not in page


def test_cases_panel_labels_dry_run_and_sample_history(app):
    html = app._cases_panel([])
    assert "dry run" in html and "nothing is blocked" in html
    assert "sample history" in html


# ── seeded history is labelled as sample ─────────────────────────────────────────────────────────────────────────
def test_every_seeded_decision_is_labelled_sample_history():
    assert seed.SEED_DECISIONS
    for _flag, _val, _dur, reason in seed.SEED_DECISIONS:
        assert reason.startswith("SAMPLE HISTORY: "), reason
        assert "crowdsecurity/" in reason                              # the scenario token the dashboard parses survives


# ── tagline and docs ─────────────────────────────────────────────────────────────────────────────────────────────
def test_manifest_and_module_tagline_do_not_claim_a_firewall():
    manifest = importlib.import_module("edge-sentinel.manifest")
    text = (HERE / "manifest.py").read_text() + (HERE.parent.parent / "modules.yaml").read_text()
    assert "Your firewall" not in text
    assert "approval-gated response" in text
    assert manifest  # imports cleanly


def test_readme_does_not_describe_the_learning_policy_as_running_or_a_green_all_clear():
    readme = (HERE / "README.md").read_text()
    assert "All systems normal" not in readme and "agent active" not in readme
    assert "not running in this app" in readme and "simulated" in readme

"""Deployment Inspection must not turn Edge Sentinel's SAMPLE data into findings.

Edge Sentinel serves hard-coded sample peers, policies and databases on its network and backup planes until they are
connected, and marks that with `live: false` (and `data_status: "sample"`). The payloads below are shaped like what the
live demo returns (https://sentinel.redevops.io/api/network/review and /api/backups/status, observed 2026-11-01).
"""
from __future__ import annotations

from agentic_os.agent_gateway.external.deployment_inspection import (
    SAMPLE_NOTE, derive_findings, demo_inspection_mission, is_sample_payload, run_inspection)
from agentic_os.agent_gateway.external.sample_projections import deployment_inspection_view

NET_RISKS = [
    {"kind": "broad_policy", "severity": "high", "detail": "policy 'Default' rule 'Default' allows All→All (all) — over-broad"},
    {"kind": "unapproved_peer", "severity": "high", "detail": "peer 'contractor-vm' is pending approval — unapproved device on the network"},
    {"kind": "login_expired", "severity": "medium", "detail": "peer 'example-laptop' has an expired login — should re-authenticate"}]
BACKUPS = {"total": 5, "ok": 4, "never": ["listmonk (lifecycle)"], "no_offsite": ["lago (billing)"], "unencrypted": []}
ACTIVITY = {"connected": True, "has_threat": False, "kpis": [{"label": "Active decisions", "value": "0"}], "decisions": []}


def snap(net_extra, bk_extra, activity=None):
    return {"/health": {"status": "ok", "core": "crowdsec", "connected": True},
            "/api/activity": activity or ACTIVITY,
            "/api/network/review": {"peers": 4, "connected": 3, "policies": 2, "risks": NET_RISKS, **net_extra},
            "/api/backups/status": {**BACKUPS, **bk_extra}}


SAMPLE_AS_SERVED = snap({"live": False}, {"live": False})                       # what the demo serves today
SAMPLE_FLAGGED = snap({"live": False, "data_status": "sample"}, {"live": False, "data_status": "sample"})
LIVE = snap({"live": True, "data_status": "live"}, {"live": True, "data_status": "live"})
LEGACY = snap({}, {})                                                           # an older Sentinel: neither key


def test_payload_classification():
    assert is_sample_payload({"live": False}) and is_sample_payload({"data_status": "sample"})
    assert is_sample_payload({"live": False, "data_status": "sample"})
    assert not is_sample_payload({"live": True, "data_status": "live"}) and not is_sample_payload({}) and not is_sample_payload(None)


def test_sample_mode_sentinel_produces_no_findings_from_network_or_backups():
    for s in (SAMPLE_AS_SERVED, SAMPLE_FLAGGED):
        assert derive_findings(s) == []


def test_sample_sources_are_reported_as_sample_data_not_a_finding():
    report = run_inspection("https://sentinel.example", fetch=lambda b, p: SAMPLE_FLAGGED.get(p))
    assert report.findings == [] and report.top_finding is None and report.proposed_actions == []
    by = {x["source"]: x for x in report.sample_sources}
    assert by["network"] == {"source": "network", "status": SAMPLE_NOTE, "items": 3}
    assert by["backups"] == {"source": "backups", "status": SAMPLE_NOTE, "items": 2}
    assert SAMPLE_NOTE == "sample data, not a finding" and report.view()["sample_sources"] == report.sample_sources


def test_live_sentinel_still_produces_the_same_findings():
    kinds = {f.kind for f in derive_findings(LIVE)}
    assert {"broad_policy", "unapproved_peer", "login_expired", "no_backup", "no_offsite"} <= kinds
    assert run_inspection("https://sentinel.example", fetch=lambda b, p: LIVE.get(p)).sample_sources == []


def test_a_payload_without_the_flags_is_read_as_live_as_before():
    assert {"broad_policy", "no_backup"} <= {f.kind for f in derive_findings(LEGACY)}


def test_mixed_sample_network_and_live_backups():
    s = snap({"live": False}, {"live": True})
    kinds = {f.kind for f in derive_findings(s)}
    assert "no_backup" in kinds and "broad_policy" not in kinds
    assert [x["source"] for x in run_inspection("t", fetch=lambda b, p: s.get(p)).sample_sources] == ["network"]


def test_demo_mission_on_a_sample_sentinel_governs_nothing():
    out = demo_inspection_mission("https://sentinel.example", fetch=lambda b, p: SAMPLE_AS_SERVED.get(p))
    assert out["governed_action"] is None and out["inspection"]["findings"] == []
    assert len(out["inspection"]["sample_sources"]) == 2


def test_projects_card_for_a_sample_sentinel_has_no_findings_and_says_why():
    view = deployment_inspection_view("https://sentinel.example", fetch=lambda b, p: SAMPLE_AS_SERVED.get(p))
    assert view["finding_count"] == 0 and view["findings"] == [] and view["governed_action"] is None
    assert {x["status"] for x in view["sample_sources"]} == {SAMPLE_NOTE}


def test_crowdsec_finding_does_not_claim_enforcement_against_live_sources():
    act = {"connected": True, "has_threat": True, "decisions": [{"value": "203.0.113.9"}]}
    details = [f.detail for f in derive_findings(snap({"live": True}, {"live": True}, act)) if f.kind == "active_threats"]
    assert len(details) == 1 and "1 active CrowdSec decision(s) held" in details[0]
    assert "enforced against live sources" not in details[0] and "not proof that a bouncer enforces" in details[0]


def test_unread_decisions_are_not_a_finding():
    act = {"connected": True, "has_threat": True, "decisions": [{"value": "x"}], "decisions_status": {"ok": False, "note": "HTTP 401"}}
    assert [f for f in derive_findings(snap({"live": True}, {"live": True}, act)) if f.kind == "active_threats"] == []


# ── malformed and conflicting payloads must neither raise nor hide findings ─────────────────────────────────────
def test_conflicting_flags_are_not_trusted_to_drop_findings():
    assert not is_sample_payload({"live": False, "data_status": "live"}) and not is_sample_payload({"live": True, "data_status": "sample"})
    assert is_sample_payload({"live": False, "data_status": "sample"})
    s = snap({"live": False, "data_status": "live"}, {"live": True, "data_status": "sample"})
    assert {"broad_policy", "no_backup"} <= {f.kind for f in derive_findings(s)}               # read as live: findings kept
    assert run_inspection("t", fetch=lambda b, p: s.get(p)).sample_sources == []


def test_a_non_object_decisions_status_does_not_raise():
    for bad in ("down", ["ok"], 7, None):
        act = {"connected": True, "has_threat": True, "decisions": [{"value": "x"}], "decisions_status": bad}
        kinds = [f.kind for f in derive_findings(snap({"live": True}, {"live": True}, act))]
        assert "active_threats" in kinds                      # an unreadable status is not a reason to drop the finding


def test_malformed_payload_shapes_do_not_raise():
    junk = {"/health": "up", "/api/activity": [1], "/api/network/review": ["x"], "/api/backups/status": "nope"}
    assert derive_findings(junk) == []
    s = snap({"live": True}, {"live": True})
    s["/api/network/review"]["risks"] = "oops"
    s["/api/backups/status"]["never"] = {"a": 1}
    s["/api/activity"] = {"has_threat": True, "decisions": "many"}
    kinds = {f.kind for f in derive_findings(s)}
    assert "no_offsite" in kinds and "broad_policy" not in kinds
    assert run_inspection("t", fetch=lambda b, p: junk.get(p)).connected is False

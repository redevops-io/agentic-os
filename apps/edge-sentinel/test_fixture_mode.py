"""P4: the fixture core must be honest, deterministic, and incapable of touching a live edge.

The claims under test are the ones that make a demo safe to put in front of a customer:

1. it is **off** unless an operator says so, and turning it on changes the *data source* only;
2. the numbers on the page are computed by the real `fetch_activity` path from known inputs, so the
   dashboard is under test rather than bypassed;
3. no CrowdSec side effect is reachable — not through the UI, not through the handlers, and not with
   `SENTINEL_BLOCK_ENABLED` set, which is the most permissive write configuration the app has;
4. the page says it is synthetic, and `/health` says which mode produced it;
5. demo mode does not weaken P9: exposure still needs authorisation and credentials.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

core = importlib.import_module("edge-sentinel.core")
fixture_mode = importlib.import_module("edge-sentinel.fixture_mode")


@pytest.fixture
def demo():
    """Install the fixture core for one test and put the original functions back afterwards."""
    fixture_mode.uninstall()
    yield fixture_mode
    fixture_mode.uninstall()


# ── enabling ──────────────────────────────────────────────────────────────────────────────────────────

def test_demo_is_off_unless_an_operator_turns_it_on():
    assert fixture_mode.enabled({}) is False
    assert fixture_mode.enabled({"EDGE_SENTINEL_DEMO": ""}) is False
    assert fixture_mode.enabled({"EDGE_SENTINEL_DEMO": "0"}) is False
    assert fixture_mode.enabled({"EDGE_SENTINEL_DEMO": "1"}) is True


def test_an_unknown_scenario_falls_back_to_mixed_rather_than_nothing():
    # a typo in a demo flag must not produce an empty dashboard that looks like a clean environment
    assert fixture_mode.scenario({"EDGE_SENTINEL_DEMO_SCENARIO": "mixd"}) == "mixed"
    assert fixture_mode.scenario({}) == "mixed"
    assert fixture_mode.scenario({"EDGE_SENTINEL_DEMO_SCENARIO": "unavailable"}) == "unavailable"


def test_install_swaps_the_transport_and_uninstall_restores_it(demo):
    before = (core.crowdsec_connected, core.fetch_decisions, core.fetch_alerts, core.block_ip)
    report = demo.install({})
    assert report["installed"] and not report["already"]
    assert set(report["patched"]) == {"crowdsec_connected", "fetch_decisions", "fetch_alerts",
                                      "block_ip", "unblock_ip", "_cscli"}
    assert (core.crowdsec_connected, core.fetch_decisions, core.fetch_alerts, core.block_ip) != before
    demo.uninstall()
    assert (core.crowdsec_connected, core.fetch_decisions, core.fetch_alerts, core.block_ip) == before


# ── the numbers are computed, not scripted ────────────────────────────────────────────────────────────

def _kpi(activity: dict, label: str) -> dict:
    return next(k for k in activity["kpis"] if k["label"] == label)


def test_fixture_rows_run_through_the_real_kpi_path(demo):
    demo.install({})
    a = core.fetch_activity(force=True)

    assert a["connected"] is True
    assert _kpi(a, "Active decisions")["value"] == "2", "both fixture bans are type=ban"
    assert _kpi(a, "Alerts (24h)")["value"] == "3", "the 26h-old benign alert is outside the 24h window"
    assert _kpi(a, "Source IPs")["value"] == "2", "two unique sources hold active decisions"
    assert a["has_threat"] is True
    # sorting and severity bucketing are production code, not fixture data
    assert a["decisions"][0]["severity"] == "critical", "the RCE decision must outrank the brute-force one"
    assert [r["scenario_full"] for r in a["alerts"]][0].startswith("crowdsecurity/http-cve"), "newest first"
    assert a["counts"] == {"decisions": 2, "alerts": 4, "sources": 2}


def test_the_stories_are_present_and_the_ambiguous_one_has_no_decision(demo):
    demo.install({})
    a = core.fetch_activity(force=True)
    by_scenario = {r["scenario_full"]: r for r in a["alerts"]}
    assert {"crowdsecurity/http-cve-2021-41773-rce", "crowdsecurity/ssh-bf",
            "custom/unclear-signal-needs-review", "crowdsecurity/tor-exit-node"} <= set(by_scenario)
    # the abstain case: an alert the core acted on in no way at all
    assert by_scenario["custom/unclear-signal-needs-review"]["origin"] == ""
    assert by_scenario["crowdsecurity/ssh-bf"]["events"] == 5              # the repeated story


def test_unavailable_scenario_reports_not_read_rather_than_zero(demo):
    """The distinction matters: '0 decisions' is a claim about a clean edge, 'not read' is a claim about
    our own visibility. A demo that shows 0 for an unreachable core teaches the wrong lesson."""
    demo.install({"EDGE_SENTINEL_DEMO_SCENARIO": "unavailable"})
    a = core.fetch_activity(force=True)
    assert a["connected"] is False
    for label in ("Active decisions", "Alerts (24h)", "Source IPs", "Last event"):
        assert _kpi(a, label)["value"] == "not read", label
    assert a["decisions_status"]["ok"] is False and a["alerts_status"]["ok"] is False


def test_fixture_addresses_cannot_point_at_a_real_host(demo):
    """RFC 5737 documentation ranges only. A screenshot or a report that carries a routable address is a
    disclosure risk, and 192.168.x is not 'safe' — it is somebody's network."""
    demo.install({})
    import ipaddress
    rows = [d["value"] for d in core.fetch_decisions()] + [a["source"]["value"] for a in core.fetch_alerts()]
    assert rows, "the fixtures produced no addresses - this test would pass vacuously"
    for raw in rows:
        ip = ipaddress.ip_address(raw)
        assert raw.startswith(("192.0.2.", "198.51.100.", "203.0.113.")), f"{ip} is not a documentation range"


# ── no side effect is reachable ───────────────────────────────────────────────────────────────────────

def test_every_crowdsec_side_effect_refuses(demo):
    demo.install({})
    for fn, arg in ((core.block_ip, {"ip": "192.0.2.53"}), (core.unblock_ip, {"ip": "192.0.2.53"})):
        with pytest.raises(fixture_mode.DemoRefused) as e:
            fn(arg)
        assert "no CrowdSec side effect is permitted" in str(e.value)
    with pytest.raises(fixture_mode.DemoRefused):
        core._cscli(["bouncers", "list"])


def test_the_write_path_cannot_reach_the_core_even_when_blocks_are_enabled(demo, monkeypatch):
    """`SENTINEL_BLOCK_ENABLED=1` is the configuration in which an approved ban really is enforced, so it
    is the configuration in which a demo must still be inert.

    Asserted by tripwire rather than by expecting `DemoRefused`: the handler has an earlier refusal of its
    own (`ipguard` treats RFC 5737 space as reserved and declines to ban it), so "it raised DemoRefused"
    could pass while a different path still reached the LAPI. What must be true in every branch is that
    nothing leaves the process. `__wrapped__` skips the approval gate so the write path itself is
    isolated, and `_unblock` is included because it calls `core.unblock_ip` with no dry-run switch.
    """
    monkeypatch.setenv("SENTINEL_BLOCK_ENABLED", "1")
    demo.install({})
    app_module = importlib.import_module("edge-sentinel.app")
    hits: list = []
    monkeypatch.setattr(core.httpx, "get", _Tripwire(hits))
    monkeypatch.setattr(core.httpx, "Client", _Tripwire(hits))
    monkeypatch.setattr(core.subprocess, "run", _Tripwire(hits))

    for call in (app_module._approve_block.__wrapped__, app_module._unblock):
        try:
            result = call({"ip": "192.0.2.53"})
        except fixture_mode.DemoRefused:
            continue                                        # the direct refusal we are looking for
        assert result.get("ip_blocked") is not True and result.get("enforced") is not True, result
    assert hits == [], f"the write path reached the CrowdSec boundary: {hits}"


def test_the_refusal_surfaces_as_a_403_not_a_500(demo, load_app):
    """The refusal is raised deep in `core`; the operator still needs a clean answer, not a stack trace."""
    demo.install({})
    module = load_app("edge-sentinel")
    handler = module.app.exception_handlers[fixture_mode.DemoRefused]
    import asyncio
    response = asyncio.run(handler(None, fixture_mode.DemoRefused("block_ip refused")))
    assert response.status_code == 403
    import json
    payload = json.loads(response.body)
    assert payload["refused"] == "demo_mode" and payload["synthetic"] is True


class _Tripwire:
    """Anything that reaches this has tried to leave the process."""

    def __init__(self, calls):
        self.calls = calls

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError(f"fixture mode reached the network/subprocess: {args[:1]}")


def test_no_request_can_reach_the_crowdsec_boundary(demo, load_app, monkeypatch):
    """Reads and the mutating routes, with the CrowdSec transport tripped. If any handler still reached
    for the LAPI or for `docker exec cscli`, this raises inside the request."""
    demo.install({})
    module = load_app("edge-sentinel")
    calls: list = []
    monkeypatch.setattr(core.httpx, "get", _Tripwire(calls))
    monkeypatch.setattr(core.httpx, "Client", _Tripwire(calls))
    monkeypatch.setattr(core.subprocess, "run", _Tripwire(calls))

    c = TestClient(module.app)
    assert c.get("/health").status_code == 200
    assert c.get("/api/activity").status_code == 200
    assert c.post("/api/agent", json={"action": "triage"}).status_code in (200, 403)
    assert c.post("/api/agent", json={"action": "block_ip", "ip": "192.0.2.53"}).status_code in (200, 403)
    assert calls == [], f"fixture mode touched the real boundary: {calls}"


# ── labelling, and the boundary demo mode must not cross ──────────────────────────────────────────────

def test_the_dashboard_says_its_data_is_synthetic(demo, load_app):
    demo.install({})
    c = TestClient(load_app("edge-sentinel").app)
    body = c.get("/").text
    assert fixture_mode.SYNTHETIC_TAG in body, "a demo screenshot must be self-identifying"
    assert "role='status'" in body or 'role="status"' in body, "assistive tech should announce it too"


def test_the_banner_is_absent_when_demo_is_off(load_app):
    fixture_mode.uninstall()
    body = TestClient(load_app("edge-sentinel").app).get("/").text
    assert fixture_mode.SYNTHETIC_TAG not in body


def test_health_reports_which_mode_produced_the_answer(demo, load_app):
    demo.install({})
    payload = TestClient(load_app("edge-sentinel").app).get("/health").json()
    assert payload["demo"]["enabled"] is True
    assert payload["connected"] is True, "/health must report the fixture's view, not the pre-swap alias"


def test_demo_mode_does_not_weaken_the_p9_exposure_guard():
    """The interesting failure mode of a demo flag is that it becomes a way to run exposed without
    authorisation. It must not: the guard reads its own variables and demo is not one of them."""
    security = importlib.import_module("edge-sentinel.security")
    with_demo = {"EDGE_SENTINEL_DEMO": "1"}

    with pytest.raises(security.ExposureRefused):
        security.exposure_guard("0.0.0.0", env=dict(with_demo))                       # reachable, unauthorised
    with pytest.raises(security.ExposureRefused):
        # authorised to bind, but no credential and no explicit read-only-demo opt-out
        security.exposure_guard("0.0.0.0", env={**with_demo, "SENTINEL_ALLOW_EXPOSURE": "1"})
    # the documented paths still work, and demo mode changes neither of them
    security.exposure_guard("127.0.0.1", env=dict(with_demo))
    security.exposure_guard("0.0.0.0", env={**with_demo, "SENTINEL_ALLOW_EXPOSURE": "1",
                                            "SENTINEL_BASIC_AUTH_USER": "ops", "SENTINEL_BASIC_AUTH_PASS": "x" * 16})


def test_credentials_are_still_required_alongside_demo_mode():
    security = importlib.import_module("edge-sentinel.security")
    assert security.operator_credential_configured({"EDGE_SENTINEL_DEMO": "1"}) is False
    assert security.operator_credential_configured({"EDGE_SENTINEL_DEMO": "1",
                                                    "SENTINEL_BASIC_AUTH_USER": "ops",
                                                    "SENTINEL_BASIC_AUTH_PASS": "x" * 16}) is True


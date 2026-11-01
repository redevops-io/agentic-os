"""Behaviour tests for the Edge Sentinel UI claims, driven through the real code paths (no hand-built payloads where the
logic under test is how the payload is made):

* H-2: a FAILED CrowdSec read is never shown as a reading or as a quiet system. `fetch_activity` is run against a stub
  LAPI (ok, 401, timeout, malformed JSON, empty-but-ok, alert feed down) and the rendered page and chat answers checked.
* H-1: the REAL chat tool descriptions and system prompt (the console is built with a recording stand-in, so this runs
  in CI without `context_runtime`) must not claim a capability the demo does not provide.
* M-1: the "sample data" label on the banner link and the chat chip follows the same snapshot the panel uses.
* M-2: hand-added entries are tagged with only what is known.
"""
from __future__ import annotations

import html as _html
import importlib
import json as _json
import pathlib
import re
import sys
import threading
import time
import types

import pytest

HERE = pathlib.Path(__file__).resolve().parent
if (HERE / "core.py").exists():                          # the public tree: tests live next to the app
    APPDIR, REPO = HERE, HERE.parent.parent
else:                                                    # the deployed copy: tests live in <repo>/tests
    REPO = HERE.parent
    APPDIR = REPO / "agents" / "edge-sentinel"
    sys.path.insert(0, str(REPO / "agents"))
core = importlib.import_module("edge-sentinel.core")
netbird = importlib.import_module("edge-sentinel.netbird")
portabase = importlib.import_module("edge-sentinel.portabase")
httpx = pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

PUBLIC_TREE = hasattr(core, "_cscli") and not hasattr(core, "_machine_token")

DECISION = {"value": "203.0.113.9", "scope": "Ip", "scenario": "crowdsecurity/ssh-bf", "type": "ban", "duration": "3h59m",
            "origin": "crowdsec"}
ALERT = {"scenario": "crowdsecurity/ssh-bf", "source": {"value": "203.0.113.9", "scope": "Ip"},
         "created_at": "2026-11-01T10:00:00Z", "events_count": 5, "decisions": [{"origin": "crowdsec"}]}


@pytest.fixture()
def app(request, monkeypatch):
    try:
        mod = request.getfixturevalue("load_app")("edge-sentinel")
    except pytest.FixtureLookupError:                     # the deployed copy has no load_app fixture
        mod = importlib.import_module("edge-sentinel.app")
    monkeypatch.setattr(mod.live, "sync_cases", lambda *a, **k: [])
    return mod


class _R:
    def __init__(self, code, body=None, bad_json=False):
        self.status_code, self._body, self._bad = code, body, bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._body


def stub_lapi(monkeypatch, *, decisions="ok", alerts="ok", alert_items=None, alert_delay=0.0):
    """A stub CrowdSec: GET /health answers 404 (up), /v1/decisions behaves per `decisions`, the alert feed per `alerts`."""
    monkeypatch.setattr(core, "CROWDSEC_BOUNCER_KEY", "bouncer-key")
    core._CACHE.update(ts=0.0, data=None)

    def get(url, headers=None, timeout=None, params=None):
        if url.endswith("/v1/decisions"):
            if decisions == "timeout":
                raise httpx.ReadTimeout("slow")
            return {"ok": _R(200, [DECISION]), "empty": _R(200, []), "null": _R(200, None), "401": _R(401, {"message": "no"}),
                    "malformed": _R(200, bad_json=True), "object": _R(200, {"a": 1})}[decisions]
        return _R(404, {})                                # /health and /: the LAPI answers, with any status

    items = [ALERT] if alert_items is None else alert_items
    ns = dict(get=get, HTTPError=httpx.HTTPError, ConnectError=httpx.ConnectError, ReadTimeout=httpx.ReadTimeout)
    if PUBLIC_TREE:
        monkeypatch.setattr(core, "httpx", types.SimpleNamespace(**ns))
        def cscli(args):
            time.sleep(alert_delay)
            if alerts == "fail":
                return 1, "", "boom"
            return (0, "this is not json", "") if alerts == "malformed" else (0, _json.dumps(items), "")
        monkeypatch.setattr(core, "_cscli", cscli)
    else:
        monkeypatch.setenv("SENTINEL_LAPI_MACHINE_ID", "m")
        monkeypatch.setenv("SENTINEL_LAPI_MACHINE_PASSWORD", "p")
        core._TOKEN.update(tok="", exp=0.0)
        core._ALERTS_STATUS.update(ok=False, note=core.ALERTS_NOT_CONFIGURED)

        class Client:
            def __init__(self, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def post(self, url, json=None, headers=None):
                return _R(401, {}) if alerts == "fail" else _R(200, {"token": "t", "expire": "2999-01-01T00:00:00Z"})

            def get(self, url, params=None, headers=None):
                time.sleep(alert_delay)
                return _R(200, bad_json=True) if alerts == "malformed" else _R(200, items)
        ns["Client"] = Client
        monkeypatch.setattr(core, "httpx", types.SimpleNamespace(**ns))


def page(app):
    return app.render(core.fetch_activity(force=True))


# ── H-2: a failed read is not a reading ──────────────────────────────────────────────────────────────────────────
def test_ok_read_is_stamped_and_rendered_as_a_reading(app, monkeypatch):
    stub_lapi(monkeypatch)
    data = core.fetch_activity(force=True)
    assert data["decisions_status"]["ok"] and data["alerts_status"]["ok"]
    assert data["read_at"].endswith(" UTC") and data["read_at_iso"].startswith("20") and "T" in data["read_at_iso"]
    assert data["decisions_read_at"] == data["alerts_read_at"] == data["read_at"]
    html = app.render(data)
    assert "ACTIVE DECISIONS" in html and "read from CrowdSec at " + data["read_at"] in html


@pytest.mark.parametrize("mode,note", [("401", "HTTP 401"), ("timeout", "ReadTimeout"), ("malformed", "malformed response"),
                                       ("object", "unexpected response")])
def test_failed_decisions_read_is_never_shown_as_quiet_or_as_a_read(app, monkeypatch, mode, note):
    stub_lapi(monkeypatch, decisions=mode, alert_items=[])
    data = core.fetch_activity(force=True)
    assert data["decisions_status"]["ok"] is False and note in data["decisions_status"]["note"]
    assert data["read_at"] == "" and data["read_at_iso"] == "" and data["decisions_read_at"] == ""
    assert {k["label"]: k["value"] for k in data["kpis"]}["Active decisions"] == "not read"
    html = app.render(data)
    assert "DECISIONS NOT READ" in html and note in html
    assert "Quiet reading" not in html
    assert "read from CrowdSec at" not in app._status_banner(data) + app._read_note(data)   # decisions failed: no overall read claim
    assert "0 active decisions" not in html                      # the empty list from a failed read is not a count
    monkeypatch.setattr(app, "fetch_activity", lambda force=False: data)
    assert "I could not read CrowdSec's decisions" in app._t_list_bans({})["text"]
    assert "only partly read" in app._t_threat_summary({})["text"]


@pytest.mark.parametrize("mode", ["empty", "null"])
def test_empty_but_ok_read_is_a_quiet_reading_with_a_stamp(app, monkeypatch, mode):
    stub_lapi(monkeypatch, decisions=mode, alert_items=[])
    data = core.fetch_activity(force=True)
    assert data["decisions_status"]["ok"] and data["alerts_status"]["ok"] and data["read_at"]
    html = app.render(data)
    assert "Quiet reading" in html and "0 active decisions and 0 alerts" in html and "read from CrowdSec at " + data["read_at"] in html
    assert "not an assessment of the environment" in html


def test_alert_feed_down_with_no_decisions_says_so_and_is_not_quiet(app, monkeypatch):
    stub_lapi(monkeypatch, decisions="empty", alerts="fail")
    data = core.fetch_activity(force=True)
    assert data["alerts_status"]["ok"] is False and data["alerts_read_at"] == "" and data["read_at"] == ""
    assert data["decisions_read_at"]                                         # the decisions WERE read
    html = app.render(data)
    assert "ALERT FEED UNAVAILABLE" in html and "Quiet reading" not in html
    assert "alerts not read (" in html


def test_alert_feed_down_with_active_decisions_is_stated_in_the_red_banner_and_the_card(app, monkeypatch):
    stub_lapi(monkeypatch, decisions="ok", alerts="fail")
    data = core.fetch_activity(force=True)
    banner = app._status_banner(data)
    assert "ACTIVE DECISIONS" in banner and "alert feed could not be read" in banner     # not silently omitted
    assert "Latest alert" not in banner and "critical alert" not in banner               # no alert claim from an unread feed
    card = app._alert_feed(data)
    assert "alerts not read (" in card and "read from CrowdSec at" not in card           # the card does not claim a read


def test_unreachable_core_is_not_a_reading(app, monkeypatch):
    stub_lapi(monkeypatch)
    monkeypatch.setattr(core, "crowdsec_connected", lambda: False)
    data = core.fetch_activity(force=True)
    assert data["connected"] is False and data["read_at"] == "" and data["decisions_status"]["note"] == "CrowdSec unreachable"
    html = app.render(data)
    assert "UNREACHABLE" in html and "Quiet reading" not in html


# ── H-1: the console's real tool text and system prompt ─────────────────────────────────────────────────────────
class _StubConsole:
    def __init__(self, tenant, primer, tools=(), *, suggestions=(), subtitle="", model=None, allow_side_effects=None, **k):
        self.tenant, self.primer, self.subtitle = tenant, primer, subtitle
        self.suggestions, self.allow = list(suggestions), allow_side_effects
        self.tools = {t["name"]: t for t in tools}

    def panel_html(self, mount="agent", endpoint="api/agent"):
        chips = "".join(f"<button class='chip'>{_html.escape(s)}</button>" for s in self.suggestions)
        return f"<div class='console'><p class='sub'>{_html.escape(self.subtitle)}</p>{chips}</div>"


def _stub_tool(name, description, fn, *, parameters=None, side_effecting=False):
    return {"name": name, "description": description, "side_effecting": side_effecting}


EXPECTED_TOOLS = {
    "threat_summary", "list_bans", "explain_alert", "ban_ip", "supply_chain_scan", "list_vulnerabilities",
    "explain_vulnerability", "scan_app", "scan_apps", "list_scannable_apps", "vuln_db_lookup", "vuln_db_permissions", "sbom",
    "scanner_status", "scan_deployment", "scan_status", "scan_results", "malware_scan", "wazuh_status", "network_access_review",
    "network_posture", "propose_network_policy", "backup_status"}
CLAIMS_THE_DEMO_CANNOT_BACK = [
    "block an IP at the edge", "threat posture", "scan all apps", "exploitable", "SIEM", "who is being blocked",
    "CrowdSec blocked them", "nothing to do", "inspects the software before you trust it", "block the attacker",
    "scan MANY agentic apps", "scan our own software supply chain", "for the stack",
]


@pytest.fixture()
def console(app):
    return app._build_sentinel_console(_StubConsole, _stub_tool)


def test_all_23_tools_are_registered_and_none_claims_what_the_demo_cannot_do(console):
    assert set(console.tools) == EXPECTED_TOOLS and len(console.tools) == 23
    for name, t in console.tools.items():
        for bad in CLAIMS_THE_DEMO_CANNOT_BACK:
            assert bad.lower() not in t["description"].lower(), f"{name}: {bad!r}"
    for name in ("scan_app", "scan_apps", "list_scannable_apps", "scan_deployment"):
        assert console.tools[name]["description"].startswith("Not available"), name
    assert "Scanning other apps' images needs docker access" in console.tools["supply_chain_scan"]["description"]
    for name in ("network_access_review", "network_posture", "backup_status", "propose_network_policy"):
        assert "sample data" in console.tools[name]["description"].lower(), name
    ban = console.tools["ban_ip"]["description"]
    assert console.tools["ban_ip"]["side_effecting"] and "dry run" in ban and "never runs from this chat" in ban
    assert console.tools["scan_deployment"]["side_effecting"] and "never runs here" in console.tools["scan_deployment"]["description"]
    assert console.allow == []                                              # no side-effecting tool may run from chat


def test_system_prompt_tells_the_model_what_is_true(console):
    p = console.primer
    for bad in CLAIMS_THE_DEMO_CANNOT_BACK:
        assert bad.lower() not in p.lower(), bad
    assert "depends on a bouncer" in p and "edge-sentinel does not know whether one is present" in p
    assert "dry run" in p and "not available in the public demo" in p
    assert "Never describe the network as safe or clean" in p


def test_console_page_chips_and_subtitle_come_from_the_console(app, console, monkeypatch):
    stub_lapi(monkeypatch)
    monkeypatch.setattr(app, "fetch_activity", core.fetch_activity)       # other tests may have replaced it on the module
    monkeypatch.setattr(app, "_SENTINEL_CONSOLE", console)
    monkeypatch.setattr(app, "_is_sample", lambda name: True)
    html = TestClient(app.app).get("/").text
    assert "Review network access (sample data)" in html and "List the active bans" in html
    assert "Scan the billing app" not in html and "Scan a running deployment" not in html and "SIEM" not in html
    assert "app image scans" not in html.lower() and "deployment scans" not in html.lower()   # no scan is advertised
    assert html.count("CONTEXT RUNTIME") == 1                                # the sticky banner appears once


# ── M-1: the sample label follows the panel's real state ────────────────────────────────────────────────────────
def _state():
    return importlib.import_module("edge-sentinel.app")._SAMPLE_STATE


def _reset_sample():
    for st in _state().values():
        st.update(ts=0.0, sample=True, busy=False)


def test_token_against_an_unreachable_service_is_still_labelled_sample_everywhere(app, monkeypatch):
    monkeypatch.setattr(netbird, "NETBIRD_API_TOKEN", "bad-token")
    monkeypatch.setattr(netbird, "_nb_get", lambda path: None)                  # the service does not answer
    assert netbird.snapshot()["live"] is False and "SAMPLE DATA" in netbird.panel()      # the panel says sample
    _reset_sample()
    app._refresh_sample_state("netbird")
    assert "Network access (sample data)" in app._cr_banner()                    # so does the banner link
    assert "Review network access (sample data)" in app._chips()                 # and the chat chip


def test_label_is_dropped_only_when_the_snapshot_is_really_live(app, monkeypatch):
    monkeypatch.setattr(netbird, "NETBIRD_API_TOKEN", "good-token")
    monkeypatch.setattr(netbird, "snapshot", lambda: {"live": True, "peers": [], "policies": []})
    monkeypatch.setattr(portabase, "PORTABASE_API_TOKEN", "")
    _reset_sample()
    app._refresh_sample_state("netbird")
    b = app._cr_banner()
    assert "Network access (sample data)" not in b and "Backups (sample data)" in b
    assert "Review network access" in app._chips() and "Review network access (sample data)" not in app._chips()


def test_no_token_is_sample_without_any_network_call(app, monkeypatch):
    monkeypatch.setattr(netbird, "NETBIRD_API_TOKEN", "")
    monkeypatch.setattr(portabase, "PORTABASE_API_TOKEN", "")
    _reset_sample()
    assert app._is_sample("netbird") is True and app._is_sample("portabase") is True


# ── M-2: manual tags say only what is known ──────────────────────────────────────────────────────────────────────
def test_manual_tag_does_not_claim_it_is_sample_history(app):
    tag = app._origin_tag("cscli", "approved ban reason")
    assert "manual (cscli)" in tag and "approved block" in tag and "sample history created by seed.py" not in tag
    assert app._origin_tag("crowdsec", "x") == "" and app._origin_tag("", "x") == "" and app._origin_tag("CAPI", "x") == ""


def test_seed_marker_survives_to_the_page(app, monkeypatch):
    stub = {"value": "203.0.113.9", "scope": "Ip", "scenario": "SAMPLE HISTORY: ssh bruteforce (crowdsecurity/ssh-bf)", "type": "ban",
            "duration": "1h", "origin": "cscli"}
    stub_lapi(monkeypatch, alert_items=[{"scenario": stub["scenario"], "source": {"value": "203.0.113.9", "scope": "Ip"},
                                         "created_at": "2026-11-01T10:00:00Z", "events_count": 1, "decisions": []}])
    monkeypatch.setattr(core, "fetch_decisions", lambda: (core._DECISIONS_STATUS.update(ok=True, note="") or [stub]))
    data = core.fetch_activity(force=True)
    assert data["decisions"][0]["scenario"] == "crowdsecurity/ssh-bf"           # the short name is unchanged
    html = app.render(data)
    assert html.count(">sample history</span>") >= 2                            # decisions table AND the alert with no decision
    assert "sample history created by seed.py" not in html


def test_alert_with_an_empty_decision_list_and_no_marker_is_untagged(app, monkeypatch):
    stub_lapi(monkeypatch, decisions="empty", alert_items=[{**ALERT, "decisions": []}])
    html = app.render(core.fetch_activity(force=True))
    assert ">manual (cscli)</span>" not in html and ">sample history</span>" not in html


# ── M-5: the dry-run wording makes no promise beyond the paths it sits on ───────────────────────────────────────
def test_cases_panel_does_not_promise_a_dry_run_everywhere(app):
    html = app._cases_panel([])
    assert "needs an enable flag" in html and "this execute step is a <b>dry run</b>" in html and "nothing is blocked" in html
    assert "unless SENTINEL_BLOCK_ENABLED is set" not in html


# ── owner decisions: removals ───────────────────────────────────────────────────────────────────────────────────
def test_no_benchmark_comparison_on_any_product_surface(app, monkeypatch):
    banner = app._cr_banner()
    readme = (APPDIR / "README.md").read_text()
    for text, where in ((banner, "sticky banner"), (readme, "README")):
        for bad in ("0.900", "0.800", "benchmark", "learned", "learns the cheapest"):
            assert bad.lower() not in text.lower(), f"{where}: {bad!r}"
    stub_lapi(monkeypatch)
    html = page(app)
    assert "0.900" not in html and "0.800" not in html and "benchmark" not in html.lower()
    assert "supply-chain/vuln scanners" not in banner                      # the banner no longer advertises scanners


class _NoDockerScanner:
    def list_scannable_containers(self, name_filter=""):
        return []                                                          # no Docker access: nothing can be listed


def test_scan_chat_actions_answer_that_they_are_unavailable_when_there_is_no_docker_access(app, monkeypatch):
    monkeypatch.setattr(app, "_SCAN", _NoDockerScanner())
    for tool, args in ((app._t_scan_app, {"app": "billing"}), (app._t_scan_apps, {}), (app._t_list_apps, {})):
        out = tool(args)
        assert "not available in this deployment" in out["text"] and "planned" in out["text"], tool.__name__
        assert "Scannable apps" not in out["text"] and out["data"] == {"available": False}


def test_readme_examples_do_not_mount_the_docker_socket_and_give_least_privilege_guidance():
    readme = (APPDIR / "README.md").read_text()
    assert "-v /var/run/docker.sock" not in readme and "docker.sock:/var/run/docker.sock" not in readme
    assert "DOCKER_HOST=tcp://sentinel-docker-proxy" not in readme and "sentinel-docker-scan" not in readme
    assert "## Docker access (least privilege)" in readme and "does not need unrestricted Docker access" in readme
    assert "operator-supplied" in readme and "does not ship or support one" in readme
    for endpoint in ("GET /_ping", "POST /containers/<crowdsec container>/exec", "POST /exec/<id>/start", "GET /images/<ref>/json"):
        assert endpoint in readme, endpoint
    assert "not read-only" in readme and "may hold secrets" in readme            # the limits of a path allow-list are stated
    assert "planned" in readme


def test_no_default_compose_service_for_edge_sentinel_mounts_the_docker_socket():
    yaml = pytest.importorskip("yaml")
    checked = 0
    for f in list(REPO.glob("*compose*.y*ml")) + list((REPO / "deploy").glob("*compose*.y*ml")):
        doc = yaml.safe_load(f.read_text()) or {}
        for name, svc in (doc.get("services") or {}).items():
            if "edge-sentinel" in name:
                checked += 1
                assert not any("docker.sock" in str(v) for v in (svc.get("volumes") or [])), f"{f.name}:{name}"
    assert checked >= 1


def test_apps_readme_no_longer_tells_edge_sentinel_to_mount_the_socket():
    f = REPO / "apps" / "README.md"
    if not f.exists():
        pytest.skip("apps/README.md is in the public tree only")
    line = next(ln for ln in f.read_text().splitlines() if ln.strip().startswith("- edge-sentinel"))
    assert "docker CLI used for" not in line and "not the raw socket" in line


# ── retest: malformed alert body, overlapping requests, per-source stamps, not-read card bodies ───────────────
def test_decisions_ok_with_a_malformed_alert_body_is_not_a_read(app, monkeypatch):
    stub_lapi(monkeypatch, decisions="ok", alerts="malformed")
    data = core.fetch_activity(force=True)
    assert data["decisions_status"]["ok"] is True and data["alerts_status"]["ok"] is False
    assert "malformed" in data["alerts_status"]["note"] or "not JSON" in data["alerts_status"]["note"]
    assert data["alerts_read_at"] == "" and data["read_at"] == "" and data["decisions_read_at"]
    banner = app._status_banner(data)
    assert "alert feed could not be read" in banner and "Latest alert" not in banner
    assert {k["label"]: k["value"] for k in data["kpis"]}["Alerts (24h)"] == "not read"


def test_overlapping_requests_do_not_swap_each_others_read_status(app, monkeypatch):
    """Request A reads a 401 on decisions; while it is still between its read and its status lookup, request B finishes a
    successful read. A must still report its own 401 (the status is per call, not a module global)."""
    stub_lapi(monkeypatch)                                              # decisions answer 200 by default
    ok_get = core.httpx.get

    def get(url, **kw):
        if url.endswith("/v1/decisions") and threading.current_thread().name == "MainThread":
            return _R(401, {"message": "no"})                           # A (this thread) is refused
        return ok_get(url, **kw)
    monkeypatch.setattr(core.httpx, "get", get)
    real, other = core.fetch_decisions, {}

    def racing():
        data = real()                                                   # A: 401, recorded for A
        th = threading.Thread(name="B", target=lambda: (real(), other.update(b=core._own_read("decisions"))))
        th.start(); th.join()                                           # B finishes OK in the gap, overwriting the globals
        return data
    monkeypatch.setattr(core, "fetch_decisions", racing)
    a_data, a_status = core.read_decisions()
    assert other["b"]["ok"] is True and core.decisions_status()["ok"] is True      # the shared store now says "ok"
    assert a_data == [] and a_status["ok"] is False and "HTTP 401" in a_status["note"]   # A still reports its own failure
    core._CACHE.update(ts=0.0, data=None)
    data = core.fetch_activity(force=True)                              # the same through the whole payload
    assert data["decisions_status"]["ok"] is False and {k["label"]: k["value"] for k in data["kpis"]}["Active decisions"] == "not read"


def test_each_source_is_stamped_when_its_own_read_finished(app, monkeypatch):
    stub_lapi(monkeypatch, alert_delay=1.1)                             # a slow alert read must not skew the decisions stamp
    data = core.fetch_activity(force=True)
    assert data["decisions_read_at"] and data["alerts_read_at"] and data["decisions_read_at"] < data["alerts_read_at"]
    assert data["read_at"] == data["alerts_read_at"]                    # read_at is the later of the two


def test_card_bodies_say_not_read_not_empty_when_a_source_failed(app, monkeypatch):
    stub_lapi(monkeypatch, decisions="401", alerts="fail")
    html = page(app)
    assert "Decisions not read (" in html and "Alerts not read (" in html and "Alerts not read, so no scenarios are shown" in html
    for empty in ("No active decisions.", "No alerts in the CrowdSec store.", "No scenarios recorded."):
        assert empty not in html, empty
    last = {k["label"]: k["value"] for k in core.fetch_activity()["kpis"]}["Last event"]
    assert last == "not read"
    stub_lapi(monkeypatch, decisions="empty", alert_items=[])
    ok_html = page(app)
    assert "No active decisions." in ok_html and "No alerts in the CrowdSec store." in ok_html   # a real empty read still says so


def test_block_execution_wording_is_scoped_to_this_ui(console):
    texts = [console.primer, console.tools["ban_ip"]["description"], (APPDIR / "README.md").read_text()]
    for text in texts:
        assert not re.search(r"executing (a block|it|a real block) needs", text), text[:80]
    assert "executing a block from this UI needs an operator credential and an enable flag" in console.tools["ban_ip"]["description"]
    assert "executing it from this UI needs" in console.primer


def test_readme_proxy_limits_include_image_export():
    readme = (APPDIR / "README.md").read_text()
    assert "GET /images/<ref>/get" in readme and "baked-in secrets" in readme


@pytest.mark.skipif(not PUBLIC_TREE, reason="the public tree's Dockerfile and core comments")
def test_public_dockerfile_and_core_comments_do_not_tell_the_reader_to_mount_the_socket():
    for f in ("Dockerfile", "core.py"):
        text = (APPDIR / f).read_text()
        assert "mount /var/run/docker.sock" not in text and "via the mounted /var/run/docker.sock" not in text, f
        assert "docker socket mounted" not in text, f
    assert "do not mount the raw /var/run/docker.sock" in (APPDIR / "Dockerfile").read_text()

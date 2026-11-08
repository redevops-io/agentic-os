"""Every integration boundary is a boundary, not only CrowdSec (PR #613 review, finding 1).

The review put its hands on a real hole: with the fixture core installed and `SENTINEL_BLOCK_ENABLED=1` —
the most permissive write configuration the app has, and the one a realistic demo uses —
`approve_network_policy` reached NetBird's hosted management API and created a real policy, while the page
went on saying "synthetic". The existing tripwire stayed green because it only ever walked CrowdSec.

So the claim under test here is the one a reader actually takes from a labelled demo — *nothing in this
deployment touches a live service* — and it is tested in both directions:

* demo ON, integration configured, execution enabled → no bytes leave, and the refusal is a policy
  decision (403), not an outage (FAILED receipt);
* demo OFF, integration configured → the integration is still reached, because a "fix" that disabled
  NetBird would pass the first half by destroying the product.
"""
from __future__ import annotations

import base64
import importlib
import types

import pytest
from fastapi.testclient import TestClient

core = importlib.import_module("edge-sentinel.core")
fixture_mode = importlib.import_module("edge-sentinel.fixture_mode")
netbird = importlib.import_module("edge-sentinel.netbird")
portabase = importlib.import_module("edge-sentinel.portabase")
deploy_scan = importlib.import_module("edge-sentinel.deploy_scan")


@pytest.fixture
def demo():
    fixture_mode.uninstall()
    yield fixture_mode
    fixture_mode.uninstall()


#: (label, the call that would leave the process, and the module attributes that make the call *possible*.
#: These are patched as attributes, not environment variables: every one of these integrations reads its
#: configuration once at import time into a module constant, so setting os.environ in a test would change
#: nothing and the call would do nothing for reasons unrelated to the boundary — a test passing for exactly
#: the wrong reason).
_EGRESS_PATHS = (
    ("netbird-read", lambda: netbird.access_review({}),
     {"netbird.NETBIRD_API_URL": "https://api.netbird.io/api", "netbird.NETBIRD_API_TOKEN": "nb-token"}),
    ("netbird-write", lambda: netbird.approve({"draft": {"name": "demo", "rules": []}}),
     {"netbird.NETBIRD_API_URL": "https://api.netbird.io/api", "netbird.NETBIRD_API_TOKEN": "nb-token"}),
    ("portabase-read", lambda: portabase.backup_status({}),
     {"portabase.PORTABASE_API_URL": "https://api.portabase.io", "portabase.PORTABASE_API_TOKEN": "pb-token"}),
    ("deploy-scan-write", lambda: deploy_scan.scan({"target": "https://app.example"}),
     {"deploy_scan.DEPLOY_SCAN_MCP_URL": "https://deploy-scan.example/mcp"}),
    ("deploy-scan-read", lambda: deploy_scan.status({"job_id": "job-1"}),
     {"deploy_scan.DEPLOY_SCAN_MCP_URL": "https://deploy-scan.example/mcp"}),
)


def _configure(monkeypatch, attrs):
    """Point an integration at a URL it cannot possibly reach, with a credential that makes it *configured*."""
    for dotted, value in attrs.items():
        mod_name, attr = dotted.rsplit(".", 1)
        monkeypatch.setattr(importlib.import_module(f"edge-sentinel.{mod_name}"), attr, value)



@pytest.fixture
def egress_tripwire(monkeypatch):
    """Every way this app is known to leave the process, replaced by an alarm.

    `httpx` is a single module object shared by all importers, so patching its attributes covers core,
    netbird, portabase, deploy_scan and app at once. The raw socket is patched too, because ClamAV is
    reached with a TCP connection rather than through httpx — an egress path an httpx-only tripwire is
    structurally blind to. Subprocess covers the `cscli`/Docker route."""
    import httpx as _httpx
    import socket as _socket

    hits: list = []

    class _Alarm:
        def __init__(self, where):
            self.where = where

        def __call__(self, *args, **kwargs):
            hits.append((self.where, args[:1]))
            raise AssertionError(f"demo mode tried to leave the process via {self.where}: {args[:1]}")

    for name in ("get", "post", "put", "delete", "patch", "Client", "AsyncClient"):
        monkeypatch.setattr(_httpx, name, _Alarm(f"httpx.{name}"))
    monkeypatch.setattr(_socket, "create_connection", _Alarm("socket.create_connection"))
    for name in ("run", "Popen", "check_output", "call"):
        monkeypatch.setattr(core.subprocess, name, _Alarm(f"subprocess.{name}"))
    return hits


# ── demo ON: nothing leaves, and the answer stays honest ──────────────────────────────────────────────

_READS = tuple(p for p in _EGRESS_PATHS if p[0].endswith("-read"))
_WRITES = (("netbird-write", _EGRESS_PATHS[1][1], _EGRESS_PATHS[1][2]),
           ("deploy-scan-write", _EGRESS_PATHS[3][1], _EGRESS_PATHS[3][2]))


@pytest.mark.parametrize("label,call,attrs", _READS, ids=[p[0] for p in _READS])
def test_demo_mode_reads_no_integration_and_says_so(demo, egress_tripwire, monkeypatch, label, call, attrs):
    """A refused read must degrade to the module's *labelled sample* shape. Returning live-looking numbers
    from a refused read would be worse than an error: the page would look like a reviewed environment."""
    _configure(monkeypatch, attrs)
    demo.install({})
    out = call()
    assert egress_tripwire == [], f"{label}: demo mode reached the integration: {egress_tripwire}"
    text = out.get("text", "") if isinstance(out, dict) else str(out)
    data = out.get("data", {}) if isinstance(out, dict) else {}
    # Either label is acceptable — each module has its own honest "this was not read" wording — but the
    # answer must carry one of them, and must never claim a live read.
    assert fixture_mode.SYNTHETIC_TAG in text or "SAMPLE DATA" in text, \
        f"{label}: a refused read presented itself without any not-read label: {text[:140]}"
    assert data.get("live") is not True and data.get("data_status") != "live", f"{label}: claims a live read"


@pytest.mark.parametrize("label,call,attrs", _WRITES, ids=[p[0] for p in _WRITES])
def test_demo_mode_refuses_every_integration_write(demo, egress_tripwire, monkeypatch, label, call, attrs):
    """Configured, execution-enabled, installed: the write still must not leave. `SENTINEL_BLOCK_ENABLED=1`
    is set because that is the configuration the original hole needed."""
    _configure(monkeypatch, attrs)
    monkeypatch.setenv("SENTINEL_BLOCK_ENABLED", "1")
    demo.install({})
    with pytest.raises(fixture_mode.DemoRefused) as e:
        call()
    assert "no external side effect is permitted" in str(e.value)
    assert egress_tripwire == [], f"{label}: the refusal happened after the bytes left: {egress_tripwire}"


def test_demo_mode_opens_no_clamd_socket(demo, egress_tripwire, load_app, monkeypatch):
    """ClamAV is reached over a raw socket, so it is invisible to an httpx-only tripwire — and a scan is the
    app asking another container to walk a host filesystem."""
    monkeypatch.setattr(importlib.import_module("edge-sentinel.app"), "CLAMAV_HOST", "192.0.2.9")
    demo.install({})
    out = load_app("edge-sentinel")._t_malware_scan({"path": "/scan"})
    assert out["data"].get("refused") == "demo_mode", out
    assert fixture_mode.SYNTHETIC_TAG in out["text"], "and it must be legible as a demo refusal, not an outage"
    assert egress_tripwire == []


def test_demo_mode_makes_no_model_call_from_either_path(demo, load_app, monkeypatch):
    """Two independent roads lead to the model: the triage blurb and the console's own turn loop. Patching
    only the road someone remembered is not a boundary."""
    demo.install({})
    app_module = load_app("edge-sentinel")
    calls: list = []
    kernel_llm = importlib.import_module("agentic_os.app.llm")
    monkeypatch.setattr(kernel_llm, "governed_text", lambda *a, **k: calls.append(a) or "unreachable")
    assert app_module._llm_blurb("summarise the case") is None
    with pytest.raises(fixture_mode.DemoRefused):
        app_module._GovernedConsoleModel().complete(
            types.SimpleNamespace(messages=[{"content": "hi"}], system="", max_tokens=10))
    assert calls == [], "demo mode reached the model"


# ── the route and the receipt: a refusal is a decision, not an outage ─────────────────────────────────

def test_the_mutating_route_refuses_with_403_and_no_egress(demo, egress_tripwire, load_app, monkeypatch):
    """The review's reproduction, at the route: operator authenticated, execution enabled, NetBird
    configured, demo on. Before the fix this created a policy at api.netbird.io."""
    monkeypatch.setenv("SENTINEL_BLOCK_ENABLED", "1")
    monkeypatch.setenv("SENTINEL_BASIC_AUTH_USER", "ops")
    monkeypatch.setenv("SENTINEL_BASIC_AUTH_PASS", "correct-horse-battery")
    _configure(monkeypatch, {"netbird.NETBIRD_API_URL": "https://api.netbird.io/api",
                             "netbird.NETBIRD_API_TOKEN": "nb-token"})
    demo.install({})
    c = TestClient(load_app("edge-sentinel").app)
    basic = "Basic " + base64.b64encode(b"ops:correct-horse-battery").decode()
    # /agent/run is the action dispatcher; /api/agent is the conversational console, which never mutates.
    r = c.post("/agent/run", json={"action": "approve_network_policy",
                                   "draft": {"name": "demo-tighten", "rules": []}},
               headers={"Authorization": basic})
    assert r.status_code == 403, r.text
    assert r.json()["refused"] == "demo_mode", "a policy decision must not look like a client or server error"
    assert egress_tripwire == [], f"the route reached NetBird: {egress_tripwire}"


def test_a_refused_demo_leaves_no_case_state_and_no_receipt(demo):
    """J5's lesson applied to the new boundary: because the refusal happens before `live.execute` writes,
    the case stays APPROVED and no FAILED receipt is recorded."""
    demo.install({})
    live = importlib.import_module("edge-sentinel.live")
    CaseStore = importlib.import_module("edge-sentinel.case_store").CaseStore
    st = CaseStore()
    live.sync_cases(st, alerts_fn=lambda: [{"scenario": "crowdsecurity/ssh-bf",
                                            "source": {"value": "192.0.2.73", "scope": "Ip"},
                                            "created_at": "2026-11-07T10:00:00Z", "events_count": 9}])
    cid = next(iter(st.cases))
    rid = live.case_summaries(st)[0]["open_action"]["request_id"]
    live.decide(cid, rid, approved=True, actor="operator:test", st=st)
    before = live.case_summaries(st)[0]["open_action"]["state"]
    with pytest.raises(fixture_mode.DemoRefused):
        live.execute(cid, rid, actor="operator:test", st=st, enabled=True)
    assert live.case_summaries(st)[0]["open_action"]["state"] == before
    assert st.receipts == {}, "a refused demo must not record a receipt"


# ── the same paths in the other direction: production still reaches the integrations ──────────────────

@pytest.mark.parametrize("label,call,attrs", _EGRESS_PATHS, ids=[p[0] for p in _EGRESS_PATHS])
def test_production_still_reaches_the_integration_when_demo_is_off(demo, monkeypatch, label, call, attrs):
    """The more important half of the change. A boundary implemented as "never call NetBird" would satisfy
    every refusal test above and destroy the product; this test fails if an integration stops being reached
    with demo mode off."""
    _configure(monkeypatch, attrs)
    demo.uninstall()
    attempts: list = []
    import httpx

    class _Ok:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"result": {"structuredContent": {"job_id": "job-1", "state": "running"}}}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: attempts.append(("get", a[:1])) or _Ok())
    monkeypatch.setattr(httpx, "post", lambda *a, **k: attempts.append(("post", a[:1])) or _Ok())
    try:
        call()
    except Exception:  # noqa: BLE001 — a stub payload need not satisfy every caller; the attempt is the point
        pass
    assert attempts, f"{label}: with demo mode off the integration is never reached — production changed"


# ── the claim a reader sees must match the boundary the code enforces ─────────────────────────────────

def test_health_names_the_scope_of_the_boundary(demo, load_app):
    """`scopes` exists so a reader of the evidence can see what the synthetic label covers, instead of
    inferring it from whichever panels happen to be on screen."""
    demo.install({})
    payload = TestClient(load_app("edge-sentinel").app).get("/health").json()
    assert set(payload["demo"]["scopes"]) == set(fixture_mode.DEMO_SCOPES)


def test_the_banner_claims_only_what_the_boundary_enforces(demo, load_app):
    """Finding 1's documentation half. The sentence used to name CrowdSec alone on a page that also showed
    NetBird and backups; the fix widens the enforcement and this pins the sentence to it, so the claim can
    only ever be as wide as the code — never wider."""
    demo.install({})
    body = TestClient(load_app("edge-sentinel").app).get("/").text
    assert fixture_mode.SYNTHETIC_TAG in body
    for integration in ("CrowdSec", "NetBird", "Portabase", "deploy-scan", "Wazuh", "ClamAV"):
        assert integration in body, f"the banner omits {integration} from what demo mode holds back"
    assert fixture_mode.DEMO_ENV in body, "and it must say how to leave demo mode"


def test_every_declared_scope_is_backed_by_a_real_refusal(demo):
    """A scopes list naming something nothing refuses would be a new overstatement of exactly the kind this
    change exists to remove."""
    demo.install({})
    try:
        for scope in fixture_mode.DEMO_SCOPES:
            assert fixture_mode.read_refusal(scope) is not None, scope
        for scope in ("crowdsec", "netbird", "deploy_scan"):
            with pytest.raises(fixture_mode.DemoRefused):
                fixture_mode.refuse_if_enabled(scope)
    finally:
        demo.uninstall()
    assert fixture_mode.read_refusal("anything") is None, "and with demo off nothing is refused"




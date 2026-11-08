"""The route-policy enforcement posture: profile-gated today, plus an opt-in exposure gate.

Background, measured rather than assumed. `route_authz` computes a policy for every write route and acts on it
only under the production profile, while `deploy/apps/compose.yml` passes `AGENTIC_OS_PROFILE` through unset,
whose documented meaning is "development, with a warning". Of the 81 write-route policies declared across the
kernel and the 17 apps, the 29 that can DENY (`authority_required`, `human_only`) deny nothing on a default
`docker compose up`, and a write route with no policy at all is logged rather than refused. The other 52 are
`stages_only`, `operator_boundary`, `read_only` and `public_intake`, which `decide()` never refuses — they are
unaffected either way, which is why the public demo keeps working.

`AGENTIC_OS_ROUTE_AUTHZ_ENFORCE=exposure` is the proposed narrow fix: enforce whenever the connection did not
arrive over loopback. It is opt-in here because the production profile also mandates a durable authority store
and an owner envelope, so switching the shipped default is a deployment decision, not a code change. These
tests pin both the current behaviour and the opt-in behaviour, so whichever way the owner decides, the seam is
covered.
"""
from __future__ import annotations

import secrets as _secrets

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from agentic_os.app_kit import route_authz as ra
from agentic_os.app_kit.reachability import arrived_on_loopback
from agentic_os.app_kit.route_authz import RoutePolicy as P
from agentic_os.mission.approval import ReplayGuard

APP = "demo"
SECRET = _secrets.token_hex(24)

LOOPBACK = "http://127.0.0.1:8080"
EXTERNAL = "http://10.0.0.7:8080"      # what an app sees when Caddy forwards to it on the compose network


def _app():
    app = FastAPI()
    calls: list[str] = []

    def handler_factory(name):
        async def handler(request: Request):
            await request.body()
            calls.append(name)
            return {"ok": True}
        return handler

    for path in ("/act", "/intake", "/run"):
        app.post(path)(handler_factory(path))

    @app.get("/read")
    async def read_route():
        return {"ok": True}

    ra.install(app, APP, {
        "POST /act": P("demo.act", "authority_required"),
        "POST /intake": P("demo.intake", "public_intake", rationale="a public form; records only, rate limited"),
        "POST /run": P("demo.run", "stages_only", rationale="stages a preview unless handed an approval marker"),
    }, replay_guard=ReplayGuard())
    return app, calls


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("AGENTIC_OS_PROFILE", "AGENTIC_OS_REQUIRE_AUTHORITY", ra.ENV_ENFORCE,
              "AGENTIC_OS_SERVICE_TOKENS", "AGENTIC_OS_TENANT_TOKENS", "OPERATOR_BASIC_AUTH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENTIC_OS_APPROVAL_SECRET", SECRET)


def _client(base_url):
    app, calls = _app()
    return TestClient(app, base_url=base_url, raise_server_exceptions=False), calls


@pytest.mark.parametrize("host,loopback", [("127.0.0.1", True), ("::1", True), ("127.0.0.53", True),
                                           ("10.0.0.7", False), ("172.17.0.4", False), ("0.0.0.0", False),
                                           ("::", False), ("testserver", False), ("", False)])
def test_reachability_is_decided_by_ip_not_by_name(host, loopback):
    assert arrived_on_loopback({"server": (host, 8080)}) is loopback


def test_a_scope_without_a_server_address_counts_as_exposed():
    assert arrived_on_loopback({}) is False
    assert arrived_on_loopback({"server": None}) is False



# ── today's behaviour, pinned so a future default flip is a deliberate act ─────────────────────────────

def test_a_deniable_write_is_not_actually_refused_while_the_profile_is_unset():
    """The finding, as an executable statement: an anonymous write to an authority_required route reaches the
    handler on a default deployment. If this test starts failing, enforcement has become the default — which
    is the point, and it should be loud rather than silent."""
    c, calls = _client(EXTERNAL)
    assert c.post("/act", json={}).status_code == 200
    assert calls == ["/act"], "the handler ran, and that is the gap"


# ── the proposed posture: reachability decides, not memory ─────────────────────────────────────────────

def test_exposure_mode_refuses_the_anonymous_write_from_outside_the_host(monkeypatch):
    monkeypatch.setenv(ra.ENV_ENFORCE, "exposure")
    c, calls = _client(EXTERNAL)
    assert c.post("/act", json={}).status_code == 401
    assert calls == [], "a refused write must not reach the handler"


def test_exposure_mode_still_lets_the_same_call_through_from_loopback(monkeypatch):
    """Local development is unchanged: a developer on 127.0.0.1 keeps the permissive profile behaviour, so the
    fix does not push people toward disabling enforcement wholesale to get work done."""
    monkeypatch.setenv(ra.ENV_ENFORCE, "exposure")
    c, calls = _client(LOOPBACK)
    assert c.post("/act", json={}).status_code == 200
    assert calls == ["/act"]


def test_production_enforces_on_loopback_too(monkeypatch):
    """The exposure gate is additive: it never loosens production, where localhost is not a credential."""
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    for base in (LOOPBACK, EXTERNAL):
        c, calls = _client(base)
        assert c.post("/act", json={}).status_code == 401, base
        assert calls == [], base


def test_a_write_route_with_no_policy_is_refused_from_outside(monkeypatch):
    monkeypatch.setenv(ra.ENV_ENFORCE, "exposure")
    app, _ = _app()

    async def undeclared(request: Request):
        return {"ok": True}

    app.post("/undeclared")(undeclared)
    c = TestClient(app, base_url=EXTERNAL, raise_server_exceptions=False)
    r = c.post("/undeclared", json={})
    assert r.status_code == 403 and "no authorization policy" in r.text


def test_reads_and_the_public_classes_stay_open_in_exposure_mode(monkeypatch):
    """The public demo journeys are reads and public intake; `decide()` never refuses them, in any profile.
    This is the test that keeps a fail-closed default from turning into a blanket authentication wall."""
    monkeypatch.setenv(ra.ENV_ENFORCE, "exposure")
    c, calls = _client(EXTERNAL)
    assert c.get("/read").status_code == 200
    assert c.post("/intake", json={"email": "subject@example.com"}).status_code == 200, "public intake is public by design"
    assert c.post("/run", json={}).status_code == 200, "stages_only stages a preview without a marker"
    assert calls == ["/intake", "/run"]


def test_enforcement_is_unchanged_when_the_new_variable_is_absent():
    """The commit that introduces this must change nothing by default."""
    assert ra.enforces({"server": ("10.0.0.7", 80)}) is False
    assert ra.enforces({"server": ("127.0.0.1", 80)}) is False


# ── the deployment gap, encoded so it cannot be quietly re-introduced ─────────────────────────────────

_XF = """A shipped compose file publishes a port while leaving AGENTIC_OS_PROFILE unset.

That combination IS the route-policy bypass: the service is reachable from outside its host and enforces none
of its declared write policies. Closing it means choosing between `AGENTIC_OS_PROFILE=production` (which also
requires a durable authority store and an owner envelope, so it is an operator provisioning decision) and
`AGENTIC_OS_ROUTE_AUTHZ_ENFORCE=exposure`. This test is strict-xfail: it fails-as-expected while the shipped
files are unchanged, and it turns into a hard failure the moment any one file is fixed — which is the signal
to finish the job across every published service rather than leave a half-enforced fleet."""


def _published_services():
    import pathlib

    import yaml
    root = pathlib.Path(__file__).resolve().parents[1]

    class _Loader(yaml.SafeLoader):
        pass

    # Compose files carry their own short tags (!reset, !override). They say nothing about enforcement, so
    # they are loaded as an empty value rather than making the audit unable to read the file at all.
    _Loader.add_multi_constructor("!", lambda loader, suffix, node: [])
    files = sorted(root.glob("deploy/**/compose*.yml")) + sorted(root.glob("docker-compose*.y*ml"))
    out = []
    for f in files:
        doc = yaml.load(f.read_text(), Loader=_Loader) or {}
        for name, svc in (doc.get("services") or {}).items():
            if not (svc.get("ports") or []):
                continue
            env = " ".join(str(e) for e in (svc.get("environment") or []))
            out.append((str(f.relative_to(root)), name, env))
    return out


def test_the_audit_counts_published_services_before_blaming_the_code():
    """The xfail below needs real published services to mean anything; this one keeps it honest."""
    assert len(_published_services()) >= 5


@pytest.mark.xfail(strict=True, reason=_XF)
def test_no_shipped_compose_publishes_a_port_with_the_profile_left_unset():
    offenders = [(f, name) for f, name, env in _published_services()
                 if "AGENTIC_OS_PROFILE=" not in env or "${AGENTIC_OS_PROFILE:-}" in env]
    assert not offenders, f"{len(offenders)} published service(s) run with no enforcement posture: {offenders[:6]}"

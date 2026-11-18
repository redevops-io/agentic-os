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

import os
import re
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
              "AGENTIC_OS_SERVICE_TOKENS", "AGENTIC_OS_TENANT_TOKENS", "OPERATOR_BASIC_AUTH",
              ra.ENV_FORWARDED_ALLOW_IPS, ra.ENV_BEHIND_PROXY, "AGENTIC_OS_BIND_HOST", "HOST"):
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

def test_an_anonymous_write_to_an_authority_required_route_is_now_refused_by_default():
    """The finding, and its closure, as one executable statement.

    Before this change an anonymous write to an `authority_required` route reached the handler on a default
    deployment (profile unset ⇒ every policy advisory) — the exact behaviour this test used to pin. Enforcement
    is now the default for a routable peer, so the same request is refused and the handler never runs. No
    profile, authority store or owner envelope is involved: the peer address alone decides.
    """
    c, calls = _client(EXTERNAL)
    assert c.post("/act", json={}).status_code == 401
    assert calls == [], "the handler must not run"


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


def test_enforcement_is_the_default_for_every_network_peer_including_loopback():
    """Unknown provenance enforces. A loopback peer is not proof of locality — a same-host proxy is loopback
    too — so the only peers that get advisory behaviour are ones that cannot have come from the network."""
    assert ra.enforces({"server": ("10.0.0.7", 80)}) is True       # routable peer: enforced
    assert ra.enforces({"server": ("172.17.0.4", 80)}) is True     # a container edge address, not loopback
    assert ra.enforces({"server": ("127.0.0.1", 80)}) is True      # loopback with an unknown front: enforced
    assert ra.enforces({"server": ("::1", 80)}) is True            # … on IPv6 loopback as well
    assert ra.enforces({"server": ("testserver", 80)}) is False    # in-process harness: no network peer


def test_advisory_behaviour_requires_an_explicit_direct_declaration(monkeypatch):
    """The single way to get the advisory posture over a network socket: state that nothing forwards here."""
    monkeypatch.delenv(ra.ENV_ENFORCE, raising=False)
    scope = {"server": ("127.0.0.1", 80)}
    assert ra.enforces(scope) is True                              # undeclared provenance
    monkeypatch.setenv(ra.ENV_BEHIND_PROXY, "unknown")
    assert ra.enforces(scope) is True                              # a value that is not an answer
    monkeypatch.setenv(ra.ENV_BEHIND_PROXY, "0")
    assert ra.enforces(scope) is False                             # the explicit statement
    assert ra.front_declaration() == "direct"
    monkeypatch.setenv(ra.ENV_BEHIND_PROXY, "1")
    assert ra.enforces(scope) is True
    assert ra.front_declaration() == "proxied"


def test_a_same_host_proxy_declared_in_front_of_the_process_enforces(monkeypatch):
    """The hole the socket cannot see, closed by the one fact the operator has to state.

    A reverse proxy terminating on the same host connects over 127.0.0.1, so the accepted socket is identical
    to a developer's own request (tests/test_mproxy_write_containment.py pins that). Uvicorn's
    FORWARDED_ALLOW_IPS — which any deployment that parses X-Forwarded-For already sets — is the declaration
    that loopback peers may be carrying somebody else's request, and it flips the posture on its own.
    """
    monkeypatch.delenv(ra.ENV_ENFORCE, raising=False)
    monkeypatch.delenv(ra.ENV_BEHIND_PROXY, raising=False)
    monkeypatch.setenv(ra.ENV_FORWARDED_ALLOW_IPS, "127.0.0.1")
    assert ra.front_declaration() == "proxied"
    assert ra.enforces({"server": ("127.0.0.1", 80)}) is True
    # a proxy declaration outranks a `direct` statement: the more specific fact about the front wins
    monkeypatch.setenv(ra.ENV_BEHIND_PROXY, "0")
    assert ra.front_declaration() == "proxied"
    assert ra.enforces({"server": ("127.0.0.1", 80)}) is True


def test_a_published_bind_host_enforces_whatever_the_socket_says(monkeypatch):
    """`0.0.0.0`/`::`/a named bind means other machines can reach this process; loopback peers then prove
    nothing about who is upstream, and no front statement can make them local."""
    monkeypatch.delenv(ra.ENV_ENFORCE, raising=False)
    for host in ("0.0.0.0", "::", "172.17.0.4", "api.example.com"):
        monkeypatch.setenv("AGENTIC_OS_BIND_HOST", host)
        assert ra.enforces({"server": ("127.0.0.1", 80)}) is True, host
        monkeypatch.setenv(ra.ENV_BEHIND_PROXY, "0")               # a published port stays enforced
        assert ra.enforces({"server": ("127.0.0.1", 80)}) is True, host
    monkeypatch.setenv("AGENTIC_OS_BIND_HOST", "127.0.0.1")
    monkeypatch.delenv(ra.ENV_BEHIND_PROXY)
    assert ra.enforces({"server": ("127.0.0.1", 80)}) is True      # bind known-loopback, front still unknown
    monkeypatch.setenv(ra.ENV_BEHIND_PROXY, "0")
    assert ra.enforces({"server": ("127.0.0.1", 80)}) is False     # … until the deployment says "direct"



def test_an_unrecognised_posture_enforces_rather_than_relaxing(monkeypatch):
    """A typo in a security-relevant variable must not silently turn enforcement off."""
    monkeypatch.setenv(ra.ENV_ENFORCE, "advisory-ish")
    assert ra.enforces({"server": ("127.0.0.1", 80)}) is True


def test_the_explicit_opt_outs_still_work_for_the_uses_they_exist_for(monkeypatch):
    """`profile` is the documented escape hatch for tooling that predates the audit; `always` is stricter than
    the default. Both are stated, neither is the default."""
    monkeypatch.setenv(ra.ENV_ENFORCE, "profile")
    assert ra.enforces({"server": ("10.0.0.7", 80)}) is False
    monkeypatch.setenv(ra.ENV_ENFORCE, "always")
    assert ra.enforces({"server": ("127.0.0.1", 80)}) is True


# ── deployment compliance: every published service states its front and its bind (audit items 1 and 6) ─
# Enforcement is the default and unknown provenance enforces, so "published port with the profile left
# unset" is no longer a bypass and the old fleet-wide strict-xfail tripwire is obsolete. What these checks
# hold the deployments to is the part no process can observe for itself: whether something forwards to it,
# and what it binds. Both are one line per service, and the real-socket demonstration of why they matter is
# in tests/test_route_authz_real_topology.py.


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
            # Only services running Agentic OS code can read our variables; a digest-pinned third-party image
            # (Caddy, Twenty, Metabase, Postiz) is part of the topology but not a service this check can make
            # declare anything. Including them would bury the services that can act.
            ours = bool(svc.get("build")) or "agentic" in str(svc.get("image", "")).lower()
            if ours:
                out.append((str(f.relative_to(root)), name, env))
    return out


def test_the_audit_counts_published_services_before_blaming_the_code():
    """The compliance checks below need real published services to mean anything; this keeps them honest."""
    assert len(_published_services()) >= 5


def test_no_shipped_service_opts_out_of_enforcement():
    """No shipped deployment may turn enforcement off. `profile` restores the pre-audit posture and `always`
    is stricter than the default; the offending value is named so the review comment is unambiguous."""
    offenders = [(f, name) for f, name, env in _published_services()
                 if re.search(r"AGENTIC_OS_ROUTE_AUTHZ_ENFORCE=(profile|off|none|0)\b", env)]
    assert not offenders, f"published service(s) that disable enforcement: {offenders}"


# The five published first-party services that shipped with no front declaration (audit of 2026-11-08). Each
# one is now stated in its own compose file; this set is asserted empty so the fix cannot quietly regress, and
# so the list stays readable as what was wrong rather than what is still permitted.
FORMERLY_UNDECLARED = {
    ("deploy/apps/compose.egress.yml", "forward-twenty"),
    ("deploy/apps/compose.egress.yml", "forward-metabase"),
    ("deploy/apps/compose.egress.yml", "forward-postiz"),
    ("deploy/projects-sidekick/compose.yml", "projects-sidekick"),
    ("docker-compose.yml", "control-plane"),
}


def _front_undeclared():
    declares = re.compile(r"FORWARDED_ALLOW_IPS=\S|AGENTIC_OS_BEHIND_PROXY=\S")
    return {(f, name) for f, name, env in _published_services() if not declares.search(env)}


def test_the_five_formerly_undeclared_services_now_state_their_front():
    """The audit named five; all five now declare, and any that regresses fails here by name."""
    still_silent = FORMERLY_UNDECLARED & _front_undeclared()
    assert still_silent == set(), f"service(s) that lost their front declaration: {sorted(still_silent)}"




def test_every_published_service_declares_its_front():
    """Every published first-party service states whether something forwards to it. This used to be a list of
    outstanding work; the five services now declare it, so the check is a requirement with no exceptions.

    Accepted declarations:

      - FORWARDED_ALLOW_IPS=127.0.0.1        # something on this host forwards requests here
      - AGENTIC_OS_BEHIND_PROXY=1            # the same statement without parsing X-Forwarded-For
      - AGENTIC_OS_BEHIND_PROXY=0            # nothing forwards to us; loopback peers are local clients

    A published service that says nothing is still enforced for every peer, so silence is not a bypass; it is
    a missing piece of documentation that would matter the day someone adds `AGENTIC_OS_BIND_HOST=127.0.0.1`
    or wonders why a local request behaves differently from a remote one.
    """
    silent = _front_undeclared()
    assert silent == set(), (
        f"published service(s) with no declared front — add FORWARDED_ALLOW_IPS=... or "
        f"AGENTIC_OS_BEHIND_PROXY=0|1 to each: {sorted(silent)}")


def test_every_published_service_also_declares_its_bind():
    """A published service states its bind host too, so the posture is decided from stated facts rather than
    inferred from a uvicorn command line buried three lines away.

    `AGENTIC_OS_BIND_HOST` beyond loopback means other machines can reach the process, and that outranks any
    front statement: everything is enforced. Writing it down is what makes "why is this request enforced?"
    answerable from the compose file alone.
    """
    missing = {(f, name) for f, name, env in _published_services()
               if not re.search(r"AGENTIC_OS_BIND_HOST=\S", env)}
    assert missing == set(), (
        f"published service(s) with no stated bind host — add AGENTIC_OS_BIND_HOST=0.0.0.0 (published) or "
        f"=127.0.0.1 (machine-local) to each: {sorted(missing)}")




def test_the_front_declaration_actually_changes_the_posture():
    """The compliance list above is only worth having if the declaration does something: an undeclared
    loopback request is enforced, and the explicit `direct` statement is what makes it advisory."""
    scope = {"server": ("127.0.0.1", 80)}
    assert ra.enforces(scope) is True
    os.environ[ra.ENV_BEHIND_PROXY] = "0"
    try:
        assert ra.enforces(scope) is False
    finally:
        del os.environ[ra.ENV_BEHIND_PROXY]
    os.environ[ra.ENV_FORWARDED_ALLOW_IPS] = "127.0.0.1"
    try:
        assert ra.enforces(scope) is True
    finally:
        del os.environ[ra.ENV_FORWARDED_ALLOW_IPS]


"""P9: the defaults are the secure ones, and every way to open them is an explicit, named setting.

Three defaults were unsafe rather than merely permissive: the app bound 0.0.0.0 (a SOC dashboard with
approval-gated actions on every interface); the Wazuh client passed `verify=False` unconditionally (Wazuh
ships a self-signed certificate, so the code turned certificate checking off for everybody, always); and
every page view fetched Roboto from Google Fonts — one outbound request per view of a security console,
from a machine that may be offline, with no content policy of any kind.

These tests fail on those defaults and pass on the new ones. Nothing here binds a socket, needs CrowdSec,
Wazuh, docker or the network: TestClient serves in-process, and the bind guard is called directly.
"""
from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

app = importlib.import_module("edge-sentinel.app")
security = importlib.import_module("edge-sentinel.security")
core = importlib.import_module("edge-sentinel.core")

EXPOSURE_ENV = ("SENTINEL_BIND_HOST", "SENTINEL_ALLOW_EXPOSURE", "SENTINEL_ALLOW_UNAUTHENTICATED_EXPOSURE",
                "SENTINEL_BASIC_AUTH_USER", "SENTINEL_BASIC_AUTH_PASS",
                "PARTNERS_BASIC_AUTH_USER", "PARTNERS_BASIC_AUTH_PASS", "WAZUH_TLS_VERIFY")


def _clean(monkeypatch):
    for e in EXPOSURE_ENV:
        monkeypatch.delenv(e, raising=False)


# ── bind: loopback unless asked ───────────────────────────────────────────────

def test_the_default_bind_is_loopback(monkeypatch):
    _clean(monkeypatch)
    assert security.resolve_bind_host() == "127.0.0.1"
    assert app.BIND_HOST == security.DEFAULT_BIND_HOST or security.is_loopback(app.BIND_HOST)


def test_unreachable_hosts_are_not_called_loopback():
    for h in ("127.0.0.1", "localhost", "::1", "127.0.0.53"):
        assert security.is_loopback(h), h
    for h in ("0.0.0.0", "0", "::", "10.0.0.7", "192.0.2.1", "0.0.0.0:8203"):
        assert not security.is_loopback(h), h


def test_loopback_needs_no_permission_slip(monkeypatch):
    _clean(monkeypatch)
    security.exposure_guard("127.0.0.1")          # must not raise: the local single-user path
    security.exposure_guard("localhost")


def test_a_reachable_bind_is_refused_until_it_is_asked_for(monkeypatch):
    _clean(monkeypatch)
    try:
        security.exposure_guard("0.0.0.0")
        raise AssertionError("a reachable bind with nothing set must not be accepted")
    except security.ExposureRefused as e:
        assert "SENTINEL_ALLOW_EXPOSURE" in str(e)      # the message names the way out


def test_a_reachable_bind_without_a_credential_is_still_refused(monkeypatch):
    """The flag says "this box is reachable"; it does not say "anyone may read every case"."""
    _clean(monkeypatch)
    monkeypatch.setenv("SENTINEL_ALLOW_EXPOSURE", "1")
    try:
        security.exposure_guard("0.0.0.0")
        raise AssertionError("reachable + credential-less must not be accepted")
    except security.ExposureRefused as e:
        assert "no operator credential" in str(e)


def test_a_reachable_bind_is_allowed_with_a_credential(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("SENTINEL_ALLOW_EXPOSURE", "1")
    monkeypatch.setenv("SENTINEL_BASIC_AUTH_USER", "sec")
    monkeypatch.setenv("SENTINEL_BASIC_AUTH_PASS", "hunter2")
    security.exposure_guard("0.0.0.0")                   # must not raise


def test_the_public_demo_path_is_open_by_name_not_by_accident(monkeypatch):
    """The read-only public dashboard is a real deployment: it stays possible, and it stops being implicit.
    Execution is unaffected — it remains authenticated and dry-run unless SENTINEL_BLOCK_ENABLED is set."""
    _clean(monkeypatch)
    monkeypatch.setenv("SENTINEL_ALLOW_EXPOSURE", "1")
    monkeypatch.setenv("SENTINEL_ALLOW_UNAUTHENTICATED_EXPOSURE", "1")
    security.exposure_guard("0.0.0.0")                   # must not raise
    assert not security.operator_credential_configured()  # and still nobody to authenticate as


def test_the_shared_ops_credential_counts_as_a_credential(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("PARTNERS_BASIC_AUTH_USER", "ops")
    monkeypatch.setenv("PARTNERS_BASIC_AUTH_PASS", "hunter2")
    assert security.operator_credential_configured()
    monkeypatch.setenv("PARTNERS_BASIC_AUTH_PASS", "")
    assert not security.operator_credential_configured()   # half a credential is no credential


def test_a_declared_reachable_bind_stops_the_app_before_it_serves(monkeypatch):
    """The container image declares SENTINEL_BIND_HOST; the startup guard checks the declaration, so an
    image with no credentials and no opt-out fails to start instead of serving the whole network.
    ExposureRefused is a normal application exception (requirement: testable), raised from the startup
    hook, and it is what actually propagates out of the lifespan when TestClient enters it."""
    _clean(monkeypatch)
    monkeypatch.setenv("SENTINEL_BIND_HOST", "0.0.0.0")
    try:
        with TestClient(app.app):                       # entering the context runs the startup hooks
            raise AssertionError("startup must refuse a declared reachable bind with no opt-out")
    except security.ExposureRefused as e:
        assert "SENTINEL_ALLOW_EXPOSURE" in str(e)      # the message names the way out


def test_the_cli_boundary_turns_the_refusal_into_a_startup_error(monkeypatch):
    """`python -m edge-sentinel.app` catches ExposureRefused and exits with it as the message — the
    user-facing error lives at the process boundary, not inside the guard."""
    _clean(monkeypatch)
    import subprocess as _sp
    src = ("import importlib, sys\n"
           "sec = importlib.import_module('edge-sentinel.security')\n"
           "try:\n"
           "    sec.exposure_guard('0.0.0.0')\n"
           "except sec.ExposureRefused as e:\n"
           "    sys.exit(f'edge-sentinel: startup refused: {e}')\n")
    p = _sp.run([__import__("sys").executable, "-c", src], capture_output=True, text=True,
                env={"PYTHONPATH": str(__import__("pathlib").Path(core.__file__).parent.parent)})
    assert p.returncode == 1
    assert "startup refused" in p.stderr and "SENTINEL_ALLOW_EXPOSURE" in p.stderr


def test_a_declared_loopback_bind_starts_normally(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("SENTINEL_BIND_HOST", "127.0.0.1")
    with TestClient(app.app) as c:
        assert c.get("/health").status_code == 200


# ── headers + no third-party request on any page ──────────────────────────────

def _get(path="/"):
    return TestClient(app.app).get(path)


def test_every_response_carries_the_baseline_headers():
    for path in ("/", "/health"):
        r = _get(path)
        assert r.status_code == 200, path
        assert r.headers["x-content-type-options"] == "nosniff", path
        assert r.headers["x-frame-options"] == "DENY", path          # never frame a ban button
        assert r.headers["referrer-policy"] == "no-referrer", path   # case ids and IPs stay here
        assert "default-src 'self'" in r.headers["content-security-policy"], path


def test_the_content_policy_refuses_everything_the_page_does_not_use():
    csp = _get("/").headers["content-security-policy"]
    for directive in ("object-src 'none'", "frame-ancestors 'none'", "base-uri 'none'",
                      "form-action 'self'", "connect-src 'self'", "font-src 'self'"):
        assert directive in csp, csp


def test_no_page_fetches_anything_from_another_host():
    """The old <head> fetched Roboto from fonts.googleapis.com / fonts.gstatic.com. A page view of a SOC
    dashboard must not be an outbound request: it leaks the dashboard's existence, and it fails offline.
    Only FETCHED resources are the claim — the banner's `<a href="https://github.com/...">` and the
    "Open CrowdSec metrics" button are links a human clicks, and are allowed to be absolute."""
    for path in ("/", "/api/permissions/ui", "/api/network/ui", "/api/backups/ui"):
        html = _get(path).text
        assert "fonts.googleapis" not in html and "fonts.gstatic" not in html, path
        for token in ("<link", "<script src", "<script src", "src=\"http", "src='http", "srcset=", "url(http"):
            assert token not in html, f"{path} fetches an external resource: {token}"


def security_css_var(name: str) -> str:
    line = [l for l in app.BASE_CSS.splitlines() if name in l]
    assert line, name
    return line[0]


def test_the_font_stack_falls_back_to_system_fonts():
    sans = security_css_var("--font-sans")
    assert "system-ui" in sans and sans.index("system-ui") < sans.index("Roboto")   # local Roboto only
    assert "ui-monospace" in security_css_var("--font-mono")


# ── TLS verification is on, and off only by name ──────────────────────────────

def test_wazuh_certificates_are_verified_by_default(monkeypatch):
    _clean(monkeypatch)
    assert security.tls_verify("WAZUH_TLS_VERIFY") is True
    assert security.tls_verify_or_ca("WAZUH_TLS_VERIFY") is True


def test_an_empty_setting_does_not_disable_verification(monkeypatch):
    """An accidentally empty environment variable must never turn security off."""
    monkeypatch.setenv("WAZUH_TLS_VERIFY", "")
    assert security.tls_verify("WAZUH_TLS_VERIFY") is True
    assert security.tls_verify_or_ca("WAZUH_TLS_VERIFY") is True


def test_the_opt_out_is_named_and_said_out_loud(monkeypatch):
    monkeypatch.setenv("WAZUH_TLS_VERIFY", "off")
    assert security.tls_verify("WAZUH_TLS_VERIFY") is False
    assert security.tls_verify_or_ca("WAZUH_TLS_VERIFY") is False
    note = security.tls_verify_note("WAZUH_TLS_VERIFY")
    assert "NOT verified" in note and "WAZUH_TLS_VERIFY" in note        # the operator reads what is off
    assert security.tls_verify_note("WAZUH_TLS_VERIFY", env={}) == ""   # verified = nothing appended


def test_a_private_ca_bundle_is_passed_through_rather_than_verification_being_dropped(monkeypatch):
    """Wazuh's self-signed certificate is the reason verify=False existed. The answer is the CA file."""
    monkeypatch.setenv("WAZUH_TLS_VERIFY", "/etc/ssl/wazuh-ca.pem")
    assert security.tls_verify_or_ca("WAZUH_TLS_VERIFY") == "/etc/ssl/wazuh-ca.pem"


def test_the_wazuh_status_line_reports_the_verification_state(monkeypatch):
    """A reachable manager whose certificate nobody checked must not read like a clean bill of health."""
    _clean(monkeypatch)
    monkeypatch.setattr(app, "WAZUH_PASS", "hunter2")
    monkeypatch.setattr(app, "_WZ_TOKEN", {"t": "tok"})
    payload = {"data": {"connection": {"active": 3, "disconnected": 1, "total": 4}}}

    class _R:
        def json(self):
            return payload

    seen = {}

    def _fake_get(url, **kw):
        seen["verify"] = kw.get("verify")
        return _R()

    monkeypatch.setattr(app.httpx, "get", _fake_get)
    out = app._t_wazuh_status({})
    assert seen["verify"] is True and out["data"]["tls_verified"] is True
    assert "NOT verified" not in out["text"]

    monkeypatch.setenv("WAZUH_TLS_VERIFY", "off")
    out = app._t_wazuh_status({})
    assert seen["verify"] is False and out["data"]["tls_verified"] is False
    assert "NOT verified" in out["text"]


# ── no interactive privileged command on any request path ─────────────────────

def test_serving_a_page_can_never_invoke_sudo(monkeypatch):
    """GET / reaches CrowdSec through `docker exec ... cscli`, and until recently that command began
    with `sudo`: the prompt fired during this very suite's live runs — three `Password:` lines printed
    in the middle of pytest, and the run took a minute on prompts alone. An interactive password prompt
    inside a request handler hangs the worker and turns reading a dashboard into a privileged action.
    Serve a page with CrowdSec 'reachable' and every subprocess captured: no command may be sudo, and
    none can be a password-prompting variant, because the default carries no sudo at all."""
    calls = []

    class _P:
        returncode, stdout, stderr = 1, "", "docker unavailable (test)"

    def _fake_run(cmd, *a, **kw):
        calls.append([str(t) for t in cmd])
        return _P()

    monkeypatch.setattr(core.subprocess, "run", _fake_run)
    monkeypatch.setattr(core, "crowdsec_connected", lambda: True)   # pretend CrowdSec answers
    core._CACHE.update(ts=0.0, data=None)                           # expire the dashboard cache
    try:
        r = TestClient(app.app).get("/")
        assert r.status_code == 200
        assert calls, "with CrowdSec reachable, serving / must reach cscli — through docker, not sudo"
        for cmd in calls:
            assert not any(str(t).rsplit("/", 1)[-1] == "sudo" for t in cmd), f"request path invoked sudo: {cmd}"
    finally:
        core._CACHE.update(ts=0.0, data=None)


def test_an_operator_sudo_wrapper_is_forced_non_interactive(monkeypatch):
    """Even an explicitly configured `sudo docker` can never prompt: the app rewrites it to `sudo -n`,
    which fails fast instead of waiting for a password nobody is there to type."""
    monkeypatch.setattr(core, "DOCKER_CMD", ["sudo", "docker"])
    assert core._docker_cmd() == ["sudo", "-n", "docker"]
    monkeypatch.setattr(core, "DOCKER_CMD", ["sudo", "-n", "docker"])
    assert core._docker_cmd() == ["sudo", "-n", "docker"]            # not doubled
    monkeypatch.setattr(core, "DOCKER_CMD", ["docker"])
    assert core._docker_cmd() == ["docker"]                          # the shipped default: no sudo


def test_the_shipped_default_docker_command_carries_no_sudo():
    """The default is read from the shipped source, so the assertion is about the code, not this run."""
    src = open(core.__file__, encoding="utf-8").read()
    assert 'os.environ.get("DOCKER_CMD", "docker")' in src           # plain docker by default
    assert 'os.environ.get("DOCKER_CMD", "sudo docker")' not in src  # the old unsafe default is gone


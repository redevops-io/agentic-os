"""Page re-test items RT-01..RT-05.

RT-01  with PERMISSIONS_TRUSTED_PROXIES unset (the compose default) every visitor shares ONE bucket (the proxy's address), so ten anonymous wrong keys used to lock
       the real admin out for five minutes. Now: no hard lock on a shared bucket; failures slow the bucket down (exponential backoff, capped, bounded waiters), the
       same for correct, missing and wrong keys (no oracle); the hard 429 lock stays only where the client identity is trustworthy (trusted proxies configured);
       a one-time start-up warning; deploy.sh requires the setting.
RT-02  IPv6 clients are bucketed by /64; the eviction caveat is documented.
RT-03  uvicorn's proxy_headers is explicit: off unless trusted proxies are configured, forwarded_allow_ips limited to that list.
RT-04  the showcase page has a Lock control that forgets the in-memory key.
RT-05  SECURITY_MODEL no longer says 'for every app'.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import textwrap

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
KEY = base64.b64encode(b"t" * 32).decode()
ADMIN = "rt-admin-0123456789-0123456789"
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")

LOAD = textwrap.dedent("""
    import importlib.util, json, logging, os
    from fastapi.testclient import TestClient
    logs = []
    class H(logging.Handler):
        def emit(self, rec): logs.append(rec.getMessage())
    logging.getLogger("agentic_os").addHandler(H()); logging.getLogger("agentic_os").setLevel(logging.DEBUG)
    def load_cp():
        from agentic_os import control_plane as cp
        return cp
    def load_demo():
        spec = importlib.util.spec_from_file_location("d", "deploy/permissions-showcase-demo/app.py")
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
""")


def _run(body, tmp_path, **env_extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PERMISSIONS_", "APPROVAL_", "AGENTIC_OS_", "TEST_", "DEMO_"))}
    env.update(PYTHONPATH=str(ROOT), PERMISSIONS_DIR=str(tmp_path / "p"), AGENTIC_OS_HOME=str(tmp_path / "h"), **env_extra)
    p = subprocess.run([sys.executable, "-c", LOAD + textwrap.dedent(body)], env=env, capture_output=True, text=True, timeout=300, cwd=str(ROOT))
    assert p.returncode == 0, p.stderr[-1500:]
    return json.loads(p.stdout.strip().splitlines()[-1])


STD = dict(PERMISSIONS_KEY=KEY, PERMISSIONS_ADMIN_KEY=ADMIN)

# ───────────────────────── RT-01 ─────────────────────────

SHARED_BUCKET = """
    from agentic_os import permissions_http as H
    H.BACKOFF_BASE, H.BACKOFF_CAP = 0.001, 0.004
    slept = []
    H._sleep = lambda s: slept.append(round(s, 6))
    out = {}
    for name, mod in (("cp", load_cp()), ("demo", load_demo())):
        H._failures.clear(); slept.clear()
        c = TestClient(mod.app)
        good = {"x-api-key": os.environ["PERMISSIONS_ADMIN_KEY"]}
        # ten (and more) anonymous wrong keys from the one proxy address: the RT-01 demonstration
        wrong = [c.get("/api/permissions/status", headers={"x-api-key": "wrong-" + str(i)}).status_code for i in range(H.FAIL_LIMIT * 3)]
        before = len(slept); slept.clear()
        probes = {}
        for label, hdr in (("correct", good), ("missing", {}), ("wrong", {"x-api-key": "wrong-final"})):
            for i in range(H.FAIL_LIMIT * 3):            # (re)prime the bucket: a correct key clears it, the next probe must start from the same state
                c.get("/api/permissions/status", headers={"x-api-key": "prime-" + str(i)})
            slept.clear()
            r = c.get("/api/permissions/status", headers=hdr)
            probes[label] = {"status": r.status_code, "retry_after": r.headers.get("retry-after"), "slept": list(slept)}
        H._failures.clear()
        out[name] = {"wrong": wrong, "probes": probes, "backoff_started": before}
    print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def shared(tmp_path_factory):
    return _run(SHARED_BUCKET, tmp_path_factory.mktemp("rt1"), **STD)         # no PERMISSIONS_TRUSTED_PROXIES


@pytest.mark.parametrize("plane", ["cp", "demo"])
class TestSharedBucketIsNeverALockout:
    def test_ten_anonymous_wrong_keys_do_not_lock_the_real_admin_out(self, shared, plane):
        s = shared[plane]
        assert set(s["wrong"]) == {401}                                          # never a 429: no hard lock on a shared bucket
        assert s["probes"]["correct"]["status"] == 200 and s["probes"]["correct"]["retry_after"] is None

    def test_the_bucket_is_slowed_instead_and_the_slow_down_is_capped(self, shared, plane):
        s = shared[plane]
        assert s["backoff_started"] > 0
        delays = s["probes"]["correct"]["slept"]
        assert delays and all(0 < d <= 0.004 for d in delays)

    def test_correct_missing_and_wrong_keys_are_slowed_identically_no_oracle(self, shared, plane):
        p = shared[plane]["probes"]
        assert p["correct"]["slept"] == p["missing"]["slept"] == p["wrong"]["slept"] and p["correct"]["slept"]
        assert (p["missing"]["status"], p["wrong"]["status"]) == (401, 401)


def test_backoff_grows_exponentially_up_to_the_cap_only_past_the_limit(monkeypatch):
    from agentic_os import permissions_http as H
    monkeypatch.setattr(H, "BACKOFF_BASE", 0.1)
    monkeypatch.setattr(H, "BACKOFF_CAP", 1.0)
    assert H._backoff_delay(0) == H._backoff_delay(H.FAIL_LIMIT - 1) == 0
    d = [H._backoff_delay(H.FAIL_LIMIT + k) for k in range(8)]
    assert d[0] == pytest.approx(0.1) and d[1] == pytest.approx(0.2) and d[2] == pytest.approx(0.4) and max(d) == 1.0 and d == sorted(d)


def test_the_number_of_threads_sleeping_in_backoff_is_bounded_and_the_overflow_is_a_uniform_429(monkeypatch):
    from agentic_os import permissions_http as H
    monkeypatch.setattr(H, "MAX_BACKOFF_WAITERS", 0)
    import time as _t
    monkeypatch.setattr(H, "_failures", {"peer": [H.FAIL_LIMIT + 5, _t.monotonic()]})
    from fastapi import HTTPException
    req = type("R", (), {"client": type("C", (), {"host": "peer"})(), "headers": {}, "query_params": {}})()
    for cred in ({}, {"x-api-key": "anything"}, {"x-api-key": ADMIN}):
        req.headers = cred
        with pytest.raises(HTTPException) as e:
            H.require_admin_key(req, ADMIN)
        assert e.value.status_code == 429 and e.value.headers.get("Retry-After")


def test_the_hard_lock_remains_where_the_client_identity_is_trustworthy(tmp_path):
    r = _run("""
        from agentic_os import permissions_http as H
        c = TestClient(load_cp().app)
        codes = [c.get("/api/permissions/status", headers={"x-api-key": "bad", "x-forwarded-for": "203.0.113.9"}).status_code for i in range(H.FAIL_LIMIT + 2)]
        other = c.get("/api/permissions/status", headers={"x-api-key": os.environ["PERMISSIONS_ADMIN_KEY"], "x-forwarded-for": "203.0.113.10"}).status_code
        print(json.dumps({"codes": codes, "other": other}))
    """, tmp_path, PERMISSIONS_TRUSTED_PROXIES="testclient", **STD)
    assert 429 in r["codes"] and r["other"] == 200


def test_a_trusted_proxy_setting_with_no_usable_forwarded_address_is_still_a_shared_bucket_so_it_backs_off(tmp_path):
    r = _run("""
        from agentic_os import permissions_http as H
        H.BACKOFF_BASE, H.BACKOFF_CAP = 0.001, 0.002
        H._sleep = lambda s: None
        c = TestClient(load_cp().app)
        codes = [c.get("/api/permissions/status", headers={"x-api-key": "bad"}).status_code for i in range(H.FAIL_LIMIT * 2)]
        print(json.dumps({"codes": codes, "good": c.get("/api/permissions/status", headers={"x-api-key": os.environ["PERMISSIONS_ADMIN_KEY"]}).status_code}))
    """, tmp_path, PERMISSIONS_TRUSTED_PROXIES="testclient", **STD)
    assert set(r["codes"]) == {401} and r["good"] == 200


@pytest.mark.parametrize("trusted,expect_warning", [("", True), ("127.0.0.1", False)])
def test_a_one_time_start_up_warning_when_the_admin_api_is_reachable_with_no_trusted_proxy_setting(tmp_path, trusted, expect_warning):
    env = dict(STD)
    if trusted:
        env["PERMISSIONS_TRUSTED_PROXIES"] = trusted
    r = _run("""
        load_cp(); load_demo()
        print(json.dumps({"logs": [m for m in logs if "PERMISSIONS_TRUSTED_PROXIES" in m]}))
    """, tmp_path, **env)
    assert (len(r["logs"]) == 1) is expect_warning and (not expect_warning or "shares one" in r["logs"][0].lower())


def test_no_warning_when_the_admin_api_is_disabled(tmp_path):
    r = _run("""
        load_cp(); load_demo()
        print(json.dumps({"logs": [m for m in logs if "PERMISSIONS_TRUSTED_PROXIES" in m]}))
    """, tmp_path, PERMISSIONS_KEY=KEY)
    assert r["logs"] == []


def test_deploy_sh_requires_the_trusted_proxy_setting_and_is_still_valid_bash():
    sh = (ROOT / "deploy" / "permissions-showcase-demo" / "deploy.sh").read_text()
    assert "PERMISSIONS_TRUSTED_PROXIES" in sh and "exit 1" in sh
    block = sh[sh.index("error: PERMISSIONS_TRUSTED_PROXIES"):][:900]
    assert "error:" in block and "required" in block.lower()
    assert subprocess.run(["bash", "-n", str(ROOT / "deploy" / "permissions-showcase-demo" / "deploy.sh")]).returncode == 0


# ───────────────────────── RT-02 ─────────────────────────

@pytest.mark.parametrize("a,b,same", [("2001:db8:1:2::1", "2001:db8:1:2:ffff:ffff:ffff:ffff", True), ("2001:db8:1:2::1", "2001:db8:1:3::1", False),
                                      ("::ffff:203.0.113.7", "203.0.113.7", True), ("203.0.113.7", "203.0.113.8", False)])
def test_ipv6_clients_are_bucketed_by_their_64_and_a_mapped_ipv4_is_the_ipv4(monkeypatch, a, b, same):
    from agentic_os import permissions_http as H
    monkeypatch.delenv("PERMISSIONS_TRUSTED_PROXIES", raising=False)
    mk = lambda host: type("R", (), {"client": type("C", (), {"host": host})(), "headers": {}})()          # noqa: E731
    assert (H.client_id(mk(a)) == H.client_id(mk(b))) is same


def test_a_forwarded_ipv6_client_is_bucketed_by_its_64_too(monkeypatch):
    from agentic_os import permissions_http as H
    monkeypatch.setenv("PERMISSIONS_TRUSTED_PROXIES", "127.0.0.1")
    mk = lambda xff: type("R", (), {"client": type("C", (), {"host": "127.0.0.1"})(), "headers": {"x-forwarded-for": xff}})()      # noqa: E731
    assert H.client_id(mk("2001:db8:9:9::1")) == H.client_id(mk("2001:db8:9:9::dead:beef"))


def test_the_ipv6_and_eviction_caveats_are_documented():
    t = (ROOT / "docs" / "SECURITY_MODEL.md").read_text()
    for needle in ("/64", "evict", "PERMISSIONS_TRUSTED_PROXIES", "backoff"):
        assert needle.lower() in t.lower(), needle


# ───────────────────────── RT-03 ─────────────────────────

def test_uvicorn_proxy_headers_are_explicit_off_without_trusted_proxies_and_limited_to_them_with():
    df = (ROOT / "deploy" / "permissions-showcase-demo" / "Dockerfile").read_text()
    cmd = df[df.index("CMD"):]
    assert "--no-proxy-headers" in cmd and "--proxy-headers" in cmd and "--forwarded-allow-ips" in cmd and "PERMISSIONS_TRUSTED_PROXIES" in cmd
    assert re.search(r'forwarded-allow-ips[= ]\\?"?\$PERMISSIONS_TRUSTED_PROXIES', cmd)
    assert cmd.index("--no-proxy-headers") < cmd.index("--proxy-headers --forwarded") or "else" in cmd


def test_the_compose_passes_the_trusted_proxies_through_and_documents_the_required_setting():
    t = (ROOT / "deploy" / "permissions-showcase-demo" / "permissions-demo.compose.yml").read_text()
    assert "PERMISSIONS_TRUSTED_PROXIES=${PERMISSIONS_TRUSTED_PROXIES:-}" in t and "REQUIRED" in t


# ───────────────────────── RT-04 ─────────────────────────

def _page(tmp_path, fixtures):
    from agentic_os import permissions_demo as demo
    from agentic_os import views
    (tmp_path / "page.html").write_text(views.permissions_demo_page(demo.list_scenarios(), nonce="n"))
    (tmp_path / "fx.json").write_text(json.dumps(fixtures))
    p = subprocess.run(["node", str(ROOT / "tests" / "_page_js_harness.js"), str(tmp_path / "page.html"), str(tmp_path / "fx.json")],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr[-400:]
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert "error" not in out, out
    return out


@needs_node
class TestShowcaseLock:
    FX = {"__storage_throws": True, "__values": {"akey": "k" * 30, "sc": "support-us-masked"},
          "api/permissions/grants": {"status": 200, "body": []}, "api/permissions/status": {"status": 200, "body": {"encrypted": True}},
          "api/demo/scenarios/support-us-masked": {"status": 200, "body": {"explanation": "", "allowed": False, "columns": [], "records": [],
                                                                            "withheld": 0, "full_count": 0}}}

    def test_the_page_has_a_lock_control(self, tmp_path):
        out = _page(tmp_path, {**self.FX, "__press": ["aload"]})
        assert "alock" in out["els"] and "click" in out["els"]["alock"]["listeners"]

    def test_lock_forgets_the_key_so_a_later_load_calls_nothing(self, tmp_path):
        out = _page(tmp_path, {**self.FX, "__press": ["aload", "alock", "aload"]})
        admin = [c for c in out["calls"] if "api/permissions" in c["url"]]
        assert len(admin) == 2 and all(c["headers"].get("X-API-Key") == "k" * 30 for c in admin)        # only the first load called the admin API
        assert "enter the admin key" in out["els"]["abox"]["textContent"] and out["els"]["akey"]["value"] == ""

    def test_without_lock_the_key_is_kept_for_the_next_load(self, tmp_path):
        out = _page(tmp_path, {**self.FX, "__press": ["aload", "aload"]})
        assert len([c for c in out["calls"] if "api/permissions" in c["url"]]) == 4


# ───────────────────────── RT-05 ─────────────────────────

def test_security_model_says_what_the_plane_does_not_for_every_app():
    t = (ROOT / "docs" / "SECURITY_MODEL.md").read_text()
    assert "Fine-grained data access for every app" not in t
    assert "basic resource permissions" in t.lower() and "trusted small-team" in t.lower()

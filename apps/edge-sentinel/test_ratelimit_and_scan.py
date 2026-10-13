"""Edge Sentinel hardening: per-IP token bucket on the unauthenticated POST routes, the ClamAV scan-path
allow-list, and the governed (in-boundary only) LLM path."""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

ratelimit = importlib.import_module("edge-sentinel.ratelimit")


class _Clock:
    t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("SENTINEL_RATE_PER_MIN", "SENTINEL_RATE_BURST", "SENTINEL_TRUST_PROXY"):
        monkeypatch.delenv(k, raising=False)
    ratelimit.LIMITER.reset()
    yield
    ratelimit.LIMITER.reset()


def test_bucket_allows_burst_then_throttles_then_refills():
    clk = _Clock()
    lim = ratelimit.TokenBucketLimiter(clock=clk)
    assert [lim.check("1.2.3.4") for _ in range(10)] == [0.0] * 10          # burst 10
    wait = lim.check("1.2.3.4")
    assert wait > 0                                                           # 11th is refused
    assert lim.check("9.9.9.9") == 0.0                                        # other client unaffected
    clk.t += 3.0                                                              # 20/min = 1 token / 3s
    assert lim.check("1.2.3.4") == 0.0
    assert lim.check("1.2.3.4") > 0


def test_env_overrides_and_bad_values_fall_back(monkeypatch):
    monkeypatch.setenv("SENTINEL_RATE_BURST", "2")
    monkeypatch.setenv("SENTINEL_RATE_PER_MIN", "not-a-number")
    lim = ratelimit.TokenBucketLimiter(clock=_Clock())
    assert lim.check("a") == 0.0 and lim.check("a") == 0.0 and lim.check("a") > 0


@pytest.mark.parametrize("path,body", [("/api/agent", {"message": "hi"}), ("/agent/run", {"action": "nope"}),
                                      ("/api/permissions/query", {})])
def test_unauthenticated_post_routes_return_429_with_retry_after(load_app, path, body):
    c = TestClient(load_app("edge-sentinel").app)
    codes = [c.post(path, json=body).status_code for _ in range(12)]
    assert 429 in codes and codes.index(429) == 10                           # burst of 10, then throttled
    r = c.post(path, json=body)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1


def test_xff_ignored_unless_proxy_trusted(load_app, monkeypatch):
    c = TestClient(load_app("edge-sentinel").app)
    # untrusted: spoofing X-Forwarded-For must NOT mint a fresh bucket
    for i in range(10):
        c.post("/agent/run", json={"action": "x"}, headers={"X-Forwarded-For": f"10.0.0.{i}"})
    assert c.post("/agent/run", json={"action": "x"}, headers={"X-Forwarded-For": "10.9.9.9"}).status_code == 429
    # trusted: the right-most XFF entry (appended by our proxy) is the key
    ratelimit.LIMITER.reset()
    monkeypatch.setenv("SENTINEL_TRUST_PROXY", "true")
    for _ in range(10):
        c.post("/agent/run", json={"action": "x"}, headers={"X-Forwarded-For": "6.6.6.6, 10.0.0.1"})
    assert c.post("/agent/run", json={"action": "x"}, headers={"X-Forwarded-For": "7.7.7.7, 10.0.0.1"}).status_code == 429
    assert c.post("/agent/run", json={"action": "x"}, headers={"X-Forwarded-For": "7.7.7.7, 10.0.0.2"}).status_code != 429


@pytest.mark.parametrize("raw,ok", [
    (None, "/scan"), ("", "/scan"), ("/scan", "/scan"), ("/scan/app/x.py", "/scan/app/x.py"),
    ("/scan//a/./b", "/scan/a/b"),
    ("/etc/passwd", None), ("/scan/../etc/shadow", None), ("/scanner", None), ("scan/x", None),
    ("/scan\nSHUTDOWN", None), ("/scan/x\x00", None), (["/scan"], None), ("/" + "a" * 2000, None),
])
def test_scan_path_allowlist(load_app, raw, ok):
    app = load_app("edge-sentinel")
    assert app._safe_scan_path(raw, base="/scan") == ok


def test_malware_scan_refuses_outside_base_without_touching_clamd(load_app, monkeypatch):
    app = load_app("edge-sentinel")
    monkeypatch.setattr(app, "_clamd", lambda *a, **k: pytest.fail("clamd must not be contacted"))
    out = app._t_malware_scan({"path": "/etc"})
    assert out["data"] == {"refused": True}
    seen = []
    monkeypatch.setattr(app, "_clamd", lambda cmd, timeout=30.0: seen.append(cmd) or ("PONG" if cmd == "PING" else "/scan/x: OK"))
    app._t_malware_scan({"path": "/scan/stack"})
    assert seen == ["PING", "SCAN /scan/stack"]


def test_llm_blurb_has_no_direct_http_and_fails_closed(load_app, monkeypatch):
    app = load_app("edge-sentinel")
    monkeypatch.delenv("REDEVOPS_LLM_BASE_URL", raising=False)
    monkeypatch.setattr(app.httpx, "post", lambda *a, **k: pytest.fail("direct model HTTP is forbidden"), raising=False)
    assert app._llm_blurb("x") is None
    # a PUBLIC host configured as the "model" is not in-boundary -> still no call, still None
    monkeypatch.setenv("REDEVOPS_LLM_BASE_URL", "https://api.example.com/v1")
    assert app._llm_blurb("x") is None

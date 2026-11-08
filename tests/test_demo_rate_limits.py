"""Rate limits and resource quotas on the anonymous Sidekick surface (audit item 2, second half).

The route answers without a credential by design; what bounds it is work, not identity. These tests assert
three things, in this order of importance:

1. a refused request creates nothing — no mission, no event in the store, no provider call;
2. the bucket is keyed on the socket peer, so a spoofed `X-Forwarded-For` buys no fresh budget and the
   header is only consulted for a proxy the operator declared;
3. the quota is checked before the mission exists, and it falls again when missions are resolved.

The clock is injected, so the windows are exact rather than slept through.
"""
from __future__ import annotations

import os
import tempfile

_STORE = os.path.join(tempfile.mkdtemp(), "limits_events.jsonl")
os.environ.setdefault("UNIFIED_STORE", _STORE)

import pytest  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from agentic_os.mission import unified_server  # noqa: E402
from agentic_os.mission.demo_limits import (DEFAULT_RATE_PER_MIN, ENV_TRUSTED_PROXY_IPS,  # noqa: E402
                                            DemoLimits, RateLimited, caller_key)

SYNC = {"text": "sync the pilot account from revenue into support"}
CHATTER = {"text": "what is the weather like"}


class _Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture()
def demo(monkeypatch):
    """The real app with a fresh limiter, a fresh mission list, and a store whose size is observable.

    `mission_limit` is generous here so these tests exercise the *bucket*; the quota has its own tests below
    with a tiny limit. Conflating the two once made a rate assertion fail for a quota reason.
    """
    clock = _Clock()
    monkeypatch.setattr(unified_server, "_LIMITS",
                        DemoLimits(per_min=6, burst=3, mission_limit=8, clock=clock))
    monkeypatch.setattr(unified_server, "_MISSIONS", [])
    monkeypatch.delenv(ENV_TRUSTED_PROXY_IPS, raising=False)

    def events() -> int:
        return sum(1 for _ in open(_STORE)) if os.path.exists(_STORE) else 0

    def client(peer="203.0.113.7"):
        return TestClient(unified_server.app, client=(peer, 51000), raise_server_exceptions=False)

    return clock, client, events


def _post(client, body=SYNC, peer="203.0.113.7"):
    return client(peer).post("/api/sidekick", json=body)


def test_a_refused_request_creates_no_mission_and_writes_no_event(demo):
    """The whole point of checking before the work: 429 has to mean nothing happened."""
    _, client, events = demo
    before_events, before_missions = events(), len(unified_server._MISSIONS)

    statuses = [_post(client).status_code for _ in range(8)]     # burst is 3, so 4 onwards are refused

    assert statuses[:3] == [200, 200, 200], statuses
    assert set(statuses[3:]) == {429}, statuses
    assert len(unified_server._MISSIONS) == before_missions + 3, "only the three allowed requests made missions"
    events_per_mission = (events() - before_events) // 3
    assert events_per_mission > 0, "a real mission writes events — so the store is worth measuring"

    # and a further refused request changes the store not at all
    mark = events()
    assert _post(client).status_code == 429
    assert events() == mark, "a refused request wrote to the event store"



def test_the_refusal_carries_retry_after(demo):
    """A refusal a client can act on: 429 plus how long to wait."""
    _, client, _ = demo
    for _ in range(3):
        _post(client)
    r = _post(client)
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1


def test_the_bucket_is_per_caller_so_one_busy_host_does_not_lock_out_another(demo):
    _, client, _ = demo
    for _ in range(3):
        assert _post(client, peer="203.0.113.7").status_code == 200
    assert _post(client, peer="203.0.113.7").status_code == 429
    assert _post(client, peer="198.51.100.4").status_code == 200, "a different caller has its own budget"


def test_a_spoofed_forwarded_header_buys_no_fresh_budget(demo):
    """Without a declared proxy the header is not consulted, so it cannot launder a rate limit."""
    _, client, _ = demo
    for _ in range(3):
        assert _post(client).status_code == 200
    r = client().post("/api/sidekick", json=SYNC,
                      headers={"x-forwarded-for": "198.51.100.99"})
    assert r.status_code == 429, "a header the caller controls must not reset the bucket"


def test_a_declared_proxy_makes_the_forwarded_client_the_key(demo, monkeypatch):
    """The operator's statement is what lets per-caller limiting survive a same-host proxy — and it is only
    honoured when the socket really came from the declared address."""
    monkeypatch.setenv(ENV_TRUSTED_PROXY_IPS, "127.0.0.1")
    c = TestClient(unified_server.app, client=("127.0.0.1", 44000), raise_server_exceptions=False)

    def post(xff):
        return c.post("/api/sidekick", json=CHATTER, headers={"x-forwarded-for": xff}).status_code

    for _ in range(3):
        assert post("203.0.113.11") == 200
    assert post("203.0.113.11") == 429, "the same forwarded client shares one bucket"
    assert post("198.51.100.11") == 200, "a different forwarded client behind the same proxy gets its own"


def test_a_forwarded_header_from_an_undeclared_peer_never_reaches_the_key_function():
    """The unit-level version of the spoof test: keying is a property of the scope, not of a header."""
    spoofed = {"client": ("203.0.113.7", 1), "headers": [(b"x-forwarded-for", b"198.51.100.99")]}
    assert caller_key(spoofed) == "peer:203.0.113.7"
    assert caller_key({"client": ("not-an-address", 0)}) == "peer:unknown"
    assert caller_key(None) == "peer:unknown"


def _quota_limits(mission_limit=2, clock=None):
    """A limiter where the *quota* is the binding constraint: the bucket is roomy enough that a refusal
    can only have come from the mission allowance, and each caller below is distinct anyway."""
    return DemoLimits(per_min=60, burst=30, mission_limit=mission_limit, clock=clock or _Clock())


def test_the_mission_quota_is_refused_before_the_mission_exists(demo, monkeypatch):
    """mission_limit=2: the third mission-intent is refused and the process holds exactly two missions."""
    _, client, events = demo
    monkeypatch.setattr(unified_server, "_LIMITS", _quota_limits(2))
    assert _post(client, peer="203.0.113.1").status_code == 200
    assert _post(client, peer="203.0.113.2").status_code == 200

    mark = events()
    third = _post(client, peer="203.0.113.3")
    assert third.status_code == 429, "the quota binds before seed_mission, not after"
    assert "allowance" in third.text or "awaiting approval" in third.text
    assert len(unified_server._MISSIONS) == 2
    assert events() == mark, "the refused request wrote no events"


def test_the_open_mission_bound_binds_on_its_own_without_the_lifetime_one():
    """Two bounds, two reasons. A mission that terminates quickly holds nothing open, so the open bound
    alone never fills; a process that has spent its allowance refuses even with nothing parked. Each
    branch has to be reachable on its own or one silently disables the other."""
    lim = DemoLimits(per_min=60, burst=30, mission_limit=2, clock=_Clock())
    with pytest.raises(RateLimited) as parked:
        lim.check_mission_quota(live_missions=2)          # nothing created here, two already parked
    assert parked.value.kind == "quota" and "awaiting approval" in str(parked.value)
    lim.mission_created()
    lim.mission_created()
    with pytest.raises(RateLimited) as spent:
        lim.check_mission_quota(live_missions=0)          # nothing parked, allowance spent
    assert spent.value.kind == "quota" and "allowance" in str(spent.value)


def test_the_lifetime_allowance_recovers_with_the_process_not_with_the_caller(demo, monkeypatch):
    """Deliberate fail-closed on an anonymous surface: the cumulative allowance bounds total work, since
    a mission that ends quickly holds nothing open and the rate bucket alone would let an anonymous caller
    create missions forever. A new process (here, a fresh limiter) is what resets it."""
    _, client, events = demo

    def fresh_process(mission_limit=1):
        monkeypatch.setattr(unified_server, "_LIMITS",
                            DemoLimits(per_min=60, burst=30, mission_limit=mission_limit, clock=_Clock()))

    fresh_process()
    assert _post(client, peer="203.0.113.1").status_code == 200
    mark = events()
    refused = _post(client, peer="203.0.113.2")
    assert refused.status_code == 429 and "allowance" in refused.text
    assert events() == mark, "the refused request wrote no events"
    fresh_process()
    assert _post(client, peer="203.0.113.3").status_code == 200, "a fresh process serves the demo again"


def test_chatter_consumes_rate_budget_but_no_mission_quota(demo):
    """Requests that create nothing must not eat the mission allowance — otherwise reading the demo starves it."""
    _, client, _ = demo
    for i in range(3):
        assert _post(client, body=CHATTER, peer=f"203.0.113.{i}").status_code == 200
    assert unified_server._MISSIONS == [], "no intent match, no mission"
    assert unified_server._LIMITS.missions_created == 0
    assert _post(client, body=SYNC, peer="203.0.113.50").status_code == 200, "quota is untouched by chatter"


def test_the_refill_restores_budget_over_time(demo):
    """A token bucket, not a permanent lockout: 6/min refills one token every 10 seconds."""
    clock, client, _ = demo
    for _ in range(3):
        assert _post(client).status_code == 200
    assert _post(client).status_code == 429
    clock.advance(11)
    assert _post(client).status_code == 200


def test_reads_are_not_rate_limited_and_keep_working(demo):
    """The limits sit on the mission-creating surface. Reading the demo stays cheap and unmetered, so a
    throttle can never turn the public journey into an outage."""
    _, client, _ = demo
    for _ in range(20):
        assert client().get("/api/connections").status_code == 200


def test_a_misconfigured_limit_falls_back_to_the_safe_default(monkeypatch):
    """An unparseable or zero value must not mean unlimited."""
    for value in ("", "many", "0", "-5"):
        monkeypatch.setenv("UNIFIED_SIDEKICK_REQUESTS_PER_MIN", value)
        lim = DemoLimits(per_min=None, burst=None, mission_limit=None, clock=_Clock())
        assert lim.per_min == DEFAULT_RATE_PER_MIN and lim.per_min > 0


def test_a_refusal_is_rate_limited_in_process_too():
    """The exception contract the route translates: kind and a usable retry window."""
    clock = _Clock()
    lim = DemoLimits(per_min=60, burst=1, mission_limit=1, clock=clock)
    lim.take("peer:a")
    with pytest.raises(RateLimited) as e:
        lim.take("peer:a")
    assert e.value.retry_after >= 1 and e.value.kind == "rate"
    with pytest.raises(RateLimited) as q:
        lim.check_mission_quota(live_missions=1)
    assert q.value.kind == "quota"


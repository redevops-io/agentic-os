"""The idempotency fingerprint (canonical form, bounds) and the bounded store, in isolation. Synthetic data."""
from __future__ import annotations

import pytest

from agentic_os.agent_gateway.idempotency import (
    CONFLICT, IN_PROGRESS, MAX_CANONICAL_BYTES, MAX_DEPTH, MAX_KEY_CHARS, NEW, REPLAY, IdempotencyError, IdempotencyStoreFull,
    InMemoryIdempotencyStore, canonical_arguments, fingerprint, normalise_key)

SCOPE = dict(tenant="t1", workspace="w1", subject="bob", authority_key="t1:bob", capability="c.read")


def fp(args=None, **over):
    return fingerprint(**{**SCOPE, **over}, arguments=args or {})


def test_the_canonical_form_is_stable_and_pinned():
    assert canonical_arguments({"b": [1, 2.0, True], "a": "café"}) == '{"a":"caf\\u00e9","b":[1,2,true]}'
    assert fp({"a": 1}) == "sha256:a5c058cd28a78ca31e64e7974e30c4daa6bf2812f546e86e125610814de6f4be"      # a change here re-keys every stored result


def test_equivalent_spellings_have_one_fingerprint_and_distinct_requests_do_not():
    assert fp({"x": 1, "y": [1, 2]}) == fp({"y": (1.0, 2), "x": 1, "idempotency_key": "k"})
    assert fp({"s": "café"}) == fp({"s": "café"})
    assert len({fp({"a": True}), fp({"a": 1}), fp({"a": "1"}), fp({"a": 1.5}), fp({"a": None}), fp({})}) == 6
    for over in (dict(tenant="t2"), dict(workspace="w2"), dict(subject="eve"), dict(authority_key="child-1"), dict(capability="c.write")):
        assert fp({"a": 1}, **over) != fp({"a": 1})


def test_arguments_that_cannot_be_bound_are_refused():
    for bad in ({"x": object()}, {"x": float("nan")}, {"x": float("inf")}, {1: "a"}, {"x": {1, 2}}, {"x": b"bytes"}):
        with pytest.raises(IdempotencyError):
            canonical_arguments(bad)
    deep = cur = {}
    for _ in range(MAX_DEPTH + 2):
        cur["n"] = {}
        cur = cur["n"]
    with pytest.raises(IdempotencyError):
        canonical_arguments(deep)
    with pytest.raises(IdempotencyError):
        canonical_arguments({"blob": "x" * (MAX_CANONICAL_BYTES + 1)})
    canonical_arguments({"blob": "x" * 1000})


def test_keys_are_bounded_and_normalised():
    assert normalise_key("café") == "café"
    for bad in ("", "k" * (MAX_KEY_CHARS + 1)):
        with pytest.raises(IdempotencyError):
            normalise_key(bad)


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def test_the_claim_protocol():
    s = InMemoryIdempotencyStore()
    assert s.begin("t", "k", "A") == (NEW, None)
    assert s.begin("t", "k", "A") == (IN_PROGRESS, None) and s.begin("t", "k", "B") == (CONFLICT, None)
    s.complete("t", "k", "B", "stolen")                                   # not the claim holder's fingerprint: ignored
    assert s.begin("t", "k", "A")[0] == IN_PROGRESS
    s.complete("t", "k", "A", {"v": 1})
    state, res = s.begin("t", "k", "A")
    assert (state, res) == (REPLAY, {"v": 1}) and s.begin("t", "k", "B")[0] == CONFLICT
    res["v"] = 99
    assert s.begin("t", "k", "A")[1] == {"v": 1}                            # a replay is a copy
    assert s.begin("other-tenant", "k", "B") == (NEW, None)                 # tenants do not share keys
    s.abandon("t", "k", "A")                                                # a completed entry is not abandoned
    assert s.begin("t", "k", "A")[0] == REPLAY


def test_abandon_releases_only_an_unfinished_claim_of_the_same_fingerprint():
    s = InMemoryIdempotencyStore()
    s.begin("t", "k", "A")
    s.abandon("t", "k", "B")
    assert s.begin("t", "k", "A")[0] == IN_PROGRESS
    s.abandon("t", "k", "A")
    assert s.begin("t", "k", "A")[0] == NEW


def test_ttl_applies_to_completed_entries_only():
    c = Clock()
    s = InMemoryIdempotencyStore(ttl_s=10, max_entries=5, clock=c)
    s.begin("t", "done", "A")
    s.complete("t", "done", "A", 1)
    s.begin("t", "flying", "A")
    c.t += 11
    assert s.begin("t", "done", "A")[0] == NEW                              # expired
    assert s.begin("t", "flying", "A")[0] == IN_PROGRESS                    # an in-flight claim has no expiry


def test_size_bound_evicts_oldest_completed_first_and_never_in_flight():
    s = InMemoryIdempotencyStore(ttl_s=1000, max_entries=3)
    s.begin("t", "flying", "A")
    for k in ("a", "b"):
        s.begin("t", k, "A")
        s.complete("t", k, "A", k)
    assert len(s) == 3
    assert s.begin("t", "c", "A")[0] == NEW and len(s) == 3                 # "a" (oldest completed) made room
    assert s.begin("t", "b", "A")[0] == REPLAY                              # "b" is still stored
    assert s.begin("t", "flying", "A")[0] == IN_PROGRESS                    # never evicted


def test_a_store_full_of_in_flight_claims_refuses_a_new_one():
    s = InMemoryIdempotencyStore(max_entries=2)
    s.begin("t", "a", "A")
    s.begin("t", "b", "A")
    with pytest.raises(IdempotencyStoreFull):
        s.begin("t", "c", "A")
    assert len(s) == 2


def test_seed_does_not_override_a_call_in_flight_and_is_bound_to_its_fingerprint():
    s = InMemoryIdempotencyStore()
    s.begin("t", "k", "A")
    s.seed("t", "k", "B", "reconciled")
    assert s.begin("t", "k", "A")[0] == IN_PROGRESS
    s2 = InMemoryIdempotencyStore()
    s2.seed("t", "k", "B", "reconciled")
    assert s2.begin("t", "k", "A")[0] == CONFLICT and s2.begin("t", "k", "B") == (REPLAY, "reconciled")


def test_invalid_bounds_are_refused():
    for kw in (dict(ttl_s=0), dict(max_entries=0)):
        with pytest.raises(ValueError):
            InMemoryIdempotencyStore(**kw)


def test_one_tenants_traffic_does_not_evict_another_tenants_keys():
    s = InMemoryIdempotencyStore(ttl_s=1000, max_entries=10)
    for k in ("a", "b"):
        s.begin("quiet", k, "F")
        s.complete("quiet", k, "F", k)
    for i in range(50):                                                        # a busy tenant far over the shared bound
        s.begin("busy", f"k{i}", "F")
        s.complete("busy", f"k{i}", "F", i)
    assert len(s) == 10
    assert s.begin("quiet", "a", "F") == (REPLAY, "a") and s.begin("quiet", "b", "F") == (REPLAY, "b")
    assert s.begin("busy", "k49", "F")[0] == REPLAY and s.begin("busy", "k0", "F")[0] == NEW


def test_a_per_tenant_cap_evicts_within_the_tenant_and_refuses_when_it_is_all_in_flight():
    s = InMemoryIdempotencyStore(ttl_s=1000, max_entries=100, max_entries_per_tenant=2)
    for k in ("a", "b", "c"):
        s.begin("t", k, "F")
        s.complete("t", k, "F", k)
    assert s.begin("t", "a", "F")[0] == NEW                                    # "a" was the oldest of the tenant's two-entry share
    assert s.begin("other", "x", "F")[0] == NEW                                # another tenant has its own share
    s2 = InMemoryIdempotencyStore(max_entries=100, max_entries_per_tenant=1)
    s2.begin("t", "a", "F")
    with pytest.raises(IdempotencyStoreFull):
        s2.begin("t", "b", "F")
    assert s2.begin("u", "b", "F")[0] == NEW


def test_argument_names_that_collapse_under_normalisation_cannot_be_bound():
    with pytest.raises(IdempotencyError):
        canonical_arguments({"caf\u00e9": 1, "cafe\u0301": 2})

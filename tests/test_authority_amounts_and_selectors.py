"""rc2 hardening of the authority plane: invalid amounts, the selector grammar, and the reserved ``compensate:`` prefix.

* G1: a negative (or non-finite, non-numeric, boolean) amount is refused on a limited grant and never lowers a cumulative total,
  in the in-memory and the SQLite ledger alike; settlement figures can only consume.
* G2: capability patterns and resource selectors are validated against a documented grammar when a grant is built, and a value is
  matched only when it has no control characters, no ``..`` segment and no list separator the pattern does not itself contain.
* A forward capability may not use the ``compensate:`` prefix, so no forward grant can cover an undo grant's namespace.
"""
from __future__ import annotations

import json
import time

import pytest

from agentic_os.mission.authority import (
    Action, AuthorityEnvelope, AuthorityGuard, AutonomyBudget, Grant, MemoryEnvelopeStore, MemoryLedger, SelectorError,
    SqliteEnvelopeStore, SqliteLedger, Verdict, evaluate, validate_selector)

NOW = 9_000_000.0


def env(*grants, **kw):
    base = dict(tenant="acme", principal="owner", mission_id="m1", issued_at=NOW - 10, expires_at=NOW + 3600,
                grants=grants, budget=AutonomyBudget(usd=1000, max_actions=1000))
    base.update(kw)
    return AuthorityEnvelope(**base)


def guard(envelope, ledger=None):
    store = MemoryEnvelopeStore()
    store.put(envelope)
    return AuthorityGuard(store, ledger or MemoryLedger(), clock=lambda: NOW + 1)


def act(cap="pay.send", **args):
    return Action(capability=cap, arguments=args, risk_tier=1, side_effecting=True)


# ── G1: amounts ─────────────────────────────────────────────────────────────────────────────────────
BAD_AMOUNTS = [-1, -0.01, -1000, float("nan"), float("inf"), float("-inf"), "abc", "-5", "1e999", [5], {"v": 5}, True, False]


@pytest.mark.parametrize("bad", BAD_AMOUNTS, ids=repr)
def test_invalid_amount_is_denied_on_a_per_action_limit(bad):
    dec = evaluate(env(Grant("pay.send", max_amount=100)), act(amount=bad), now=NOW)
    assert dec.verdict is Verdict.DENY and dec.rule == "amount_invalid"


@pytest.mark.parametrize("bad", BAD_AMOUNTS, ids=repr)
def test_invalid_amount_is_denied_on_a_cumulative_only_limit(bad):
    dec = evaluate(env(Grant("pay.send", max_total_amount=100)), act(amount=bad), now=NOW)
    assert dec.verdict is Verdict.DENY and dec.rule == "amount_invalid"


@pytest.mark.parametrize("ok", [0, 0.0, 1, 99.99, 100, "50", "0.5"], ids=repr)
def test_valid_amounts_are_still_allowed(ok):
    dec = evaluate(env(Grant("pay.send", max_amount=100)), act(amount=ok), now=NOW)
    assert dec.verdict is Verdict.ALLOW


def test_over_limit_is_still_an_escalation_not_a_denial():
    dec = evaluate(env(Grant("pay.send", max_amount=100)), act(amount=100.01), now=NOW)
    assert dec.verdict is Verdict.ESCALATE and dec.rule == "amount_over_limit"


def test_a_grant_without_amount_limits_does_not_look_at_the_amount():
    # `amount` can be an unrelated argument on a capability the owner did not limit by amount
    assert evaluate(env(Grant("pay.send")), act(amount="all"), now=NOW).verdict is Verdict.ALLOW


def _drain(g, amounts):
    return [g.authorize("m1", "acme", act(amount=a)).verdict for a in amounts]


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_negative_amounts_cannot_launder_the_cumulative_total(kind, tmp_path):
    """Before rc2: with max_total_amount=1000 the sequence [-1000,-1000,900,900,900] was all ALLOW (2700 spent)."""
    ledger = MemoryLedger() if kind == "memory" else SqliteLedger(str(tmp_path / "usage.db"))
    g = guard(env(Grant("pay.send", max_amount=1000, max_total_amount=1000)), ledger)
    out = _drain(g, [-1000, -1000, 900, 900, 900])
    assert out[:2] == [Verdict.DENY, Verdict.DENY]
    assert out[2] is Verdict.ALLOW and out[3] is Verdict.ESCALATE and out[4] is Verdict.ESCALATE
    # the refused calls booked nothing: exactly 100 of the allowance is left
    assert g.authorize("m1", "acme", act(amount=100)).verdict is Verdict.ALLOW
    assert g.authorize("m1", "acme", act(amount=0.01)).verdict is Verdict.ESCALATE


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_settlement_figures_can_only_consume(kind, tmp_path):
    ledger = MemoryLedger() if kind == "memory" else SqliteLedger(str(tmp_path / "usage.db"))
    g = guard(env(Grant("pay.send", max_amount=1000, max_total_amount=1000)), ledger)
    d = g.authorize("m1", "acme", act(amount=600), lease_id="L1")
    assert d.verdict is Verdict.ALLOW
    assert g.dispatched("L1") and g.settle("L1", {"g0:amount": -600})        # a negative actual is dropped: 600 stays charged
    assert g.authorize("m1", "acme", act(amount=500)).verdict is Verdict.ESCALATE
    assert g.authorize("m1", "acme", act(amount=400)).verdict is Verdict.ALLOW


def test_a_non_finite_settlement_figure_is_dropped():
    g = guard(env(Grant("pay.send", max_total_amount=1000)))
    assert g.authorize("m1", "acme", act(amount=700), lease_id="L1").verdict is Verdict.ALLOW
    g.dispatched("L1")
    assert g.settle("L1", {"g0:amount": float("nan")})
    assert g.authorize("m1", "acme", act(amount=400)).verdict is Verdict.ESCALATE


# ── G2: selector grammar ────────────────────────────────────────────────────────────────────────────
GOOD = ["*", "crm.*", "crm.contacts.read", "/data/acme/*", "*@customer.com", "[ab]*", "[!x]?", "a[]]b", "compensate:crm.*",
        "https://api.example.com/v1/*", "My Report (final).pdf", "x" * 512]
MALFORMED = ["", " a", "a ", "a\nb", "a\x00b", "a\x1fb", "a\x7fb", "a b", "a**b", "**", "/data/../x", "../x", "x/..", "a\\..\\b",
             "[abc", "a[", "[!", "x" * 513, 5, None, b"a"]


@pytest.mark.parametrize("pat", GOOD, ids=lambda p: repr(p)[:30])
def test_selectors_inside_the_grammar_are_accepted(pat):
    assert validate_selector(pat) == pat
    Grant("c", resources=(("r", (pat,)),))
    Grant(pat) if "\x00" not in pat else None


@pytest.mark.parametrize("pat", MALFORMED, ids=lambda p: repr(p)[:30])
def test_selectors_outside_the_grammar_are_rejected(pat):
    with pytest.raises(SelectorError):
        validate_selector(pat)
    with pytest.raises(SelectorError):
        Grant("c", resources=(("r", (pat,)),))
    with pytest.raises(SelectorError):
        Grant(pat)


def test_a_grant_needs_a_resource_name_and_at_least_one_pattern():
    with pytest.raises(SelectorError):
        Grant("c", resources=(("", ("a",)),))
    with pytest.raises(SelectorError):
        Grant("c", resources=(("r", ()),))


def test_a_malformed_stored_envelope_is_refused_on_load():
    d = env(Grant("c", resources=(("r", ("ok",)),))).to_dict()
    d["grants"][0]["resources"] = [["r", ["/data/../etc"]]]
    with pytest.raises(SelectorError):
        AuthorityEnvelope.from_dict(json.loads(json.dumps(d)))


def _scoped(pattern, value, key="to"):
    e = env(Grant("mail.send", resources=((key, (pattern,)),)))
    return evaluate(e, act("mail.send", **{key: value}), now=NOW)


@pytest.mark.parametrize("pattern,value", [
    ("*@customer.com", "ann@customer.com"),
    ("/data/acme/*", "/data/acme/q3/report.csv"),
    ("/data/acme/*", "/data/acme/My Report.pdf"),
    ("[ab]*", "apple"),
    ("a,b", "a,b"),                                  # a separator the pattern names itself is the owner's choice
    ("*,*", "x,y"),
])
def test_values_inside_the_grant_are_allowed(pattern, value):
    assert _scoped(pattern, value).verdict is Verdict.ALLOW


@pytest.mark.parametrize("pattern,value", [
    ("*@customer.com", "x@evil.io,a@customer.com"),         # list smuggling
    ("*@customer.com", "x@evil.io;a@customer.com"),
    ("*@customer.com", "x@evil.io\na@customer.com"),        # fnmatch '*' spans a newline
    ("*@customer.com", "x@evil.io\r\na@customer.com"),
    ("*@customer.com", "a\x00@customer.com"),
    ("/data/acme/*", "/data/acme/../other/f"),              # traversal
    ("/data/acme/*", "/data/acme/sub/../../other"),
    ("/data/acme/*", "/data/acme/..\\other"),
    ("*", "a\tb"),
    ("*", "x" * 4097),
    ("*", ""),                                                # unchanged: an empty list/str match is only `*`; see next test
])
def test_hostile_values_are_not_matched(pattern, value):
    dec = _scoped(pattern, value)
    if value == "":
        assert dec.verdict is Verdict.ALLOW                    # `*` still matches the empty string (unchanged behaviour)
    else:
        assert dec.verdict is Verdict.DENY and dec.rule == "resource_out_of_scope"


def test_every_element_of_a_list_value_is_checked_with_the_grammar():
    e = env(Grant("mail.send", resources=(("to", ("*@customer.com",)),)))
    assert evaluate(e, act("mail.send", to=["a@customer.com", "b@customer.com"]), now=NOW).verdict is Verdict.ALLOW
    assert evaluate(e, act("mail.send", to=["a@customer.com", "x@evil.io,b@customer.com"]), now=NOW).verdict is Verdict.DENY
    assert evaluate(e, act("mail.send", to=[]), now=NOW).verdict is Verdict.DENY


def test_dot_dot_inside_a_name_is_not_a_traversal_segment():
    assert _scoped("*@customer.com", "a..b@customer.com").verdict is Verdict.ALLOW
    assert _scoped("/data/*", "/data/v1..v2/file").verdict is Verdict.ALLOW


# ── the reserved compensate: prefix ─────────────────────────────────────────────────────────────────
def test_a_forward_capability_cannot_borrow_the_compensate_prefix():
    for grants in ((Grant("*"),), (Grant("compensate:*"),), (Grant("compensate:x"),)):
        g = guard(env(*grants))
        for fn in (g.check, lambda m, t, a: g.authorize(m, t, a)):
            dec = fn("m1", "acme", act("compensate:x"))
            assert dec.verdict is Verdict.DENY and dec.rule == "reserved_prefix"


def test_compensation_still_works_through_its_own_check():
    g = guard(env(Grant("compensate:pay.send")))
    assert g.check_compensation("m1", "acme", "pay.send").verdict is Verdict.ALLOW
    assert g.check_compensation("m1", "acme", "other").verdict is Verdict.DENY
    # a bare forward wildcard still does not stand in for an undo
    g2 = guard(env(Grant("*")))
    assert g2.check_compensation("m1", "acme", "pay.send").verdict is Verdict.DENY


def test_the_sqlite_envelope_store_round_trips_a_valid_grant(tmp_path):
    store = SqliteEnvelopeStore(str(tmp_path / "a.db"))
    e = env(Grant("mail.send", resources=(("to", ("*@customer.com",)),), max_amount=5))
    store.put(e)
    assert store.get("m1").digest() == e.digest() and time.time() > 0


def test_a_stored_envelope_with_a_malformed_selector_is_a_denial_not_an_exception():
    class Bad(MemoryEnvelopeStore):
        def get(self, mission_id):
            raise SelectorError("grant capability contains a control character")
    store = Bad()
    g = AuthorityGuard(store, MemoryLedger(), clock=lambda: NOW + 1)
    for dec in (g.check("m1", "acme", act("x")), g.authorize("m1", "acme", act("x")), g.check_compensation("m1", "acme", "x")):
        assert dec.verdict is Verdict.DENY and dec.rule == "authority_malformed"


# ── accounting: an invalid figure never moves a counter, with or without explicit amount limits ──────────────
INVALID = [-1, -0.01, float("nan"), float("inf"), float("-inf"), "abc", "-5", "1e999", [5], True]
GRANT_SHAPES = {
    "no limits": Grant("pay.send"),
    "per-action only": Grant("pay.send", max_amount=100),
    "cumulative only": Grant("pay.send", max_total_amount=100),
    "both": Grant("pay.send", max_amount=100, max_total_amount=100),
    "call and rate limits only": Grant("pay.send", max_calls=50, rate_calls=50),
}


def _ledger(kind, tmp_path):
    return MemoryLedger() if kind == "memory" else SqliteLedger(str(tmp_path / "usage.db"))


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
@pytest.mark.parametrize("lease", [False, True], ids=["plain", "leased"])
@pytest.mark.parametrize("shape", sorted(GRANT_SHAPES))
@pytest.mark.parametrize("bad", INVALID, ids=repr)
def test_an_invalid_amount_never_lowers_or_moves_an_amount_counter(kind, lease, shape, bad, tmp_path):
    ledger = _ledger(kind, tmp_path)
    g = guard(env(GRANT_SHAPES[shape]), ledger)
    before = dict(ledger.used("m1"))
    d = g.authorize("m1", "acme", act(amount=bad), lease_id="L1" if lease else "")
    used = ledger.used("m1")
    # whatever the verdict, no amount counter is ever below zero, and an amount counter never moves because of an invalid figure
    assert all(v >= 0 for v in used.values()), used
    assert used.get("g0:amount", 0.0) == 0.0, (shape, bad, d.verdict, used)
    if GRANT_SHAPES[shape].max_amount is not None or GRANT_SHAPES[shape].max_total_amount is not None:
        assert d.verdict is Verdict.DENY and used == before                  # refused: nothing at all was booked
    # the allowance is exactly as it was: a full-size valid spend still fits
    assert g.authorize("m1", "acme", act(amount=100), lease_id="L2" if lease else "").verdict in (Verdict.ALLOW, Verdict.ESCALATE)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_valid_amounts_are_booked_exactly(kind, tmp_path):
    ledger = _ledger(kind, tmp_path)
    g = guard(env(Grant("pay.send", max_total_amount=100)), ledger)
    assert g.authorize("m1", "acme", act(amount=30)).verdict is Verdict.ALLOW
    assert g.authorize("m1", "acme", act(amount="12.5")).verdict is Verdict.ALLOW
    assert ledger.used("m1")["g0:amount"] == pytest.approx(42.5)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_a_negative_or_non_finite_cost_or_token_estimate_is_refused_and_books_nothing(kind, tmp_path):
    ledger = _ledger(kind, tmp_path)
    g = guard(env(Grant("pay.send")), ledger)
    for cost, tokens in ((-5.0, 0), (float("nan"), 0), (float("inf"), 0), (0.0, -100)):
        d = g.authorize("m1", "acme", Action("pay.send", {}, risk_tier=1, side_effecting=True, cost_usd=cost, tokens=tokens))
        assert d.verdict is Verdict.DENY and d.rule == "amount_invalid", (cost, tokens)
    assert ledger.used("m1") == {}


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_the_ledgers_themselves_refuse_a_negative_or_non_finite_delta(kind, tmp_path):
    ledger = _ledger(kind, tmp_path)
    assert ledger.reserve("k", {"actions": 1.0, "g0:amount": 10.0}, {}) is None
    for bad in (-1.0, float("nan"), float("inf"), float("-inf")):
        assert ledger.reserve("k", {"g0:amount": bad}, {}) == "g0:amount"
        assert ledger.reserve_lease("Lx", "k", {"g0:amount": bad}, {}) == "g0:amount"
    assert ledger.used("k") == {"actions": 1.0, "g0:amount": 10.0}
    assert ledger.lease_state("Lx") is None


# ── a malformed selector in a PERSISTED envelope (real SQLite store, valid hash chain) ─────────────────────
def _malformed_grant(capability, resources=()):
    g = object.__new__(Grant)                     # bypasses the constructor's validation: what an older release could have stored
    for k, v in dict(capability=capability, resources=resources, max_tier=1, amount_param="amount", max_amount=None,
                     max_total_amount=None, max_calls=None, rate_calls=None, rate_window_s=3600.0, covers_dynamic_risk=False).items():
        object.__setattr__(g, k, v)
    return g


@pytest.mark.parametrize("bad", [
    dict(capability="pay.\nsend"), dict(capability="pay.**"), dict(capability="pay.[send"),
    dict(capability="pay.send", resources=(("to", ("/data/../x",)),)),
    dict(capability="pay.send", resources=(("to", ("a\x00b",)),)),
    dict(capability="pay.send", resources=(("to", ("",)),)),
], ids=lambda d: repr(d)[:50])
def test_a_persisted_malformed_selector_is_a_denial_for_every_entry_point(bad, tmp_path):
    path = str(tmp_path / "authority.db")
    store = SqliteEnvelopeStore(path)
    store.put(env(_malformed_grant(**bad)))                 # stored and hash-chained as written
    store2 = SqliteEnvelopeStore(path)                      # a fresh process opening the same file: it opens
    with pytest.raises(SelectorError):
        store2.get("m1")
    g = AuthorityGuard(store2, SqliteLedger(path + ".usage"), clock=lambda: NOW + 1)
    for dec in (g.check("m1", "acme", act("pay.send", to="x")), g.authorize("m1", "acme", act("pay.send", to="x")),
                g.authorize("m1", "acme", act("pay.send", to="x"), lease_id="L1"), g.check_compensation("m1", "acme", "pay.send")):
        assert dec.verdict is Verdict.DENY and dec.rule == "authority_malformed"
    assert SqliteLedger(path + ".usage").used("m1") == {}                    # and nothing was reserved


def test_a_persisted_well_formed_envelope_still_works_from_a_fresh_process(tmp_path):
    path = str(tmp_path / "authority.db")
    SqliteEnvelopeStore(path).put(env(Grant("pay.send", resources=(("to", ("*@customer.com",)),), max_total_amount=100)))
    g = AuthorityGuard(SqliteEnvelopeStore(path), SqliteLedger(path + ".usage"), clock=lambda: NOW + 1)
    assert g.authorize("m1", "acme", act("pay.send", to="a@customer.com", amount=5)).verdict is Verdict.ALLOW
    assert g.authorize("m1", "acme", act("pay.send", to="a@evil.io,b@customer.com", amount=5)).verdict is Verdict.DENY


def test_a_tampered_envelope_is_still_an_integrity_failure_not_a_malformed_selector(tmp_path):
    import sqlite3

    path = str(tmp_path / "authority.db")
    SqliteEnvelopeStore(path).put(env(Grant("pay.send")))
    c = sqlite3.connect(path)
    body = c.execute("SELECT body FROM envelopes WHERE mission_id='m1'").fetchone()[0]
    c.execute("UPDATE envelopes SET body=? WHERE mission_id='m1'", (body.replace('"pay.send"', '"pay.**"'),))   # edited into a malformed selector
    c.commit()
    c.close()
    from agentic_os.mission.authority import AuthorityIntegrityError
    with pytest.raises(AuthorityIntegrityError):                       # opening a tampered store is refused outright
        SqliteEnvelopeStore(path)
    g = AuthorityGuard(SqliteEnvelopeStore(path, verify=False), SqliteLedger(path + ".usage"), clock=lambda: NOW + 1)
    dec = g.authorize("m1", "acme", act("pay.send"))                    # and a store opened without the up-front pass still catches it per read
    assert dec.verdict is Verdict.DENY and dec.rule == "authority_integrity"


def test_one_unreadable_envelope_does_not_stop_the_store_opening_for_the_others(tmp_path):
    path = str(tmp_path / "authority.db")
    s0 = SqliteEnvelopeStore(path)
    s0.put(env(_malformed_grant("pay.**"), mission_id="bad"))
    s0.put(env(Grant("pay.send"), mission_id="good"))
    g = AuthorityGuard(SqliteEnvelopeStore(path), SqliteLedger(path + ".usage"), clock=lambda: NOW + 1)   # opens: bytes match the log
    assert g.authorize("good", "acme", act("pay.send")).verdict is Verdict.ALLOW
    assert g.authorize("bad", "acme", act("pay.send")).rule == "authority_malformed"

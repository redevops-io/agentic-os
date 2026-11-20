"""Phase 9c: the authorization contract enforced at the PRODUCTION entry points, not only in the evaluation.

An evaluation that adjudicates and a live system that does not have two different security semantics, and
only the green one gets measured. This file therefore drives the real ingestion path
(`incident.ingest_crowdsec_alert`, which is what `live.sync_cases` runs on every refresh, and what turns an
alert into a `sentinel.block_ip` proposal) and the real graph seam (`cti.seed_ioc`), with an authorization
file on disk read through the same loader the app uses. Nothing here calls a copy of the detector.

The cases are the five that decide whether the contract is a control or a decoration — valid scope, wrong
source, wrong observable, expired grant, missing/invalid configuration — plus the one that decides whether
it is an evasion grant: an authorization for a sanctioned scanner must NOT stand down that scenario when
the traffic comes from somewhere else.
"""
from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

authorization = importlib.import_module("edge-sentinel.authorization")
attack = importlib.import_module("edge-sentinel.attack")
case_store = importlib.import_module("edge-sentinel.case_store")
evidence = importlib.import_module("edge-sentinel.evidence")
cti = importlib.import_module("edge-sentinel.cti")
incident = importlib.import_module("edge-sentinel.incident")

CaseStatus = evidence.CaseStatus
CaseStore = case_store.CaseStore
CONFIG_VAR = authorization.CONFIG_VAR
OBSERVATION = attack.KIND_OBSERVATION
LABEL = attack.KIND_LABEL

#: The alert under test arrives from ALIEN. SANCTIONED is the address the operator named in the grant —
#: the whole point of `source_scope` is that these two being different is a detection, not a detail.
ALIEN = "203.0.113.7"
SANCTIONED = "198.51.100.7"

ALERT = {
    "scenario": "crowdsecurity/ssh-bf",
    "source": {"value": ALIEN, "scope": "Ip"},
    "events_count": 12,
    "created_at": "2026-09-21T10:00:00Z",
    "message": "12 failed ssh authentications",
}


def _grant(**over):
    """A complete, sourced grant for exactly this alert's scenario and token."""
    base = {"observable": "crowdsecurity/ssh-bf", "token": "ssh-bf", "principal": "secops-scanner",
            "source": "imported:inventory/vuln-manager", "source_scope": (SANCTIONED,)}
    base.update(over)
    return authorization.Authorization(**base)


def _ctx(*entries) -> authorization.AuthorizationContext:
    return authorization.AuthorizationContext(tuple(entries))


def _write(tmp_path, entries, *, doc_over=None) -> str:
    """The operator's file, on disk, in the documented shape. `doc_over` writes a broken document."""
    doc = {"version": authorization.CONFIG_VERSION,
           "authorizations": [dict(e) if isinstance(e, dict) else _entry(e) for e in entries]}
    doc.update(doc_over or {})
    path = tmp_path / "authorizations.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def _entry(a: authorization.Authorization) -> dict:
    return {"observable": a.observable, "token": a.token, "principal": a.principal, "source": a.source,
            **({"note": a.note} if a.note else {}),
            **({"expires_at": a.expires_at} if a.expires_at else {}),
            **({"source_scope": list(a.source_scope)} if a.source_scope else {})}


def _use(monkeypatch, path):
    monkeypatch.setenv(CONFIG_VAR, str(path))
    authorization.reset_cache()
    return authorization.authorization_config()


def _unconfigured(monkeypatch) -> None:
    monkeypatch.delenv(CONFIG_VAR, raising=False)
    authorization.reset_cache()


def _ingest(store=None, alert=None, ctx=None):
    """One pass of the real ingestion path. `ctx=None` means 'let the app resolve it from configuration',
    which is what a dashboard refresh actually does."""
    st = store or CaseStore()
    case = (incident.ingest_crowdsec_alert(st, alert or ALERT) if ctx is None
            else incident.ingest_crowdsec_alert(st, alert or ALERT, authorization=ctx))
    return st, case


def _offers(st, case):
    return [st.actions[r] for r in case.action_refs]


def _finding(st, case):
    return st.findings[case.finding_refs[0]]


# ── 1. valid scope: the one case a stand-down is supposed to happen ──

def test_an_in_scope_authorized_scenario_offers_no_block(monkeypatch):
    """The point of the whole mechanism: a sanctioned scanner's alert still produces the case and the
    evidence, and does NOT produce a ban proposal."""
    ctx = _ctx(_grant(source_scope=(ALIEN,)))
    st, case = _ingest(ctx=ctx)
    assert _offers(st, case) == []
    assert case.case_id                                  # the case exists: stood down is not deleted
    assert _finding(st, case).attack_refs == ()


def test_the_stood_down_finding_says_who_stood_it_down(monkeypatch):
    """A suppression that cannot name its principal and its source is a coverage gap with paperwork."""
    st, case = _ingest(ctx=_ctx(_grant(source_scope=(ALIEN,), expires_at="2027-01-31")))
    claim = _finding(st, case).claim
    assert "secops-scanner" in claim and "imported:inventory/vuln-manager" in claim
    assert "2027-01-31" in claim
    assert f"matched CrowdSec scenario 'crowdsecurity/ssh-bf'" in claim      # the match is still recorded


def test_a_cidr_scope_covers_an_address_inside_it():
    st, case = _ingest(ctx=_ctx(_grant(source_scope=("203.0.113.0/24",))))
    assert _offers(st, case) == []


# ── 2. the source axis: without this, the contract is an evasion grant ──

def test_a_grant_for_a_different_source_does_not_stand_the_alert_down():
    """THE regression this file exists for. An authorization says the vuln manager at 198.51.100.7 runs
    ssh brute-force activity; it says nothing about 203.0.113.7. If it did, publishing one inventory entry
    would silence that scenario for every address on earth — strictly worse than the false positive it
    removes."""
    st, case = _ingest(ctx=_ctx(_grant(source_scope=(SANCTIONED,))))
    assert [a.capability for a in _offers(st, case)] == ["sentinel.block_ip"]
    assert _offers(st, case)[0].parameters["ip"] == ALIEN


def test_a_grant_without_a_source_scope_cannot_cover_an_observation():
    """An entry that does not say where the activity comes from has not authorized an observation of it."""
    st, case = _ingest(ctx=_ctx(_grant(source_scope=())))
    assert [a.capability for a in _offers(st, case)] == ["sentinel.block_ip"]


def test_an_alert_with_no_source_address_is_never_stood_down():
    """Unknown source must never read as authorized source."""
    anonymous = dict(ALERT, source={})
    st, case = _ingest(alert=anonymous, ctx=_ctx(_grant(source_scope=("0.0.0.0/1",))))
    assert _finding(st, case).attack_refs == ("T1110", "T1110.001")


def test_an_unparseable_source_is_treated_as_unauthorized():
    ctx = _ctx(_grant(source_scope=("203.0.113.0/24",)))
    assert ctx.check(observable="crowdsecurity/ssh-bf", token="ssh-bf", source_ip="not-an-ip",
                     require_scope=True)[1] == authorization.DENY_OUT_OF_SCOPE


def test_scope_is_not_demanded_of_a_label():
    """A hub feed name has no source address. Demanding one would make the label lane of the evaluation
    unanswerable; applying one would be checking a document's postcode."""
    grant = _grant(source_scope=())
    assert attack.adjudicate_scenario(grant.observable, kind=LABEL, authorization=_ctx(grant)).tags == ()
    assert attack.adjudicate_scenario(grant.observable, kind=OBSERVATION,
                                      authorization=_ctx(grant)).tags != ()


def test_the_denial_reason_is_reported_rather_than_silently_absorbed():
    """An operator who tightens a grant and sees nothing change concludes the rule is broken, and the usual
    fix for a broken rule is to loosen it."""
    expired = _grant(source_scope=(ALIEN,), expires_at="2020-01-01")
    v = attack.adjudicate_scenario("crowdsecurity/ssh-bf", kind=OBSERVATION, authorization=_ctx(expired))
    assert [d["reason"] for d in v.denials] == [authorization.DENY_EXPIRED]
    out = attack.adjudicate_scenario("crowdsecurity/ssh-bf", kind=OBSERVATION,
                                     authorization=_ctx(_grant(source_scope=(SANCTIONED,))),
                                     source_ip=ALIEN)
    assert [d["reason"] for d in out.denials] == [authorization.DENY_OUT_OF_SCOPE]
    none = attack.adjudicate_scenario("crowdsecurity/ssh-bf", kind=OBSERVATION)
# ── 3. wrong identity: a grant covers exactly what it names ──

def test_a_grant_for_another_scenario_does_not_apply():
    st, case = _ingest(ctx=_ctx(_grant(observable="crowdsecurity/rdp-bf", source_scope=(ALIEN,))))
    assert [a.capability for a in _offers(st, case)] == ["sentinel.block_ip"]


def test_a_grant_for_another_token_does_not_apply():
    st, case = _ingest(ctx=_ctx(_grant(token="http-sqli-probing", source_scope=(ALIEN,))))
    assert [a.capability for a in _offers(st, case)] == ["sentinel.block_ip"]


def test_no_substring_identity_on_either_axis():
    """`crowdsecurity/ssh-bf` is not inside `crowdsecurity/ssh-bf-allowlist-bastion` as far as matching is
    concerned, and neither direction of near-miss may cover the other."""
    ctx = _ctx(_grant(observable="crowdsecurity/ssh-bf-allowlist-bastion", source_scope=(ALIEN,)))
    assert ctx.cover(observable="crowdsecurity/ssh-bf", token="ssh-bf", source_ip=ALIEN,
                     require_scope=True) is None
    narrow = _ctx(_grant(source_scope=(ALIEN,)))
    assert narrow.cover(observable="crowdsecurity/ssh-bf-allowlist-bastion", token="ssh-bf",
                        source_ip=ALIEN, require_scope=True) is None


def test_an_unaccountable_principal_is_refused_where_it_is_written():
    for bad in ("", "   ", "anyone", "*"):
        with pytest.raises(authorization.AuthorizationError):
            _grant(principal=bad)


def test_a_scope_covering_every_address_is_refused():
    """The source-side twin of principal "anyone": a scope that covers all sources is a rule deletion."""
    for bad in ("0.0.0.0/0", "::/0"):
        with pytest.raises(authorization.AuthorizationError):
            _grant(source_scope=(bad,))


# ── 4. expiry: checked at match time, against an instant ──

def test_an_expired_grant_still_bans():
    ctx = _ctx(_grant(source_scope=(ALIEN,), expires_at="2020-01-01T00:00:00Z"))
    st, case = _ingest(ctx=ctx)
    assert [a.capability for a in _offers(st, case)] == ["sentinel.block_ip"]


def test_a_lapsed_grant_stops_applying_to_a_long_running_process():
    """The clock is read per match, not per process start — an app that booted in January must not still
    honour a grant that died in February."""
    grant = _grant(source_scope=(ALIEN,), expires_at="2026-06-01T00:00:00Z")
    ctx = _ctx(grant)
    before = datetime(2026, 5, 31, 23, 59, 59, tzinfo=timezone.utc)
    after = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    claim = lambda now: attack.adjudicate_scenario("crowdsecurity/ssh-bf", kind=OBSERVATION,
                                                   authorization=ctx, source_ip=ALIEN, now=now).tags
    # Before the expiry the grant applies, so nothing may be claimed; after it, the detection stands.
    assert claim(before) == () and claim(after) == ("T1110", "T1110.001")


def test_a_bare_date_expires_at_midnight_utc_not_end_of_day():
    grant = _grant(source_scope=(ALIEN,), expires_at="2026-12-01")
    ctx = _ctx(grant)
    inside = datetime(2026, 11, 30, 23, 59, 59, tzinfo=timezone.utc)
    on = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)
    cover = lambda now: ctx.cover(observable=grant.observable, token=grant.token,
                                  source_ip=ALIEN, require_scope=True, now=now)
    assert cover(inside) is not None and cover(on) is None


def test_an_unparseable_expiry_is_an_error_never_a_permanent_grant():
    for bad in ("sometime next year", "2026-13-01", "31/12/2026"):
        with pytest.raises(authorization.AuthorizationError):
            _grant(expires_at=bad)



# ── 5. the configuration seam: missing or broken must never suppress anything ──

def test_unset_configuration_authorizes_nothing(monkeypatch):
    _unconfigured(monkeypatch)
    assert authorization.deployment_authorization().entries == ()
    cfg = authorization.authorization_config()
    assert cfg.status == "unconfigured" and not cfg.usable and cfg.detail
    st, case = _ingest()
    assert [a.capability for a in _offers(st, case)] == ["sentinel.block_ip"]


def test_a_missing_file_is_reported_as_unreadable_and_suppresses_nothing(monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path / "gone.json")
    cfg = authorization.authorization_config()
    assert cfg.status == "unreadable" and cfg.context.entries == ()
    st, case = _ingest()
    assert [a.capability for a in _offers(st, case)] == ["sentinel.block_ip"]


BROKEN = {
    "not json": "{",
    "a list at the top level": "[]",
    "an unknown top-level key": '{"version": 1, "authorizations": [], "default_allow": true}',
    "the wrong version": '{"version": 2, "authorizations": []}',
    "authorizations not a list": '{"version": 1, "authorizations": {"ssh-bf": true}}',
    "an entry that is not an object": '{"version": 1, "authorizations": ["ssh-bf"]}',
    # the typo this shape is designed for: read leniently, "expire_at" means "no expiry"
    "an unknown entry key": ('{"version": 1, "authorizations": [{"observable": "crowdsecurity/ssh-bf",'
                             ' "token": "ssh-bf", "principal": "secops", "source": "manual:x",'
                             ' "source_scope": ["203.0.113.7"], "expire_at": "2026-01-01"}]}'),
    "an entry with no principal": ('{"version": 1, "authorizations": [{"observable": "crowdsecurity/ssh-bf",'
                                   ' "token": "ssh-bf", "source": "manual:x",'
                                   ' "source_scope": ["203.0.113.7"]}]}'),
    "an unsourced entry": ('{"version": 1, "authorizations": [{"observable": "crowdsecurity/ssh-bf",'
                           ' "token": "ssh-bf", "principal": "secops", "source": "because",'
                           ' "source_scope": ["203.0.113.7"]}]}'),
    "a scope that is not an address": ('{"version": 1, "authorizations": [{"observable": "crowdsecurity/ssh-bf",'
                                       ' "token": "ssh-bf", "principal": "secops", "source": "manual:x",'
                                       ' "source_scope": ["internal"]}]}'),
}


@pytest.mark.parametrize("name", sorted(BROKEN))
def test_a_broken_authorization_file_never_suppresses_a_detection(name, monkeypatch, tmp_path):
    (tmp_path / "authorizations.json").write_text(BROKEN[name], encoding="utf-8")
    _use(monkeypatch, tmp_path / "authorizations.json")
    cfg = authorization.authorization_config()
    assert cfg.status == "invalid", (name, cfg.status)
    assert cfg.context.entries == (), f"{name}: a broken file must not contribute a suppression"
    assert "nothing is authorized" in cfg.detail or "nothing is suppressed" in cfg.detail
    st, case = _ingest()
    assert [a.capability for a in _offers(st, case)] == ["sentinel.block_ip"]


def test_the_good_document_in_the_contract_loads(monkeypatch, tmp_path):
    path = _write(tmp_path, [_grant(source_scope=(ALIEN,))])
    cfg = _use(monkeypatch, path)
    assert cfg.status == "ok" and len(cfg.context) == 1
    st, case = _ingest()                       # resolved from configuration, exactly as a refresh does it
    assert _offers(st, case) == []


def test_an_edit_on_disk_takes_effect_without_a_restart(monkeypatch, tmp_path):
    """The cache is keyed on the file's identity, so tightening the inventory takes effect on the next
    check — a cached allowlist an operator has just revoked is the failure mode to avoid."""
    path = _write(tmp_path, [_grant(source_scope=(ALIEN,))])
    monkeypatch.setenv(CONFIG_VAR, path)
    authorization.reset_cache()
    assert _offers(*_ingest()) == []
    _write(tmp_path, [_grant(source_scope=(SANCTIONED,))])       # revoked for this source
    assert [a.capability for a in _offers(*_ingest())] == ["sentinel.block_ip"]


def test_a_later_grant_does_not_retract_an_outstanding_proposal():
    """A stand-down applies to what is ingested AFTER it. Re-running the same alert under a new grant must
    not quietly edit an incident a human is already looking at — dropping its pending block would turn the
    inventory into a way to dispose of other people's cases. The place to act on it is the approval gate,
    which is where a human already is. (The replay proof stays deterministic, because case identity is the
    alert's and the verdict lives in the finding underneath it.)"""
    st = CaseStore()
    before = incident.ingest_crowdsec_alert(st, ALERT)
    assert [a.capability for a in _offers(st, before)] == ["sentinel.block_ip"]
    ctx = _ctx(_grant(source_scope=(ALIEN,)))
    again = incident.ingest_crowdsec_alert(st, ALERT, authorization=ctx)
    assert again.case_id == before.case_id
    assert again.status is CaseStatus.AWAITING_APPROVAL
    assert [a.capability for a in _offers(st, again)] == ["sentinel.block_ip"]      # still pending, not erased
    fresh, opened = _ingest(ctx=ctx)                    # what the same alert does from now on
    assert _offers(fresh, opened) == [] and opened.status is CaseStatus.INVESTIGATING

# ── 6. the graph seam: an `indicates` edge is a claim, not a log line ──

def test_seed_ioc_links_techniques_when_nothing_is_authorized():
    g = cti.CtiGraph()
    ind = cti.seed_ioc(g, SANCTIONED, scenario="crowdsecurity/ssh-bf", evidence_refs=("ev-1",))
    assert ind.type == "indicator"
    assert {o.props.get("name") for o in g.objects_of_type("attack-pattern")} == {
        "Brute Force", "Password Guessing"}


def test_a_stood_down_scenario_adds_no_technique_edge_but_keeps_the_observable():
    """Deleting the node would make a suppression indistinguishable from never having seen the IOC."""
    g = cti.CtiGraph()
    cti.seed_ioc(g, ALIEN, scenario="crowdsecurity/ssh-bf",
                 authorization=_ctx(_grant(source_scope=(ALIEN,))))
    assert g.objects_of_type("indicator") and g.objects_of_type("ipv4-addr")
    assert g.objects_of_type("attack-pattern") == []


def test_a_technique_edge_for_an_out_of_scope_source_is_still_claimed():
    g = cti.CtiGraph()
    cti.seed_ioc(g, ALIEN, scenario="crowdsecurity/ssh-bf",
                 authorization=_ctx(_grant(source_scope=(SANCTIONED,))))
    assert g.objects_of_type("attack-pattern")


def test_a_non_address_observable_cannot_be_scoped_and_so_is_never_stood_down():
    g = cti.CtiGraph()
    cti.seed_ioc(g, "http://example.test/payload.exe", scenario="crowdsecurity/ssh-bf",
                 authorization=_ctx(_grant(source_scope=(ALIEN,))))
    assert g.objects_of_type("attack-pattern")


# ── 7. what the deployment reports about its own configuration ──

def test_the_health_block_reports_state_without_naming_the_allowlist(monkeypatch, tmp_path):
    """`/api/v1/info` is a read role's view. Which sanctioned activity has been allowlisted is a map of
    what may be sent without being stood down, so counts and reasons travel, identities do not."""
    path = _write(tmp_path, [_grant(source_scope=(ALIEN,), expires_at="2020-01-01"),
                             _grant(observable="crowdsecurity/rdp-bf", token="rdp-bf", source_scope=())])
    _use(monkeypatch, path)
    health = authorization.authorization_config().health()
    assert health["status"] == "ok" and health["entries"] == 2
    assert health["expired"] == 1 and health["without_source_scope"] == 1
    blob = json.dumps(health).lower()
    for secret in ("ssh-bf", "secops-scanner", "vuln-manager", ALIEN, SANCTIONED):
        assert secret.lower() not in blob.replace(path.lower(), "")


def test_a_broken_file_is_visible_in_health_rather_than_silent(monkeypatch, tmp_path):
    (tmp_path / "authorizations.json").write_text('{"version": 9, "authorizations": []}', encoding="utf-8")
    _use(monkeypatch, tmp_path / "authorizations.json")
    health = authorization.authorization_config().health()
    assert health["status"] == "invalid" and health["entries"] == 0 and health["detail"]


# ── 8. the documented contract and the code cannot drift apart ──

def test_the_contract_document_describes_the_loader():
    doc = json.loads((HERE / "config_contract.json").read_text(encoding="utf-8"))
    ac = doc["authorization_contract"]
    assert ac["top_level_fields"] == list(authorization._TOP_FIELDS)
    assert ac["entry_fields"] == list(authorization._ENTRY_FIELDS)
    assert ac["version"] == authorization.CONFIG_VERSION
    assert tuple(ac["source_prefixes"]) == authorization.SOURCE_PREFIXES
    assert set(ac["required_per_entry"]) <= set(authorization._ENTRY_FIELDS)
    var = next(v for v in doc["variables"] if v["name"] == CONFIG_VAR)
    assert var["owner"] == "edge-sentinel"


@pytest.mark.parametrize("module", ["incident.py", "live.py", "cti.py", "api_v1.py"])
def test_every_production_reader_uses_the_contract(module):
    """The claim in config_contract.json's `readers` list, checked against the source: an entry point that
    quietly stops consulting the configuration is the whole point at which the evaluation and the
    operational system part company again."""
    path = HERE / module
    if not path.exists():
        # A vendored subset of the app may not carry every reader; the ones it does carry must all consult
        # the contract, and this file is written to be vendored with them.
        pytest.skip(f"{module} is not in this tree")
    src = path.read_text(encoding="utf-8")
    assert "deployment_authorization" in src or "authorization_config" in src, module


def test_replay_is_identical_under_the_same_context_and_differs_under_another():
    """A verdict depends on the inventory and on the clock, so `replay_case` takes both as arguments rather
    than reading them from wherever it likes."""
    ctx = _ctx(_grant(source_scope=(ALIEN,)))
    st, case = _ingest(ctx=ctx)
    proof = incident.replay_case(CaseStore(), ALERT, authorization=ctx)
    assert proof["case_id"] == case.case_id
    assert proof["finding_ids"] == sorted(st.findings)
    # The CASE identity belongs to the alert; the verdict changes the finding underneath it. Replaying the
    # same alert without the grant must therefore change the finding, not invent a second incident.
    other = incident.replay_case(CaseStore(), ALERT, authorization=authorization.NO_AUTHORIZATION)
    assert other["finding_ids"] != proof["finding_ids"]
    assert other["case_id"] == case.case_id



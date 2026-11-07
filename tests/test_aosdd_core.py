"""AOS-DD runtime integration: configuration (disabled by default), redaction, HTTP seam, operation ids, durable operation ledger."""
from __future__ import annotations

import json
import multiprocessing
import os
import stat
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from aosdd_fakes import APPROVAL_SECRET, BASES, FakeStack, config_dict, make_config, reader_dict  # noqa: E402

from agentic_os.integrations.aosdd import opid
from agentic_os.integrations.aosdd.config import AosddConfig, ConfigError, Secret, load_config
from agentic_os.integrations.aosdd.http import (AppError, AppHttp, NetworkTouched, ReadOnlyViolation, UrllibTransport, auth_for,
                                                classify, network_disabled)
from agentic_os.integrations.aosdd.oplog import FencingError, OpConflict, OpLog, new_owner
from agentic_os.integrations.aosdd.redact import Redactor


# ---- configuration --------------------------------------------------------------------------------------------------
def _from(d=None, **kw):
    return AosddConfig.from_dict(d if d is not None else config_dict(), approval_secret=kw.pop("approval_secret", APPROVAL_SECRET), reader=kw.pop("reader", reader_dict()), **kw)


def _write(path, obj, mode=0o600):
    path.write_text(json.dumps(obj))
    os.chmod(path, mode)
    return str(path)


def _files(tmp_path, **over):
    """The three separate secret sources, as files."""
    cfg = config_dict(**over)
    v = _write(tmp_path / "verifier.json", {"schema": "aosdd.verifier/1", "reader": reader_dict()})
    a = _write(tmp_path / "approval.key", {"schema": "aosdd.approval-key/1", "key": APPROVAL_SECRET})
    c = _write(tmp_path / "config.json", {**cfg, "verifier_file": v, "approval_key_file": a})
    return c, v, a


def test_integration_is_disabled_without_an_explicit_config_file():
    assert load_config({}) is None
    assert load_config({"AOSDD_CONFIG_FILE": ""}) is None


def test_config_has_no_defaults_for_urls_or_credentials():
    d = config_dict()
    del d["base_urls"]["lago"]
    with pytest.raises(ConfigError, match="base_urls.lago"):
        _from(d)
    d = config_dict()
    d["writer"]["twenty"] = {}
    with pytest.raises(ConfigError, match="writer.twenty"):
        _from(d)


def test_config_refuses_non_loopback_urls_and_a_short_approval_key():
    d = config_dict()
    d["base_urls"]["twenty"] = "https://crm.example.com"
    with pytest.raises(ConfigError, match="not loopback"):
        _from(d)
    with pytest.raises(ConfigError, match="at least 32"):
        _from(approval_secret="short")


def test_config_files_must_be_private_and_secrets_never_print(tmp_path):
    c, v, a = _files(tmp_path)
    os.chmod(c, 0o644)
    with pytest.raises(ConfigError, match="chmod 600"):
        AosddConfig.from_file(c)
    os.chmod(c, 0o600)
    os.chmod(a, 0o640)
    with pytest.raises(ConfigError, match="chmod 600"):
        AosddConfig.from_file(c)
    os.chmod(a, 0o600)
    cfg = AosddConfig.from_file(c)
    assert load_config({"AOSDD_CONFIG_FILE": c}) is not None
    assert cfg.verifier_source == "verifier.json" and cfg.approval_source == "approval.key"
    for sec in cfg.secret_values():
        assert sec not in repr(cfg) and sec not in str(cfg.writer) and sec not in repr(cfg.reader) and sec not in repr(cfg.approval_secret)
    assert repr(Secret("abc123456")) == "Secret(<redacted>)"
    with pytest.raises(TypeError):
        __import__("pickle").dumps(Secret("abc123456"))


# ---- item 9: the approval signing key has its own source -------------------------------------------------------------------
def test_approval_key_and_reader_may_not_live_in_the_application_config():
    with pytest.raises(ConfigError, match="approval_secret.*own secret file"):
        _from({**config_dict(), "approval_secret": APPROVAL_SECRET})
    with pytest.raises(ConfigError, match="reader.*own secret file"):
        _from({**config_dict(), "reader": reader_dict()})


def test_the_same_value_or_file_for_approval_and_application_credentials_is_refused(tmp_path):
    d = config_dict()
    d["writer"]["lago"]["api_key"] = APPROVAL_SECRET                       # the signing key reused as an application credential
    with pytest.raises(ConfigError, match="equals an application credential"):
        _from(d)
    c, v, a = _files(tmp_path)
    with pytest.raises(ConfigError, match="different file from the application config"):
        AosddConfig.from_file(c, approval_key_file=c)
    with pytest.raises(ConfigError, match="different file from the application config"):
        AosddConfig.from_file(c, verifier_file=c)
    with pytest.raises(ConfigError, match="verifier file and the approval key file must be different"):
        AosddConfig.from_file(c, approval_key_file=v)
    cfg_no_paths = _write(tmp_path / "bare.json", config_dict())
    with pytest.raises(ConfigError, match="own file"):
        AosddConfig.from_file(cfg_no_paths)                                 # no key file configured: refuse, never fall back to a value in the config


def test_approval_key_file_can_come_from_the_environment_and_wrong_schemas_are_refused(tmp_path):
    c, v, a = _files(tmp_path)
    bare = _write(tmp_path / "bare.json", config_dict())
    cfg = load_config({"AOSDD_CONFIG_FILE": bare, "AOSDD_APPROVAL_KEY_FILE": a, "AOSDD_VERIFIER_FILE": v})
    assert cfg is not None and cfg.approval_secret.reveal() == APPROVAL_SECRET
    wrong = _write(tmp_path / "wrong.key", {"schema": "aosdd.config/1", "key": APPROVAL_SECRET})
    with pytest.raises(ConfigError, match="approval-key"):
        AosddConfig.from_file(c, approval_key_file=wrong)


def test_redaction_covers_the_approval_key_and_every_application_credential():
    cfg = _from()
    blob = " | ".join(cfg.secret_values())
    out = cfg.redactor.text(blob + " Authorization: token erpKEYwriter:erpSECRETwriter")
    for sec in cfg.secret_values():
        assert sec not in out
    assert APPROVAL_SECRET in cfg.secret_values()


# ---- item 6: lease >= http timeout ------------------------------------------------------------------------------------------------
def test_lease_is_never_shorter_than_the_http_timeout(tmp_path):
    assert _from().lease_ttl_s == 120.0 and _from().http_timeout_s == 30.0                    # provisional default: 120 s lease
    with pytest.raises(ConfigError, match="lease_ttl_s.*>=.*http_timeout_s"):
        _from({**config_dict(), "lease_ttl_s": 20, "http_timeout_s": 30})
    assert _from({**config_dict(), "lease_ttl_s": 30, "http_timeout_s": 30}).lease_ttl_s == 30     # equal is allowed
    # the wiring refuses too: a ledger whose lease is shorter than the configured HTTP timeout
    from agentic_os.integrations.aosdd.services import Services
    with pytest.raises(ConfigError, match="shorter than the HTTP timeout"):
        Services(_from(), None, OpLog(str(tmp_path / "o.db"), lease_ttl_s=5), None, transport=FakeStack())
    # and every client built from the config uses that timeout
    svc = Services(_from(), None, OpLog(str(tmp_path / "o2.db"), lease_ttl_s=120), None, transport=FakeStack())
    assert all(h.timeout == 30.0 for h in svc.writers.values()) and all(h.timeout == 30.0 for h in svc.readback.c.values())


# ---- redaction ---------------------------------------------------------------------------------------------------------
def test_redactor_scrubs_values_encodings_pairs_and_shapes():
    r = Redactor(["fake-secret-VALUE-1234"])
    r.add_pair("keyAAAAAA", "secBBBBBB")
    s = "a fake-secret-VALUE-1234 b fake-secret-VALUE-1234 Authorization: token keyAAAAAA:secBBBBBB c Bearer abcdefghijklmnop12 d api_access_token: zzzzzzzzzzz"
    out = r.text(s)
    assert "fake-secret-VALUE-1234" not in out and "secBBBBBB" not in out and "abcdefghijklmnop12" not in out and "zzzzzzzzzzz" not in out
    import base64, urllib.parse
    assert "fake-secret-VALUE-1234" not in r.text(base64.b64encode(b"fake-secret-VALUE-1234").decode())
    assert r.scrub({"k": ["fake-secret-VALUE-1234", {"x": "fake-secret-VALUE-1234"}], "fake-secret-VALUE-1234": 1}) == {"k": ["[REDACTED]", {"x": "[REDACTED]"}], "[REDACTED]": 1}
    assert r.text("ordinary text") == "ordinary text"


# ---- HTTP seam ---------------------------------------------------------------------------------------------------------
def _cfg():
    return make_config()


def test_read_only_client_refuses_every_method_but_get():
    stack, cfg = FakeStack(), _cfg()
    h = AppHttp("twenty", BASES["twenty"], auth_for("twenty", cfg.reader["twenty"]), stack, cfg.redactor, read_only=True)
    with pytest.raises(ReadOnlyViolation):
        h.request("POST", "/rest/tasks", {"id": "x"})
    with pytest.raises(ReadOnlyViolation):
        h.request("DELETE", "/rest/tasks/x")
    assert h.get("/rest/companies").status == 200
    assert stack.writes() == []


def test_errors_are_classified_and_scrubbed():
    stack, cfg = FakeStack(), _cfg()
    stack.fail("lago", "GET", "/api/v1/customers", 500, body={"error": "boom " + TOKEN_LAGO})
    h = AppHttp("lago", BASES["lago"], auth_for("lago", cfg.writer["lago"]), stack, cfg.redactor)
    with pytest.raises(AppError) as e:
        h.check(h.get("/api/v1/customers/AOSDD-HFC-001"))
    assert e.value.kind == "server" and not e.value.not_applied
    assert TOKEN_LAGO not in str(e.value) and TOKEN_LAGO not in e.value.message
    assert classify("x", 0, None) == "unavailable" and classify("x", 502, "") == "unavailable"
    assert classify("x", 401, "") == "credential" and classify("x", 403, "revoked") == "credential" and classify("x", 403, "no") == "permission"
    assert classify("x", 422, '{"error":"value_already_exist"}') == "duplicate" and classify("x", 409, "") == "conflict"
    assert AppError("a", "unavailable", 0, "").not_applied is False and AppError("a", "credential", 401, "").not_applied is True


TOKEN_LAGO = "lagoKEYsecret0004"


def test_network_disabled_blocks_the_real_transport_before_any_socket():
    t = UrllibTransport()
    from agentic_os.integrations.aosdd.http import Request
    with network_disabled():
        with pytest.raises(NetworkTouched):
            t.send(Request("GET", "http://127.0.0.1:9/x", {}, None, 1))


# ---- operation ids -----------------------------------------------------------------------------------------------------------
def test_op_ids_are_deterministic_and_bound_to_mission_step_target_attempt():
    t = {"invoice": "i1"}
    a = opid.op_id("mission_1", "S2", t)
    assert a == opid.op_id("mission_1", "S2", {"invoice": "i1"})
    assert len({a, opid.op_id("mission_2", "S2", t), opid.op_id("mission_1", "S3", t), opid.op_id("mission_1", "S2", {"invoice": "i2"}), opid.op_id("mission_1", "S2", t, 2)}) == 5
    assert opid.valid(a, "mission_1", "S2", t) and not opid.valid(a, "mission_1", "S2", {"invoice": "i2"})
    assert opid.twenty_uuid(a, "task") == opid.twenty_uuid(a, "task") != opid.twenty_uuid(a, "target")
    assert a in opid.chatwoot_marker(a) and opid.erp_title(a).startswith("AOSDD-")


# ---- durable operation ledger ----------------------------------------------------------------------------------------------
def _plan(log, op="aosdd.m.S1.a1.abc", **kw):
    return log.plan_op(op, "m", "S1", "erpnext", "cap", "td", 1)


def test_claim_is_persisted_before_any_request_and_visible_to_a_second_process(tmp_path):
    db = str(tmp_path / "ops.db")
    log = OpLog(db)
    _plan(log)
    seen = {}

    class Probe:
        def send(self, req):
            seen["state_at_send"] = OpLog(db).get("aosdd.m.S1.a1.abc")["state"]       # a fresh connection (another process would see the same)
            from agentic_os.integrations.aosdd.http import Resp
            return Resp(200, {}, "{}")
    c = log.claim("aosdd.m.S1.a1.abc", new_owner())
    assert c.status == "acquired"
    Probe().send(None)
    assert seen["state_at_send"] == "inflight"


def test_exactly_one_of_many_concurrent_claimants_wins(tmp_path):
    log = OpLog(str(tmp_path / "ops.db"))
    _plan(log)
    results, barrier = [], threading.Barrier(12)

    def go():
        l = OpLog(str(tmp_path / "ops.db"))
        barrier.wait()
        results.append(l.claim("aosdd.m.S1.a1.abc", new_owner()).status)
    ts = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results).count("acquired") == 1 and results.count("held") == 11


def _claim_in_process(args):
    db, q = args
    q.put(OpLog(db).claim("aosdd.m.S1.a1.abc", new_owner()).status)


def test_claim_is_exclusive_across_processes(tmp_path):
    db = str(tmp_path / "ops.db")
    _plan(OpLog(db))
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    ps = [ctx.Process(target=_claim_in_process, args=((db, q),)) for _ in range(4)]
    [p.start() for p in ps]
    [p.join(30) for p in ps]
    got = sorted(q.get(timeout=5) for _ in range(4))
    assert got.count("acquired") == 1 and got.count("held") == 3


def test_applied_op_is_returned_not_repeated_and_stale_executor_is_fenced(tmp_path):
    log = OpLog(str(tmp_path / "ops.db"))
    _plan(log)
    c = log.claim("aosdd.m.S1.a1.abc", "A")
    log.record_applied("aosdd.m.S1.a1.abc", c.fence, "SAL-1", {"native_id": "SAL-1"})
    again = log.claim("aosdd.m.S1.a1.abc", "B")
    assert again.status == "applied" and again.row["native_id"] == "SAL-1"
    with pytest.raises(FencingError):
        log.record_applied("aosdd.m.S1.a1.abc", c.fence, "SAL-2", {})


def test_expired_lease_or_unknown_requires_reconciliation_never_a_blind_second_write(tmp_path):
    t = [1000.0]
    log = OpLog(str(tmp_path / "ops.db"), lease_ttl_s=10, clock=lambda: t[0])
    _plan(log)
    c = log.claim("aosdd.m.S1.a1.abc", "A")
    assert log.claim("aosdd.m.S1.a1.abc", "B").status == "held"
    t[0] += 11                                              # A crashed after claiming: its request may be in flight
    assert log.claim("aosdd.m.S1.a1.abc", "B").status == "reconcile"
    log.resolve_not_applied("aosdd.m.S1.a1.abc", "marker not found in the application")
    c2 = log.claim("aosdd.m.S1.a1.abc", "B")
    assert c2.status == "acquired" and c2.fence > c.fence
    with pytest.raises(FencingError):
        log.record_applied("aosdd.m.S1.a1.abc", c.fence, "late", {})        # A wakes up: fenced


def test_definite_refusal_is_retryable_unknown_is_not(tmp_path):
    log = OpLog(str(tmp_path / "ops.db"))
    _plan(log)
    c = log.claim("aosdd.m.S1.a1.abc", "A")
    log.record_refused("aosdd.m.S1.a1.abc", c.fence, "HTTP 401")
    c = log.claim("aosdd.m.S1.a1.abc", "A")
    assert c.status == "acquired"
    log.record_unknown("aosdd.m.S1.a1.abc", c.fence, "timeout")
    assert log.claim("aosdd.m.S1.a1.abc", "A").status == "reconcile"
    log.resolve_applied("aosdd.m.S1.a1.abc", "SAL-9", {"native_id": "SAL-9"}, "found by title")
    assert log.claim("aosdd.m.S1.a1.abc", "Z").status == "applied"


def test_same_op_id_with_a_different_target_is_a_conflict_and_compensated_ops_are_not_reclaimable(tmp_path):
    log = OpLog(str(tmp_path / "ops.db"))
    _plan(log)
    with pytest.raises(OpConflict):
        log.plan_op("aosdd.m.S1.a1.abc", "m", "S1", "erpnext", "cap", "OTHER", 1)
    c = log.claim("aosdd.m.S1.a1.abc", "A")
    log.record_applied("aosdd.m.S1.a1.abc", c.fence, "SAL-1", {})
    gen = log.begin_compensation("aosdd.m.S1.a1.abc")
    assert gen == c.fence + 1
    log.end_compensation("aosdd.m.S1.a1.abc", gen, True)
    assert log.claim("aosdd.m.S1.a1.abc", "A").status == "compensated"       # a re-run needs a NEW attempt (new op id)
    assert [h["state"] for h in log.history("aosdd.m.S1.a1.abc")][-3:] == ["applied", "compensating", "compensated"]


# ---- item 10: verifier credential provenance ---------------------------------------------------------------------------------------
def test_every_verification_states_its_credential_source_and_what_enforces_read_only():
    from agentic_os.integrations.aosdd.config import READ_ONLY_KIND
    cfg = _cfg()
    assert READ_ONLY_KIND == {"erpnext": "server-enforced", "twenty": "server-enforced",
                              "chatwoot": "get-only-by-code-shared-writer-credential", "lago": "get-only-by-code-shared-writer-credential"}
    prov = cfg.verifier_provenance()
    assert prov["source"] == "verifier.json" and set(prov["apps"]) == {"chatwoot", "twenty", "erpnext", "lago"}
    # the fixtures give Chatwoot and Lago the SAME secret for reading as for writing (no scoped credential exists): the receipt says so
    assert prov["apps"]["chatwoot"]["same_secret_as_writer"] and prov["apps"]["lago"]["same_secret_as_writer"]
    assert not prov["apps"]["twenty"]["same_secret_as_writer"] and not prov["apps"]["erpnext"]["same_secret_as_writer"]

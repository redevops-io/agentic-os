"""The explicit operator action that gives the local session holder the inspect permission for a project, and takes it away.
Default: no grant. Both directions are recorded in the hash-chained audit log; a grant never exists without its record."""
from __future__ import annotations

import json

import pytest

from agentic_os import cli
from agentic_os.drilldown.mount import LOCAL_OWNER_ID
from agentic_os.drilldown.operator import GrantRefused, grant_local_inspect, local_inspect_grants, revoke_local_inspect
from agentic_os.evidence_query import JsonlAuditSink
from agentic_os.permissions import GrantStore


@pytest.fixture()
def env(tmp_path):
    return type("E", (), dict(grants=GrantStore(str(tmp_path / "perm")), audit=JsonlAuditSink(str(tmp_path / "audit.jsonl")), tmp=tmp_path))


def log(e):
    return [json.loads(x) for x in open(e.audit.path, encoding="utf-8") if x.strip()]


def test_the_default_is_no_grant(env):
    assert local_inspect_grants(env.grants, "customer-ops") == []
    assert env.grants.list() == []


def test_grant_adds_one_read_grant_on_the_exact_permission_for_the_local_owner(env):
    g = grant_local_inspect(env.grants, "customer-ops", operator="alex", audit=env.audit)
    assert (g.subject_kind, g.subject, g.resource_kind, g.resource_name, g.actions) == ("user", LOCAL_OWNER_ID, "tool", "evidence.customer-ops.inspect", ["read"])
    assert g.created_by == "operator:alex" and g.row_scope == "all"
    assert [x.id for x in local_inspect_grants(env.grants, "customer-ops")] == [g.id]
    assert local_inspect_grants(env.grants, "other") == []


def test_grant_is_recorded_with_who_what_and_the_grant_id(env):
    g = grant_local_inspect(env.grants, "customer-ops", operator="alex", audit=env.audit)
    (r,) = log(env)
    assert r["kind"] == "drilldown.grant" and r["principal"] == "operator:alex" and r["scope"] == "customer-ops" and r["outcome"] == "ok"
    assert r["subject"] == LOCAL_OWNER_ID and r["permission"] == "evidence.customer-ops.inspect" and r["grant_id"] == g.id and r["side_effects"] == 0
    assert env.audit.verify() == (True, 1)


def test_granting_twice_does_not_stack_grants(env):
    a = grant_local_inspect(env.grants, "customer-ops", operator="alex", audit=env.audit)
    b = grant_local_inspect(env.grants, "customer-ops", operator="alex", audit=env.audit)
    assert a.id == b.id and len(local_inspect_grants(env.grants, "customer-ops")) == 1
    assert [r["outcome"] for r in log(env)] == ["ok", "unchanged"]


def test_a_grant_whose_record_cannot_be_written_does_not_stay(env):
    class Down:
        def append(self, receipt):
            raise OSError("down")
    with pytest.raises(GrantRefused):
        grant_local_inspect(env.grants, "customer-ops", operator="alex", audit=Down())
    assert env.grants.list() == []                                          # no grant without its record


def test_revoke_removes_the_grant_and_records_it(env):
    g = grant_local_inspect(env.grants, "customer-ops", operator="alex", audit=env.audit)
    assert revoke_local_inspect(env.grants, "customer-ops", operator="sam", audit=env.audit) == 1
    assert local_inspect_grants(env.grants, "customer-ops") == []
    r = log(env)[-1]
    assert r["kind"] == "drilldown.revoke" and r["principal"] == "operator:sam" and r["grant_ids"] == [g.id] and r["outcome"] == "ok"
    assert revoke_local_inspect(env.grants, "customer-ops", operator="sam", audit=env.audit) == 0
    assert log(env)[-1]["outcome"] == "none"
    assert env.audit.verify() == (True, 3)


def test_revoke_only_touches_the_local_owners_grant_for_that_project(env):
    env.grants.add("user", "someone-else", "tool", "evidence.customer-ops.inspect", actions=["read"], created_by="t")
    env.grants.add("user", LOCAL_OWNER_ID, "tool", "evidence.other.inspect", actions=["read"], created_by="t")
    grant_local_inspect(env.grants, "customer-ops", operator="alex", audit=env.audit)
    revoke_local_inspect(env.grants, "customer-ops", operator="alex", audit=env.audit)
    assert {(g.subject, g.resource_name) for g in env.grants.list()} == {("someone-else", "evidence.customer-ops.inspect"), (LOCAL_OWNER_ID, "evidence.other.inspect")}


def test_revoke_survives_an_audit_failure_and_says_so(env):
    grant_local_inspect(env.grants, "customer-ops", operator="alex", audit=env.audit)

    class Down:
        def append(self, receipt):
            raise OSError("down")
    with pytest.raises(GrantRefused) as e:
        revoke_local_inspect(env.grants, "customer-ops", operator="alex", audit=Down())
    assert "revoked" in str(e.value) and local_inspect_grants(env.grants, "customer-ops") == []     # the safe direction stands


@pytest.mark.parametrize("bad", ["", "Bad Project", "a/b", "x" * 80, "..", "evidence", "a.b", None, 5])
def test_a_project_id_that_is_not_a_scope_is_refused(env, bad):
    with pytest.raises(GrantRefused):
        grant_local_inspect(env.grants, bad, operator="alex", audit=env.audit)
    assert env.grants.list() == []


def test_the_operator_name_is_required_and_bounded(env):
    for bad in ("", "  ", "x" * 200, "a\nb"):
        with pytest.raises(GrantRefused):
            grant_local_inspect(env.grants, "customer-ops", operator=bad, audit=env.audit)


# ───────────────────────── the command line ─────────────────────────
def run(argv, capsys):
    rc = cli.main(argv)
    return rc, capsys.readouterr()


def test_cli_grant_list_and_revoke_round_trip(env, capsys):
    base = ["permissions"]
    a = ["--dir", env.grants.dir, "--audit", env.audit.path, "--project", "customer-ops"]
    rc, out = run([*base, "allow-drilldown", *a], capsys)
    assert rc == 0 and "evidence.customer-ops.inspect" in out.out and LOCAL_OWNER_ID in out.out
    assert len(local_inspect_grants(GrantStore(env.grants.dir), "customer-ops")) == 1
    rc, out = run([*base, "revoke-drilldown", *a], capsys)
    assert rc == 0 and "revoked 1" in out.out
    assert local_inspect_grants(GrantStore(env.grants.dir), "customer-ops") == []
    assert [r["kind"] for r in log(env)] == ["drilldown.grant", "drilldown.revoke"]
    assert all(r["principal"].startswith("operator:") for r in log(env))


def test_cli_refuses_a_bad_project_and_a_missing_audit_path(env, capsys):
    rc, out = run(["permissions", "allow-drilldown", "--dir", env.grants.dir, "--audit", env.audit.path, "--project", "Not A Project"], capsys)
    assert rc == 2 and env.grants.list() == []
    with pytest.raises(SystemExit):
        cli.main(["permissions", "allow-drilldown", "--dir", env.grants.dir, "--project", "customer-ops"])        # --audit is required: no unrecorded grant

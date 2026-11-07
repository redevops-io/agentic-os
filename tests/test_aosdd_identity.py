"""AOS-DD operator identity (item 8): the base AuthenticatedPrincipal, issued from a credential through the base control-plane path."""
from __future__ import annotations

import argparse
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _principals import authn  # noqa: E402
from aosdd_fakes import FakeStack  # noqa: E402
from test_aosdd_runtime import ADM, APP, REF, REQ, TENANT, Env, go, state  # noqa: E402

from agentic_os.integrations.aosdd import cli
from agentic_os.integrations.aosdd.identity import AuthenticationFailed, OperatorAuth
from agentic_os.integrations.aosdd.runtime import RunnerError
from agentic_os.mission.principal import PrincipalIssuer
from agentic_os.mission.runtime import ResumeNotAuthorized
from agentic_os.mission.types import MissionState
from agentic_os.overlays import Principal

TOK_R, TOK_A, TOK_ADMIN = "r" * 40, "a" * 40, "z" * 40


def auth():
    return OperatorAuth(admin_token=TOK_ADMIN, tenant_tokens={TENANT: TOK_R}, approver_tokens={TENANT: TOK_A})


def test_the_base_control_plane_path_issues_the_principal_from_the_credential():
    a = auth()
    r, p, adm = a.authenticate(TOK_R), a.authenticate(TOK_A), a.authenticate(TOK_ADMIN)
    assert (r.id, r.roles, r.tenant, r.method) == (f"requester:{TENANT}", ("requester",), TENANT, "control-token")
    assert (p.id, p.roles, p.tenant) == (f"approver:{TENANT}", ("approver",), TENANT)
    assert (adm.id, adm.roles, adm.tenant) == ("admin", ("admin",), "*")
    for bad in ("", "nope" * 10, TOK_R[:-1]):
        with pytest.raises(AuthenticationFailed):
            a.authenticate(bad)
    with pytest.raises(AuthenticationFailed):                                                       # base ConfigError: two roles sharing a token is refused
        OperatorAuth(tenant_tokens={TENANT: TOK_R}, approver_tokens={TENANT: TOK_R}).authenticate(TOK_R)
    assert os.environ.get("AGENTIC_OS_CONTROL_TOKEN") is None and os.environ.get("AGENTIC_OS_APPROVER_TOKENS") is None      # the process env is left as it was


def test_operator_credentials_file_must_be_private(tmp_path):
    p = tmp_path / "ops.json"
    p.write_text(json.dumps({"schema": "aosdd.operators/1", "requester_tokens": {TENANT: TOK_R}, "approver_tokens": {TENANT: TOK_A}}))
    os.chmod(p, 0o644)
    with pytest.raises(AuthenticationFailed, match="chmod 600"):
        OperatorAuth.from_file(str(p))
    os.chmod(p, 0o600)
    assert OperatorAuth.from_file(str(p)).authenticate(TOK_A).id == f"approver:{TENANT}"


def test_a_free_text_actor_a_plain_principal_or_a_foreign_issuer_is_not_an_identity(tmp_path):
    env = Env(tmp_path)
    r = env.runner()
    foreign = PrincipalIssuer("someone-else").issue(Principal("approver:x", "user", ("approver",), TENANT), method="forged")
    plain = Principal("approver:aosdd-test", "user", ("approver",), TENANT)
    for who in (None, "alice@example", plain, foreign):
        with pytest.raises(RunnerError, match="authenticated operator principal"):
            r.submit("X", REF, principal=who)
    assert r.svc.store.mission_ids() == []
    mid = r.submit("X", REF, principal=REQ)
    for who in (None, "alice", plain, foreign):
        with pytest.raises(RunnerError, match="authenticated operator principal"):
            r.approve_plan(mid, r.plan(mid)["plan_sha256"], principal=who)
    assert env.stack.writes() == [] and state(r, mid) is MissionState.WAITING_HUMAN


def test_roles_decide_who_may_do_what(tmp_path):
    env = Env(tmp_path)
    r = env.runner()
    with pytest.raises(RunnerError, match="may not submit"):
        r.submit("ROLE", REF, principal=APP)                                                          # an approver does not start missions
    mid = r.submit("ROLE", REF, principal=REQ)
    with pytest.raises(RunnerError, match="may not approve"):
        r.approve_plan(mid, r.plan(mid)["plan_sha256"], principal=REQ)                               # a requester does not approve its own plan
    with pytest.raises(RunnerError, match="may not revoke"):
        r.revoke(mid, "nope", principal=REQ)
    other = authn("approver:other-tenant", tenant="other-tenant", roles=("approver",))
    with pytest.raises(RunnerError, match="another tenant"):
        r.approve_plan(mid, r.plan(mid)["plan_sha256"], principal=other)
    assert env.stack.writes() == []
    r.approve_plan(mid, r.plan(mid)["plan_sha256"], principal=ADM)                                   # the all-tenant admin may
    assert state(r, mid) is MissionState.SUCCEEDED


def test_approval_and_resume_records_carry_the_principal_not_a_name(tmp_path):
    env = Env(tmp_path)
    env.stack.crash("lago", "PUT", "/api/v1/invoices", after_effect=False)
    r = env.runner()
    mid = r.submit("REC", REF, principal=REQ)
    from aosdd_fakes import Crash
    with pytest.raises(Crash):
        r.approve_plan(mid, r.plan(mid)["plan_sha256"], principal=APP)
    r2 = env.runner()
    r2.resume(mid, principal=REQ, reason="restart after the crash")
    ev = r2.svc.store.for_mission(mid)
    appr = [e.payload for e in ev if e.type == "ApprovalGranted"][0]
    assert appr["principal"]["id"] == APP.id and appr["principal"]["method"] == "test-login" and "proof" not in json.dumps(appr)
    plan_ok = [e.payload for e in ev if e.type == "AosddPlanApproved"][0]
    assert plan_ok["claims"]["principal"]["id"] == APP.id and plan_ok["claims"]["approver"] == APP.id
    res = [e.payload for e in ev if e.type == "AosddResumeRequested"][0]
    assert res["principal"]["id"] == REQ.id
    assert [e.payload["principal"]["id"] for e in ev if e.type == "ReplayAuthorized"] == [REQ.id]


def test_a_different_principal_cannot_resume_on_anothers_grant(tmp_path):
    env = Env(tmp_path)
    env.stack.crash("lago", "PUT", "/api/v1/invoices", after_effect=False)
    r = env.runner()
    mid = r.submit("OWN", REF, principal=REQ)
    from aosdd_fakes import Crash
    with pytest.raises(Crash):
        r.approve_plan(mid, r.plan(mid)["plan_sha256"], principal=APP)
    r2 = env.runner()
    r2.attach(mid)
    assert mid in r2.rt._reconstructed
    n = len(env.stack.calls)
    with pytest.raises(ResumeNotAuthorized):
        r2.rt.run(mid, principal=REQ)                                                                # no resume grant yet: a rebuilt mission is read-only
    r2.resume(mid, principal=REQ, reason="requester resumes")                                        # REQ obtains the grant (and the mission continues to its next park)
    other = authn("requester:aosdd-test", tenant=TENANT, roles=("requester",))                         # same id, new issue: still REQ's identity
    r3 = env.runner()
    r3.attach(mid)
    r3.rt.grant_resume(mid, principal=REQ, ceiling=__import__("agentic_os.integrations.aosdd.identity", fromlist=["x"]).RoleCeiling(), scope=TENANT)
    with pytest.raises(ResumeNotAuthorized, match="another principal"):
        r3.rt.run(mid, principal=ADM)                                                                # a different principal does not ride on REQ's grant
    r3._live.add(mid)
    with pytest.raises(ResumeNotAuthorized, match="another principal"):
        r3.resume(mid, principal=ADM, reason="admin tries to use the requester's grant")
    assert len(env.stack.calls) >= n


def test_the_cli_authenticates_a_credential_and_has_no_actor_option(tmp_path):
    opsf = tmp_path / "operators.json"
    opsf.write_text(json.dumps({"schema": "aosdd.operators/1", "requester_tokens": {TENANT: TOK_R}, "approver_tokens": {TENANT: TOK_A}}))
    os.chmod(opsf, 0o600)
    tokf = tmp_path / "token"
    tokf.write_text(TOK_A + "\n")
    os.chmod(tokf, 0o600)
    ns = argparse.Namespace(operator_auth=str(opsf), operator_token_file=str(tokf))
    assert cli.operator(ns).id == f"approver:{TENANT}"
    os.chmod(tokf, 0o644)
    with pytest.raises(AuthenticationFailed, match="chmod 600"):
        cli.operator(ns)
    os.chmod(tokf, 0o600)
    tokf.write_text("wrong" * 10)
    with pytest.raises(AuthenticationFailed):
        cli.operator(ns)
    with pytest.raises(AuthenticationFailed, match="--operator-auth"):
        cli.operator(argparse.Namespace(operator_auth=None, operator_token_file=None))
    for argv in (["--state", str(tmp_path), "approve", "--mission", "m", "--plan-sha256", "h", "--actor", "alice"],
                 ["--state", str(tmp_path), "resume", "--mission", "m", "--actor", "alice"]):
        with pytest.raises(SystemExit):
            cli.main(argv)                                                                          # free-text --actor is not accepted anywhere

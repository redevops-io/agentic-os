"""SEC-ACC-02 / SEC-ACC-03 evidence from Entra ID and Azure RBAC (response shapes taken from a live tenant, 2026-10-24): privileged accounts (directory roles, Azure roles, service principals), review and leaver logs written by people,
revocation read from the directory audit log, any unreadable page or role is a gap, nothing but read-only paths can run, and the stored evidence assesses as PASS / FAIL for the right reasons."""
from __future__ import annotations

import importlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest

A = importlib.import_module("edge-sentinel.soc2.assess")
P = importlib.import_module("edge-sentinel.soc2.pipeline")
E = importlib.import_module("edge-sentinel.soc2.sources.entra_access")
kit = importlib.import_module("edge-sentinel.soc2._testkit")
NOW, DAY = kit.NOW, kit.DAY
HI = (NOW // DAY) * DAY
PERIOD = (HI - 30 * DAY, HI)
G = E.GRAPH


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def user(oid, upn, enabled=True):
    return {"id": oid, "userPrincipalName": upn, "accountEnabled": enabled, "userType": "Member", "createdDateTime": "2026-08-19T01:02:31Z"}


def member(oid, upn):
    return {"@odata.type": "#microsoft.graph.user", "id": oid, "userPrincipalName": upn, "displayName": upn}


def audit(when, name, oid, upn=None, props=()):
    return {"activityDateTime": iso(when), "activityDisplayName": name, "category": "UserManagement", "result": "success",
            "targetResources": [{"id": oid, "userPrincipalName": upn, "type": "User", "modifiedProperties": [{"displayName": d, "oldValue": "[true]", "newValue": nv} for d, nv in props]}]}


def rbac(pid, role, ptype="User"):
    return {"principalId": pid, "principalType": ptype, "roleDefinitionName": role, "scope": "/subscriptions/s"}


class Tenant:
    """A fake `az` over the four reads, with the shapes the real services return."""
    def __init__(self, users=(), roles=None, role_members=None, assignments=(), audits=(), pages=None, fail=None):
        self.users, self.roles, self.members, self.assignments, self.audits = list(users), roles if roles is not None else [{"id": "r1", "displayName": "Global Administrator"}, {"id": "r2", "displayName": "Reports Reader"}], role_members or {}, list(assignments), list(audits)
        self.pages, self.fail, self.seen = pages, fail, []

    def __call__(self, args):
        self.seen.append(list(args))
        if self.fail and self.fail(args):
            return 1, "", "ERROR: Forbidden"
        if args[:3] == ["role", "assignment", "list"]:
            return 0, json.dumps(self.assignments), ""
        url = args[4]
        rest = url[len(G):]
        if rest.startswith("users"):
            if self.pages:                                              # split into pages of this size, linked by @odata.nextLink
                skip = int(url.split("skip=")[1]) if "skip=" in url else 0
                chunk = self.users[skip:skip + self.pages]
                d = {"value": chunk}
                if skip + self.pages < len(self.users):
                    d["@odata.nextLink"] = G + f"users?$skip={skip + self.pages}"
                return 0, json.dumps(d), ""
            return 0, json.dumps({"value": self.users}), ""
        if rest.startswith("directoryRoles/"):
            rid = rest.split("/")[1]
            return 0, json.dumps({"value": self.members.get(rid, [])}), ""
        if rest.startswith("directoryRoles"):
            return 0, json.dumps({"value": self.roles}), ""
        if rest.startswith("auditLogs/directoryAudits"):
            return 0, json.dumps({"value": self.audits}), ""
        return 1, "", "unknown"


def logs(tmp_path):
    return E.ReviewLog(str(tmp_path / "rv.db"), clock=kit.Clock()), E.LeaverLog(str(tmp_path / "lv.db"), clock=kit.Clock())


def col(tmp_path, tenant):
    rv, lv = logs(tmp_path)
    return E.EntraAccess(tenant, rv, lv, clock=lambda: WT), rv, lv


WF, WT = HI - 3 * DAY, HI - 2 * DAY


# ───────────────────────── the collector ─────────────────────────
def test_privileged_accounts_come_from_directory_roles_azure_roles_and_service_principals(tmp_path):
    t = Tenant(users=[user("u1", "ada@acme.test"), user("u2", "bob@acme.test"), user("u3", "cy@acme.test")], role_members={"r1": [member("u1", "ada@acme.test")]},
               assignments=[rbac("u2", "Owner"), rbac("u3", "Reader"), rbac("sp1", "Contributor", "ServicePrincipal")])
    c, _, _ = col(tmp_path, t)
    by = {a["id"]: a for a in c.collect(WF, WT).payload["accounts"]}
    assert by["ada@acme.test"]["privileged"] and by["ada@acme.test"]["roles"] == ["directory:Global Administrator"]
    assert by["bob@acme.test"]["privileged"] and by["bob@acme.test"]["roles"] == ["azure:Owner"]
    assert not by["cy@acme.test"]["privileged"]
    assert by["sp1"]["privileged"] and by["sp1"]["kind"] == "ServicePrincipal"


def test_a_role_that_is_not_in_the_privileged_list_does_not_make_its_holders_privileged(tmp_path):
    t = Tenant(users=[user("u1", "ada@acme.test")], role_members={"r2": [member("u1", "ada@acme.test")]})
    assert not col(tmp_path, t)[0].collect(WF, WT).payload["accounts"][0]["privileged"]


def test_the_user_list_is_followed_across_pages(tmp_path):
    t = Tenant(users=[user(f"u{i}", f"p{i}@acme.test") for i in range(7)], pages=3)
    assert len(col(tmp_path, t)[0].collect(WF, WT).payload["accounts"]) == 7


@pytest.mark.parametrize("failing", ["users", "directoryRoles", "directoryRoles/r1/members", "auditLogs", "role"])
def test_any_unreadable_part_is_a_gap_never_a_partial_population(tmp_path, failing):
    t = Tenant(users=[user("u1", "a@acme.test")], role_members={"r1": [member("u1", "a@acme.test")]},
               fail=lambda a: (a[0] == "role" and failing == "role") or (a[0] == "rest" and failing != "role" and a[4][len(G):].startswith(failing) and (failing != "directoryRoles" or a[4].endswith("directoryRoles"))))
    with pytest.raises(P.CollectionFailed):
        col(tmp_path, t)[0].collect(WF, WT)


def test_a_page_that_points_outside_graph_is_refused_and_a_never_ending_listing_stops(tmp_path):
    class Evil(Tenant):
        def __call__(self, args):
            if args[0] == "rest" and args[4].startswith(G + "users"):
                return 0, json.dumps({"value": [], "@odata.nextLink": "https://evil.example/steal"}), ""
            return super().__call__(args)
    with pytest.raises(P.CollectionFailed, match="outside graph"):
        col(tmp_path, Evil())[0].collect(WF, WT)

    class Forever(Tenant):
        def __call__(self, args):
            if args[0] == "rest" and args[4].startswith(G + "users"):
                return 0, json.dumps({"value": [], "@odata.nextLink": G + "users?$skip=1"}), ""
            return super().__call__(args)
    with pytest.raises(P.CollectionFailed, match="page limit"):
        col(tmp_path, Forever())[0].collect(WF, WT)


def test_a_runner_that_cannot_run_az_is_a_gap_and_the_error_text_is_not_leaked(tmp_path):
    def boom(args):
        raise FileNotFoundError("az with token ey123456789012345678")
    rv, lv = logs(tmp_path)
    with pytest.raises(P.CollectionFailed) as e:
        E.EntraAccess(boom, rv, lv).collect(WF, WT)
    assert "ey123456789012345678" not in str(e.value)


def test_the_default_runner_runs_only_read_only_graph_paths_and_the_rbac_listing():
    run = E.default_runner()
    for bad in (["rest", "--method", "post", "--url", G + "users"], ["rest", "--method", "get", "--url", "https://evil.example/users"], ["rest", "--method", "get", "--url", G + "applications"],
                ["rest", "--method", "get", "--url", G + "users", "--body", "{}"], ["role", "assignment", "create", "--all"], ["role", "assignment", "delete", "--all"], ["role", "assignment", "list"], ["ad", "user", "delete"], []):
        with pytest.raises(PermissionError):
            run(bad)


def test_the_default_runner_runs_az_for_a_read_and_asks_for_json(monkeypatch):
    import subprocess as sp
    seen = {}

    def fake(cmd, **kw):
        seen["cmd"] = cmd
        return sp.CompletedProcess(cmd, 0, stdout="{}", stderr="")
    monkeypatch.setattr(E.subprocess, "run", fake)
    assert E.default_runner()(["rest", "--method", "get", "--url", G + "users"])[0] == 0
    assert seen["cmd"][0] == "az" and seen["cmd"][-2:] == ["-o", "json"]


# ───────────────────────── the logs people write ─────────────────────────
def test_a_review_needs_a_reviewer_who_is_not_the_account_a_decision_and_a_note_and_the_log_is_append_only(tmp_path):
    rv, lv = logs(tmp_path)
    for bad in (dict(account="a@x", reviewer="a@x", decision="retain", note="checked the roles"), dict(account="a@x", reviewer="A@X", decision="retain", note="checked the roles"),
                dict(account="a@x", reviewer="r@x", decision="maybe", note="checked the roles"), dict(account="a@x", reviewer="r@x", decision="retain", note="ok"), dict(account="", reviewer="r@x", decision="retain", note="checked the roles")):
        with pytest.raises(ValueError):
            rv.record(**bad)
    rv.record(account="a@x", reviewer="r@x", decision="retain", note="checked the roles")
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(str(tmp_path / "rv.db")).execute("UPDATE reviews SET decision='remove'")
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(str(tmp_path / "lv.db")).execute("DELETE FROM leavers") if lv.record(account="a", terminated_at=1.0, declared_by="hr", ticket="T-1") is None else None


def test_the_latest_review_made_by_the_time_of_collection_is_the_one_used(tmp_path):
    t = Tenant(users=[user("u1", "ada@acme.test")], role_members={"r1": [member("u1", "ada@acme.test")]})
    c, rv, _ = col(tmp_path, t)
    rv.record(account="ada@acme.test", reviewer="sec@acme.test", decision="retain", note="roles still needed", at=WT - DAY)
    rv.record(account="ada@acme.test", reviewer="cto@acme.test", decision="retain", note="roles still needed", at=WT - 5)
    rv.record(account="ada@acme.test", reviewer="late@acme.test", decision="retain", note="after the window", at=WT + 5)
    a = c.collect(WF, WT).payload["accounts"][0]
    assert (a["reviewed_by"], a["reviewed_at"]) == ("cto@acme.test", WT - 5)


# ───────────────────────── leavers ─────────────────────────
def test_a_leaver_disabled_within_a_day_has_a_revocation_time_from_the_audit_log(tmp_path):
    t = Tenant(users=[user("u9", "gone@acme.test", enabled=False)], audits=[audit(WF + 3600, "Update user", "u9", "gone@acme.test", props=[("AccountEnabled", "[false]")])])
    c, _, lv = col(tmp_path, t)
    lv.record(account="gone@acme.test", terminated_at=WF, declared_by="hr@acme.test", ticket="HR-12")
    a = [x for x in c.collect(WF, WT).payload["accounts"] if x["id"] == "gone@acme.test"][0]
    assert a["status"] == "terminated" and a["terminated_at"] == WF and a["access_revoked_at"] == WF + 3600 and a["access_removal_basis"] == "entra audit log"


def test_a_deleted_leaver_is_found_through_the_leaver_log_and_the_delete_event(tmp_path):
    t = Tenant(users=[], audits=[audit(WF + 600, "Delete user", "u9", "gone@acme.test")])
    c, _, lv = col(tmp_path, t)
    lv.record(account="gone@acme.test", terminated_at=WF, declared_by="hr@acme.test", ticket="HR-12")
    a = c.collect(WF, WT).payload["accounts"][0]
    assert a["status"] == "terminated" and a["access_revoked_at"] == WF + 600


def test_a_leaver_whose_account_is_still_enabled_has_no_revocation_and_says_why(tmp_path):
    t = Tenant(users=[user("u9", "gone@acme.test", enabled=True)])
    c, _, lv = col(tmp_path, t)
    lv.record(account="gone@acme.test", terminated_at=WF, declared_by="hr@acme.test", ticket="HR-12")
    a = c.collect(WF, WT).payload["accounts"][0]
    assert a["access_revoked_at"] is None and a["access_removal_basis"] == "account still enabled"


def test_a_disabled_leaver_with_no_audit_event_is_reported_as_not_shown_not_as_retained(tmp_path):
    t = Tenant(users=[user("u9", "gone@acme.test", enabled=False)])
    c, _, lv = col(tmp_path, t)
    lv.record(account="gone@acme.test", terminated_at=WF, declared_by="hr@acme.test", ticket="HR-12")
    a = c.collect(WF, WT).payload["accounts"][0]
    assert a["access_revoked_at"] is None and "when is not shown" in a["access_removal_basis"]


def test_only_a_successful_disabling_or_delete_event_counts_not_other_updates_or_failures(tmp_path):
    ev_fail = audit(WF + 10, "Delete user", "u9", "gone@acme.test"); ev_fail["result"] = "failure"
    t = Tenant(users=[user("u9", "gone@acme.test")], audits=[audit(WF + 5, "Update user", "u9", "gone@acme.test", props=[("DisplayName", "[x]")]),
                                                           audit(WF + 6, "Update user", "u9", "gone@acme.test", props=[("AccountEnabled", "[true]")]), ev_fail])
    c, _, lv = col(tmp_path, t)
    lv.record(account="gone@acme.test", terminated_at=WF, declared_by="hr@acme.test", ticket="HR-12")
    assert c.collect(WF, WT).payload["accounts"][0]["access_revoked_at"] is None


# ───────────────────────── through the pipeline and the same assessor ─────────────────────────
def assess(tmp_path, name, tenant, setup, control):
    d = tmp_path / name
    d.mkdir()
    rv, lv = logs(d)
    setup(rv, lv)
    v = kit.make_vault(d)
    p = P.Pipeline(v, lambda k: kit.SECRET_A, clock=kit.Clock())
    rep = p.run([P.Job(E.EntraAccess(tenant, rv, lv, clock=kit.Clock()), "acme", "prod", "collector-a", "key-a", window_s=DAY)], PERIOD[0], PERIOD[1])           # one snapshot per day, like the other sources
    return rep, A.Assessor(v, clock=kit.Clock()).assess(control, "acme", "prod", *PERIOD)


ADA = dict(users=[user("u1", "ada@acme.test")], role_members={"r1": [member("u1", "ada@acme.test")]})


def test_a_privileged_account_reviewed_in_the_period_by_someone_else_passes_the_access_review(tmp_path):
    rep, a = assess(tmp_path, "ok", Tenant(**ADA), lambda rv, lv: rv.record(account="ada@acme.test", reviewer="sec@acme.test", decision="retain", note="roles still needed", at=HI - 5 * DAY), "SEC-ACC-02")
    assert not rep.gaps() and a.result == A.PASS


def test_a_privileged_account_with_no_review_fails_the_access_review(tmp_path):
    _, a = assess(tmp_path, "noreview", Tenant(**ADA), lambda rv, lv: None, "SEC-ACC-02")
    assert a.result == A.FAIL and "no access review" in a.failures[0]


def test_a_review_before_the_period_fails_it(tmp_path):
    _, a = assess(tmp_path, "stale", Tenant(**ADA), lambda rv, lv: rv.record(account="ada@acme.test", reviewer="sec@acme.test", decision="retain", note="roles still needed", at=PERIOD[0] - DAY), "SEC-ACC-02")
    assert a.result == A.FAIL and "outside the period" in a.failures[0]


def test_a_leaver_removed_within_a_day_passes_and_one_still_enabled_fails_the_leaver_control(tmp_path):
    ok = Tenant(users=[user("u9", "gone@acme.test", enabled=False)], audits=[audit(HI - 10 * DAY + 3600, "Update user", "u9", "gone@acme.test", props=[("AccountEnabled", "[false]")])])
    _, a = assess(tmp_path, "left-ok", ok, lambda rv, lv: lv.record(account="gone@acme.test", terminated_at=HI - 10 * DAY, declared_by="hr@acme.test", ticket="HR-1"), "SEC-ACC-03")
    assert a.result == A.PASS
    bad = Tenant(users=[user("u9", "gone@acme.test", enabled=True)])
    _, b = assess(tmp_path, "left-bad", bad, lambda rv, lv: lv.record(account="gone@acme.test", terminated_at=HI - 10 * DAY, declared_by="hr@acme.test", ticket="HR-1"), "SEC-ACC-03")
    assert b.result == A.FAIL and "more than a day" in b.failures[0]


def test_no_leavers_in_the_period_is_a_pass_because_an_empty_complete_population_is_normal(tmp_path):
    _, a = assess(tmp_path, "noleavers", Tenant(**ADA), lambda rv, lv: None, "SEC-ACC-03")
    assert a.result == A.PASS

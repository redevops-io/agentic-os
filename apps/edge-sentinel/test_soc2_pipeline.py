"""The evidence pipeline: a failed collection is a gap, never evidence; windows are completed and never double-counted; GitHub collectors read only what the API documents and treat a refused
read as a gap; and what the pipeline stores goes through the same assessor and ends in the right result."""
from __future__ import annotations

import importlib
import json

import pytest

A = importlib.import_module("edge-sentinel.soc2.assess")
P = importlib.import_module("edge-sentinel.soc2.pipeline")
GH = importlib.import_module("edge-sentinel.soc2.sources.github")
HB = importlib.import_module("edge-sentinel.soc2.sources.heartbeat")
kit = importlib.import_module("edge-sentinel.soc2._testkit")
NOW, DAY, PERIOD = kit.NOW, kit.DAY, kit.PERIOD
SECRET = kit.SECRET_A
TOKEN = "ghp_SYNTHETICTOKEN0123456789abcdef"
AHI = (NOW // DAY) * DAY                                   # windows are day-aligned and only completed days are collected, so these tests assess a day-aligned period
APERIOD = (AHI - 30 * DAY, AHI)


def pipe(tmp_path, **kw):
    v = kit.make_vault(tmp_path)
    events = []
    return v, P.Pipeline(v, lambda key_id: SECRET if key_id == "key-a" else (_ for _ in ()).throw(KeyError(key_id)), clock=kit.Clock(), on_event=events.append, **kw), events


def job(collector, **kw):
    return P.Job(collector, "acme", "prod", "collector-a", "key-a", **kw)


class Stub:
    kind, source, name = "heartbeat", "stub", "stub"

    def __init__(self, fn):
        self.fn, self.calls = fn, []

    def collect(self, a, b):
        self.calls.append((a, b))
        return self.fn(a, b)


# ───────────────────────── the pipeline ─────────────────────────
def test_only_completed_windows_aligned_to_the_window_size_are_due(tmp_path):
    _, p, _ = pipe(tmp_path)
    j = job(Stub(lambda a, b: P.Collected({"population_complete": True})), window_s=DAY)
    w = p.windows(j, NOW - 3 * DAY - 100, NOW + 5 * DAY)            # asks for the future: the unfinished window is never included
    assert all(b - a == DAY for a, b in w) and all(b <= NOW for _, b in w) and len(w) in (3, 4)
    assert all(a % DAY == 0 for a, _ in w)


def test_a_collected_window_lands_in_the_vault_signed_and_covering_exactly_that_window(tmp_path):
    v, p, _ = pipe(tmp_path)
    rep = p.run([job(Stub(lambda a, b: P.Collected({"beats": 9, "population_complete": True})))], NOW - 2 * DAY, NOW)
    got = rep.by_status("collected")
    assert len(got) >= 2 and not rep.gaps()
    for r in got:
        ok, why = v.verify(r.artifact_id)
        assert ok, why
    s = v.find("acme", scope="prod", kind="heartbeat")
    assert {(x.coverage_from, x.coverage_to) for x in s} == {r.window for r in got}


def test_a_failed_collection_leaves_a_gap_and_no_evidence_and_says_so(tmp_path):
    v, p, events = pipe(tmp_path, redact=(TOKEN,))
    def boom(a, b):
        raise P.CollectionFailed(f"HTTP 403 for token {TOKEN}")
    rep = p.run([job(Stub(boom))], NOW - DAY, NOW)
    assert rep.gaps() and not rep.by_status("collected") and v.find("acme", kind="heartbeat") == []
    assert TOKEN not in json.dumps([r.__dict__ for r in rep.records], default=str) and TOKEN not in json.dumps(events)
    assert events and events[0]["type"] == "EVIDENCE_COLLECTION_GAP"


def test_a_registered_source_token_is_redacted_even_where_no_keyword_gives_it_away(tmp_path):
    v, p, events = pipe(tmp_path, redact=(TOKEN,))
    def boom(a, b):
        raise P.CollectionFailed(f"GET https://api.example.test/{TOKEN}/members returned 502")
    rep = p.run([job(Stub(boom))], NOW - DAY, NOW)
    assert rep.gaps() and TOKEN not in rep.gaps()[0].detail and TOKEN not in json.dumps(events) and "[redacted]" in rep.gaps()[0].detail


def test_an_unexpected_error_in_a_collector_is_a_gap_not_a_finding(tmp_path):
    v, p, _ = pipe(tmp_path)
    rep = p.run([job(Stub(lambda a, b: 1 / 0))], NOW - DAY, NOW)
    assert rep.gaps()[0].status == "failed" and "ZeroDivisionError" in rep.gaps()[0].detail and v.find("acme") == []


def test_a_collector_that_returns_nothing_is_a_gap(tmp_path):
    _, p, _ = pipe(tmp_path)
    assert p.run([job(Stub(lambda a, b: None))], NOW - DAY, NOW).gaps()


def test_a_rerun_does_not_collect_covered_windows_again_and_cannot_double_count(tmp_path):
    v, p, _ = pipe(tmp_path)
    c = Stub(lambda a, b: P.Collected({"n": 1, "population_complete": True}))
    j = job(c)
    p.run([j], NOW - 2 * DAY, NOW)
    first = len(c.calls)
    rep = p.run([j], NOW - 2 * DAY, NOW)
    assert len(c.calls) == first and len(rep.by_status("skipped_covered")) == first and len(v.find("acme", kind="heartbeat")) == first


def test_a_window_the_vault_refuses_is_a_rejected_gap(tmp_path):
    v, p, _ = pipe(tmp_path)
    j = P.Job(Stub(lambda a, b: P.Collected({"population_complete": True})), "initech", "prod", "collector-a", "key-a")      # the key may not write for this tenant
    rep = p.run([j], NOW - DAY, NOW)
    assert rep.gaps() and rep.gaps()[0].status == "rejected" and "tenant_not_allowed" in rep.gaps()[0].detail


def test_a_missing_signing_key_is_a_gap_and_the_source_is_not_even_trusted_with_a_write(tmp_path):
    v, p, _ = pipe(tmp_path)
    j = P.Job(Stub(lambda a, b: P.Collected({"population_complete": True})), "acme", "prod", "collector-a", "key-missing")
    rep = p.run([j], NOW - DAY, NOW)
    assert rep.gaps()[0].status == "failed" and v.find("acme") == []


def test_an_observer_that_raises_does_not_change_what_was_collected(tmp_path):
    v = kit.make_vault(tmp_path)
    p = P.Pipeline(v, lambda k: SECRET, clock=kit.Clock(), on_event=lambda e: 1 / 0)
    rep = p.run([job(Stub(lambda a, b: (_ for _ in ()).throw(P.CollectionFailed("x"))))], NOW - DAY, NOW)
    assert rep.gaps()


# ───────────────────────── heartbeat ─────────────────────────
def beats(every=200.0, lo=APERIOD[0], hi=AHI, skip=()):
    t, out = lo, []
    while t < hi:
        if not any(a <= t < b for a, b in skip):
            out.append(t)
        t += every
    return out


def run_monitor(tmp_path, skip=()):
    log = HB.HeartbeatLog(str(tmp_path / "beats.db"))
    log.beat_many(beats(skip=skip))
    v, p, _ = pipe(tmp_path)
    rep = p.run([job(HB.HeartbeatCollector(log, max_gap_s=600), window_s=DAY)], APERIOD[0], APERIOD[1])
    return v, rep, A.Assessor(v, clock=kit.Clock()).assess("SEC-MON-01", "acme", "prod", *APERIOD)


def test_a_monitor_that_ran_all_period_gives_coverage_that_assesses_as_a_pass(tmp_path):
    _, rep, a = run_monitor(tmp_path)
    assert not rep.gaps() and a.result == A.PASS and a.coverage >= 0.99


def test_a_day_of_monitor_downtime_is_a_gap_the_assessor_does_not_call_a_pass(tmp_path):
    out = (APERIOD[0] + 10 * DAY, APERIOD[0] + 11 * DAY)
    _, rep, a = run_monitor(tmp_path, skip=[out])
    assert len(rep.gaps()) == 1 and "no heartbeat" in rep.gaps()[0].detail
    assert a.result == A.INCONCLUSIVE and a.coverage < 0.99


def test_a_partial_outage_inside_a_window_makes_the_whole_window_a_gap(tmp_path):
    out = (APERIOD[0] + 5 * DAY + 3600, APERIOD[0] + 5 * DAY + 3600 + 3 * 3600)        # three silent hours
    _, rep, a = run_monitor(tmp_path, skip=[out])
    assert any("silent" in g.detail for g in rep.gaps()) and a.result == A.INCONCLUSIVE


# ───────────────────────── GitHub (a fake of the documented API) ─────────────────────────
def gh_ts(ts):
    return GH._iso(ts)


class FakeGitHub:
    def __init__(self):
        self.members, self.admins, self.no2fa, self.insecure = [], [], [], []
        self.prs, self.reviews, self.checks, self.protection = [], {}, {}, (200, {"checks": [{"context": "ci"}, {"context": "scan"}], "contexts": ["ci", "scan"]})
        self.commits, self.commit_prs = [], {}
        self.members_status = {}                              # filter -> status override
        self.requests = []

    def __call__(self, path, params):
        self.requests.append((path, dict(params)))
        page, per = int(params.get("page", 1)), int(params.get("per_page", 100))
        sl = lambda rows: rows[(page - 1) * per: page * per]                               # noqa: E731
        if path.endswith("/members"):
            f = params.get("filter")
            if f and self.members_status.get(f):
                return self.members_status[f], {"message": "Must be an organization owner"}, {}
            rows = self.no2fa if f == "2fa_disabled" else self.insecure if f == "2fa_insecure" else self.admins if params.get("role") == "admin" else self.members
            return 200, sl([{"login": x, "id": i, "type": "User"} for i, x in enumerate(rows)]), {}
        if path.endswith("/protection/required_status_checks"):
            return self.protection[0], self.protection[1], {}
        if path.endswith("/pulls") and "/commits/" not in path:
            return 200, sl(self.prs), {}
        if path.endswith("/reviews"):
            n = int(path.split("/")[-2])
            return 200, sl(self.reviews.get(n, [])), {}
        if path.endswith("/check-runs"):
            sha = path.split("/")[-2]
            return 200, {"total_count": len(self.checks.get(sha, [])), "check_runs": sl(self.checks.get(sha, []))}, {}
        if "/commits/" in path and path.endswith("/pulls"):
            return 200, self.commit_prs.get(path.split("/")[-2], []), {}
        if path.endswith("/commits"):
            lo, hi = params.get("since", ""), params.get("until", "9")
            return 200, sl([c for c in self.commits if lo <= c["commit"]["committer"]["date"] <= hi]), {}
        return 404, {}, {}


def pr(n, *, author="dev-1", merged=NOW - 3 * DAY, sha=None):
    return {"number": n, "user": {"login": author}, "merged_at": gh_ts(merged), "updated_at": gh_ts(merged), "merge_commit_sha": sha or f"{n:040x}"}


def runs(*pairs):
    return [{"name": n, "status": "completed", "conclusion": c} for n, c in pairs]


def collect_changes(gh, **kw):
    return GH.GithubChanges(["acme/app"], gh, **kw).collect(NOW - 7 * DAY, NOW)


def rel(c, i=0):
    return c.payload["releases"][i]


def test_a_merged_pr_with_an_independent_approval_and_passing_checks_is_recorded_as_such():
    gh = FakeGitHub()
    gh.prs = [pr(1)]
    gh.reviews = {1: [{"user": {"login": "dev-2"}, "state": "APPROVED"}]}
    gh.checks = {f"{1:040x}": runs(("ci", "success"), ("scan", "success"))}
    c = collect_changes(gh)
    assert rel(c) == {"id": "acme/app#1", "author": "dev-1", "approvers": ["dev-2"], "required_checks": ["ci", "scan"], "passed_checks": ["ci", "scan"], "deployed_at": NOW - 3 * DAY}
    assert c.payload["population_complete"] is True


def test_the_latest_review_by_each_person_decides_and_only_the_exact_approved_state_counts():
    gh = FakeGitHub()
    gh.prs = [pr(1)]
    gh.reviews = {1: [{"user": {"login": "a"}, "state": "APPROVED"}, {"user": {"login": "a"}, "state": "CHANGES_REQUESTED"},      # approved, then withdrew
                      {"user": {"login": "b"}, "state": "COMMENTED"}, {"user": {"login": "c"}, "state": "approved"},                  # not the documented value
                      {"user": {"login": "d"}, "state": "APPROVED"}]}
    assert rel(collect_changes(gh))["approvers"] == ["d"]


def test_only_successful_completed_check_runs_count_as_passed():
    gh = FakeGitHub()
    gh.prs = [pr(1)]
    gh.reviews = {1: [{"user": {"login": "dev-2"}, "state": "APPROVED"}]}
    gh.checks = {f"{1:040x}": runs(("ci", "success"), ("scan", "neutral"), ("lint", "skipped")) + [{"name": "e2e", "status": "in_progress", "conclusion": None}]}
    assert rel(collect_changes(gh))["passed_checks"] == ["ci"]


def test_a_direct_commit_with_no_pull_request_is_a_change_with_no_approval():
    gh = FakeGitHub()
    gh.commits = [{"sha": "ab" * 20, "commit": {"committer": {"date": gh_ts(NOW - DAY)}, "author": {"name": "Dev"}}, "author": {"login": "dev-1"}}]
    r = rel(collect_changes(gh))
    assert r["approvers"] == [] and r["author"] == "dev-1" and r["id"].startswith("acme/app@abababab")


def test_a_commit_that_belongs_to_a_merged_pull_request_is_not_a_second_change():
    gh = FakeGitHub()
    gh.prs = [pr(1, sha="cd" * 20)]
    gh.reviews = {1: [{"user": {"login": "dev-2"}, "state": "APPROVED"}]}
    gh.commits = [{"sha": "cd" * 20, "commit": {"committer": {"date": gh_ts(NOW - 3 * DAY)}}, "author": {"login": "dev-1"}},
                  {"sha": "ef" * 20, "commit": {"committer": {"date": gh_ts(NOW - 3 * DAY)}}, "author": {"login": "dev-1"}}]           # a rebased commit: found through its PR
    gh.commit_prs = {"ef" * 20: [{"number": 1, "merged_at": gh_ts(NOW - 3 * DAY)}]}
    assert len(collect_changes(gh).payload["releases"]) == 1


def test_a_pull_request_merged_outside_the_window_is_not_in_it():
    gh = FakeGitHub()
    gh.prs = [pr(1, merged=NOW - 40 * DAY), pr(2)]
    gh.reviews = {2: [{"user": {"login": "x"}, "state": "APPROVED"}]}
    assert [r["id"] for r in collect_changes(gh).payload["releases"]] == ["acme/app#2"]


@pytest.mark.parametrize("protection", [(404, {"message": "Branch not protected"}), (403, {"message": "Resource not accessible"}), (200, ["unexpected"])])
def test_unreadable_required_checks_are_a_gap_never_no_required_checks(protection):
    gh = FakeGitHub()
    gh.protection = protection
    gh.prs = [pr(1)]
    with pytest.raises(P.CollectionFailed):
        collect_changes(gh)


def test_a_list_cut_short_by_the_page_cap_says_the_population_is_not_complete():
    gh = FakeGitHub()
    gh.commits = [{"sha": f"{i:040x}", "commit": {"committer": {"date": gh_ts(NOW - DAY)}}, "author": {"login": "d"}} for i in range(450)]
    gh.commit_prs = {}
    c = GH.GithubChanges(["acme/app"], gh, max_pages=2).collect(NOW - 7 * DAY, NOW)         # 450 commits, 2 pages of 100 read
    assert c.payload["population_complete"] is False


def test_the_pull_request_walk_stops_at_the_start_of_the_window_instead_of_reading_all_history():
    gh = FakeGitHub()
    gh.prs = [pr(i, merged=NOW - (3 + i % 3) * DAY) for i in range(100)] + [pr(1000 + i, merged=NOW - 200 * DAY) for i in range(400)]
    GH.GithubChanges(["acme/app"], gh, max_pages=30).collect(NOW - 7 * DAY, NOW)
    pulls = [r for r in gh.requests if r[0].endswith("/pulls") and r[1].get("state") == "closed"]
    assert len(pulls) <= 3


def mfa(gh):
    return GH.GithubMfa("acme-org", gh).collect(NOW - DAY, NOW)


def test_mfa_status_marks_owners_as_privileged_and_anyone_on_the_2fa_disabled_list_as_without_mfa():
    gh = FakeGitHub()
    gh.members, gh.admins, gh.no2fa, gh.insecure = ["alice", "bob", "carol"], ["alice"], ["bob"], ["carol"]
    c = mfa(gh)
    by = {a["id"]: a for a in c.payload["accounts"]}
    assert by["alice"] == {"id": "alice", "privileged": True, "mfa": True, "status": "active"}
    assert by["bob"]["mfa"] is False and by["bob"]["privileged"] is False and by["carol"]["mfa"] is True
    assert c.payload["mfa_insecure"] == ["carol"] and c.payload["population_complete"] is True


def test_a_token_that_is_not_an_owner_gets_a_gap_never_everyone_has_mfa():
    gh = FakeGitHub()
    gh.members, gh.admins = ["alice"], ["alice"]
    gh.members_status = {"2fa_disabled": 403}
    with pytest.raises(P.CollectionFailed):
        mfa(gh)


def test_an_empty_member_list_is_a_gap_it_is_what_a_collector_without_access_also_sees():
    with pytest.raises(P.CollectionFailed):
        mfa(FakeGitHub())


def test_more_than_one_page_of_members_is_read_in_full():
    gh = FakeGitHub()
    gh.members = [f"u{i}" for i in range(230)]
    gh.admins = ["u0"]
    assert len(mfa(gh).payload["accounts"]) == 230


def test_the_github_transport_refuses_every_path_it_does_not_read():
    http = GH.default_http("t")
    for bad in ("/user", "/orgs/x/hooks", "/repos/a/b/git/refs", "/repos/a/b/pulls/1/merge", "/orgs/x/members/alice"):
        with pytest.raises(PermissionError):
            http(bad, {})


# ───────────────────────── end to end: real-shaped collection, then the same assessor ─────────────────────────
def collected(tmp_path, name, collector):
    d = tmp_path / name
    d.mkdir()
    v, p, _ = pipe(d)
    rep = p.run([job(collector, window_s=DAY)], APERIOD[0], APERIOD[1])
    return v, rep


def test_collected_changes_assess_as_a_pass_a_self_approved_one_as_a_fail_and_a_refused_read_as_not_tested(tmp_path):
    good = FakeGitHub()
    good.prs = [pr(1), pr(2, author="dev-3")]
    good.reviews = {1: [{"user": {"login": "dev-2"}, "state": "APPROVED"}], 2: [{"user": {"login": "dev-1"}, "state": "APPROVED"}]}
    good.checks = {f"{n:040x}": runs(("ci", "success"), ("scan", "success")) for n in (1, 2)}
    bad = FakeGitHub()
    bad.prs, bad.checks = [pr(1)], good.checks
    bad.reviews = {1: [{"user": {"login": "dev-1"}, "state": "APPROVED"}]}                  # approved by its own author
    refused = FakeGitHub()
    refused.protection = (403, {})
    res = {}
    for name, gh in (("good", good), ("bad", bad), ("refused", refused)):
        v, rep = collected(tmp_path, name, GH.GithubChanges(["acme/app"], gh))
        res[name] = (rep, A.Assessor(v, clock=kit.Clock()).assess("SEC-CHG-01", "acme", "prod", *APERIOD))
    assert res["good"][1].result == A.PASS and res["good"][1].population == 2 and res["good"][1].coverage >= 0.99
    assert res["bad"][1].result == A.FAIL and "independent" in res["bad"][1].failures[0]
    assert res["refused"][0].gaps() and res["refused"][1].result == A.NOT_TESTED


def test_collected_mfa_status_feeds_only_the_mfa_control(tmp_path):
    gh = FakeGitHub()
    gh.members, gh.admins, gh.no2fa = ["alice", "bob"], ["alice", "bob"], []
    v, rep = collected(tmp_path, "ok", GH.GithubMfa("acme-org", gh))
    asr = A.Assessor(v, clock=kit.Clock())
    assert not rep.gaps() and asr.assess("SEC-ACC-01", "acme", "prod", *APERIOD).result == A.PASS
    assert asr.assess("SEC-ACC-02", "acme", "prod", *APERIOD).result == A.NOT_TESTED        # an account list is not an access review
    assert asr.assess("SEC-ACC-03", "acme", "prod", *APERIOD).result == A.NOT_TESTED        # and not evidence that nobody left
    gh.no2fa = ["bob"]
    v2, _ = collected(tmp_path, "bad", GH.GithubMfa("acme-org", gh))
    assert A.Assessor(v2, clock=kit.Clock()).assess("SEC-ACC-01", "acme", "prod", *APERIOD).result == A.FAIL

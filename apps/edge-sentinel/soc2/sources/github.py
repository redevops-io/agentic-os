"""GitHub collectors: change records (SEC-CHG-01) and organisation MFA status (SEC-ACC-01).

What the GitHub REST API documents (docs.github.com/en/rest, checked 2026-10-24) and what follows from it:

  * `GET /orgs/{org}/members` takes `role` (all, admin, member) and `filter` (all, 2fa_disabled, 2fa_insecure); the filters are "only available for organization owners", so a token that
    is not an owner's gets an error and the window is a GAP. It is never read as "nobody has MFA disabled". Pages are at most 100; a list is complete only when the last page was read.
  * `GET /repos/{o}/{r}/pulls/{n}/reviews` lists reviews chronologically; `state` is not enumerated on the page, so only the exact value `APPROVED` counts, and a user's LATEST review decides
    (an approval followed by changes-requested is not an approval).
  * `GET /repos/{o}/{r}/commits/{ref}/check-runs` lists a commit's check runs; only `status: completed` with `conclusion: success` counts as a pass (neutral, skipped and cancelled do not).
  * `GET /repos/{o}/{r}/branches/{b}/protection/required_status_checks` gives the required check names; a 404 may mean no protection OR no access, so it is a GAP, never "no required checks"
    (an assessor would read that as an unchecked release and report a FAIL the data does not show). Repository rulesets are not read here.
  * Direct commits to the branch (no pull request) are changes with no approval: every commit in the window that is not part of a merged pull request becomes a release with no approvers.

What is NOT collected: deployment events (a merge to the default branch is the change event used), approvals dismissed after the fact other than via the review's own latest state, outside
collaborators, pending invitations, and any system other than GitHub. A token's scope decides what it can see; a repository it cannot see does not appear, and the payload says which repositories
were read. Completeness is the collector's statement that it read every page it needed within its caps."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional

from ..pipeline import Collected, CollectionFailed

Http = Callable[[str, dict], "tuple[int, Any, dict]"]       # (path, params) -> (status, parsed JSON, headers); GET only
PER_PAGE = 100


def _epoch(ts: Any) -> Optional[float]:
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return float(ts)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
        except ValueError:
            return None
    return None


def default_http(token: str, base_url: str = "https://api.github.com", timeout: float = 15.0) -> Http:
    """A GET-only transport over httpx (imported lazily) for the read paths these collectors use."""
    import re

    allowed = [re.compile(p) for p in (r"^/orgs/[\w.-]+/members$", r"^/repos/[\w.-]+/[\w.-]+/(pulls|commits)$", r"^/repos/[\w.-]+/[\w.-]+/pulls/\d+/reviews$",
                                       r"^/repos/[\w.-]+/[\w.-]+/commits/[0-9a-f]{7,40}/(check-runs|pulls)$",
                                       r"^/repos/[\w.-]+/[\w.-]+/branches/[\w./-]+/protection/required_status_checks$")]

    def http(path: str, params: dict) -> tuple:
        if not any(a.match(path) for a in allowed):
            raise PermissionError(f"the GitHub transport refuses {path}: not a read path")
        import httpx   # noqa: PLC0415
        r = httpx.get(base_url.rstrip("/") + path, params=params, timeout=timeout,
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        try:
            body = r.json()
        except ValueError:
            body = None
        return r.status_code, body, dict(r.headers)
    return http


class _Reader:
    def __init__(self, http: Http, max_pages: int) -> None:
        self.http, self.max_pages = http, max_pages
        self.complete = True                                  # false once a cap cut a list short

    def get(self, path: str, params: Optional[dict] = None):
        try:
            status, body, _ = self.http(path, dict(params or {}))
        except Exception as e:  # noqa: BLE001
            raise CollectionFailed(f"{path}: the request failed ({type(e).__name__})") from None
        if status != 200:
            raise CollectionFailed(f"{path}: HTTP {status} (a refused or missing read is not evidence of anything)")
        return body

    def pages(self, path: str, params: dict, key: Optional[str] = None, stop: Optional[Callable[[list], bool]] = None) -> list:
        """Every item of a paginated list. Hitting `max_pages` with a full page still pending sets `complete` false. `stop(rows)` ends the walk early when a page proves that nothing
        older can matter (a list sorted newest-first that has reached the start of the window)."""
        out: list = []
        for page in range(1, self.max_pages + 1):
            body = self.get(path, {**params, "per_page": PER_PAGE, "page": page})
            rows = body.get(key) if (key and isinstance(body, dict)) else body
            if not isinstance(rows, list):
                raise CollectionFailed(f"{path}: an unexpected response shape")
            out += rows
            if len(rows) < PER_PAGE or (stop is not None and stop(rows)):
                return out
        self.complete = False
        return out


class GithubMfa:
    """SEC-ACC-01 evidence from an organisation's member list. Emitted as `mfa_status`, NOT `access_review`: it carries no review or termination data, and the access-review and
    termination controls must not read it as if it did."""
    kind = "mfa_status"
    source = "github organisation members"

    def __init__(self, org: str, http: Http, *, name: Optional[str] = None, max_pages: int = 50) -> None:
        self.org, self.http, self.max_pages = org, http, max_pages
        self.name = name or f"github-mfa:{org}"

    def collect(self, window_from: float, window_to: float) -> Collected:
        r = _Reader(self.http, self.max_pages)
        path = f"/orgs/{self.org}/members"
        everyone = r.pages(path, {})
        admins = {m["login"] for m in r.pages(path, {"role": "admin"}) if isinstance(m, dict) and m.get("login")}
        no_2fa = {m["login"] for m in r.pages(path, {"filter": "2fa_disabled"}) if isinstance(m, dict) and m.get("login")}
        insecure = {m["login"] for m in r.pages(path, {"filter": "2fa_insecure"}) if isinstance(m, dict) and m.get("login")}
        accounts = [{"id": m["login"], "privileged": m["login"] in admins, "mfa": m["login"] not in no_2fa, "status": "active"} for m in everyone if isinstance(m, dict) and m.get("login")]
        if not accounts:
            raise CollectionFailed(f"{path}: no members returned (an empty list is what a collector without access also sees)")
        return Collected({"accounts": accounts, "population_complete": r.complete, "mfa_insecure": sorted(insecure & {a['id'] for a in accounts}),
                          "org": self.org, "note": "members only: outside collaborators and pending invitations are not included"})


class GithubChanges:
    """SEC-CHG-01 evidence: the merges (and direct pushes) to a branch in the window, with their independent approvals and required checks."""
    kind = "change_records"
    source = "github pull requests and commits"

    def __init__(self, repos: list, http: Http, *, branch: str = "main", name: Optional[str] = None, max_pages: int = 30, max_commits: int = 500) -> None:
        self.repos, self.http, self.branch, self.max_pages, self.max_commits = list(repos), http, branch, max_pages, max_commits
        self.name = name or f"github-changes:{','.join(self.repos)}"

    def collect(self, window_from: float, window_to: float) -> Collected:
        releases: list = []
        complete = True
        for repo in self.repos:
            r = _Reader(self.http, self.max_pages)
            rel = self._repo(r, repo, window_from, window_to)
            releases += rel
            complete = complete and r.complete
        return Collected({"releases": releases, "population_complete": complete, "repos": self.repos, "branch": self.branch,
                          "note": "a merge to the branch is the change event; deployments are not collected"})

    def _repo(self, r: _Reader, repo: str, lo: float, hi: float) -> list:
        prot = r.get(f"/repos/{repo}/branches/{self.branch}/protection/required_status_checks")
        required = sorted({c.get("context") for c in (prot.get("checks") or []) if isinstance(c, dict) and c.get("context")} | {c for c in (prot.get("contexts") or []) if isinstance(c, str)}) \
            if isinstance(prot, dict) else None
        if required is None:
            raise CollectionFailed(f"{repo}: the required checks came back in an unexpected shape")
        def older_than_window(rows: list) -> bool:               # newest update first: a page whose last pull request was last updated before the window ends the walk
            last = _epoch((rows[-1] or {}).get("updated_at")) if rows else None
            return last is not None and last < lo
        prs = r.pages(f"/repos/{repo}/pulls", {"state": "closed", "base": self.branch, "sort": "updated", "direction": "desc"}, stop=older_than_window)
        merged_in_window, merge_shas = [], set()
        for pr in prs:
            m = _epoch(pr.get("merged_at")) if isinstance(pr, dict) else None
            if m is not None and lo <= m < hi:
                merged_in_window.append((pr, m))
                if pr.get("merge_commit_sha"):
                    merge_shas.add(pr["merge_commit_sha"])
        out = []
        for pr, merged in merged_in_window:
            n = pr.get("number")
            reviews = r.pages(f"/repos/{repo}/pulls/{n}/reviews", {})
            latest: dict = {}
            for rv in reviews:                                  # chronological: the last review by each user decides
                u = ((rv or {}).get("user") or {}).get("login")
                if u:
                    latest[u] = rv.get("state")
            approvers = sorted(u for u, st in latest.items() if st == "APPROVED")
            passed = self._passed(r, repo, pr.get("merge_commit_sha"))
            out.append({"id": f"{repo}#{n}", "author": ((pr.get("user") or {}).get("login") or ""), "approvers": approvers, "required_checks": required,
                        "passed_checks": passed, "deployed_at": merged})
        commits = r.pages(f"/repos/{repo}/commits", {"sha": self.branch, "since": _iso(lo), "until": _iso(hi)})
        if len(commits) > self.max_commits:
            r.complete = False
            commits = commits[: self.max_commits]
        for c in commits:
            sha = (c or {}).get("sha", "")
            if not sha or sha in merge_shas:
                continue
            assoc = r.get(f"/repos/{repo}/commits/{sha}/pulls", {})
            if isinstance(assoc, list) and any(isinstance(p, dict) and p.get("merged_at") for p in assoc):
                continue
            when = _epoch(((c.get("commit") or {}).get("committer") or {}).get("date"))
            who = ((c.get("author") or {}).get("login")) or ((c.get("commit") or {}).get("author") or {}).get("name") or "unknown"
            out.append({"id": f"{repo}@{sha[:10]}", "author": who, "approvers": [], "required_checks": required, "passed_checks": self._passed(r, repo, sha),
                        "deployed_at": when if when is not None else lo})
        return out

    @staticmethod
    def _passed(r: _Reader, repo: str, sha: Optional[str]) -> list:
        if not sha:
            return []
        runs = r.pages(f"/repos/{repo}/commits/{sha}/check-runs", {"filter": "latest"}, key="check_runs")
        return sorted({x["name"] for x in runs if isinstance(x, dict) and x.get("status") == "completed" and x.get("conclusion") == "success" and x.get("name")})


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

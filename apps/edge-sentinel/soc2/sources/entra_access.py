"""SEC-ACC-02 and SEC-ACC-03 evidence from Microsoft Entra ID and Azure RBAC: who holds privileged access, who reviewed it, and whether a leaver's access was removed within a day.

What the services show (read from a real tenant, 2026-10-24): `GET /users` (id, userPrincipalName, accountEnabled, userType), `GET /directoryRoles` and `/directoryRoles/{id}/members` (the holders of the activated directory roles), `az role assignment list --all`
(Azure RBAC: principal id and type, role name, scope; service principals such as managed identities hold Owner and Contributor too), and `GET /auditLogs/directoryAudits` (who changed what: `Delete user`, an `Update user` that sets AccountEnabled to false).
This tenant has no Entra premium licence, so `signInActivity` is refused (HTTP 403) and is not used: nothing here says when an account last signed in, and nothing here says anything about MFA (SEC-ACC-01 stays with the GitHub member list).

Two things the identity provider cannot know are kept in append-only logs written by people, never by this module:

  * `ReviewLog`: that a named reviewer looked at an account's access and decided to retain or remove it. A reviewer must be someone other than the account. The collector reports the latest review made by the time it collects (the review control is once per period, so a snapshot of an early day of the period must not look unreviewed because the review came later in the period; the assessor then checks that review's time against the period).
  * `LeaverLog`: that a person left, when (the HR fact), who declared it and the ticket. Entra cannot tell a leaver from a disabled account.

The collector joins them with what the services show. A leaver whose account is still enabled has no revocation time, and the assessor reports it; a leaver whose deletion or disabling is not in the audit log (retention is short on this licence) also has none, which the
assessor reports as a failure to show removal, not as a finding of retained access: the payload says which. A population that could not be read completely (any page, role or the RBAC list failing) is a GAP: the collection fails, it never reports a partial population as complete.

The transport is the `az` CLI limited to read-only Graph paths and the RBAC listing; credentials are the operator's own `az login`, never read or stored here."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from ..pipeline import Collected, CollectionFailed

Runner = Callable[[list], "tuple[int, str, str]"]
GRAPH = "https://graph.microsoft.com/v1.0/"
READ_PREFIXES = ("users", "directoryRoles", "auditLogs/directoryAudits")
PRIVILEGED_DIRECTORY_ROLES = ("Global Administrator", "Privileged Role Administrator", "Privileged Authentication Administrator", "User Administrator", "Application Administrator", "Cloud Application Administrator",
                              "Security Administrator", "Exchange Administrator", "Conditional Access Administrator", "Authentication Administrator", "Helpdesk Administrator")
PRIVILEGED_AZURE_ROLES = ("Owner", "Contributor", "User Access Administrator", "Role Based Access Control Administrator")
MAX_PAGES = 200


def default_runner(timeout: float = 90.0) -> Runner:
    """Runs `az` for the read paths this module needs and refuses everything else."""
    def run(args: list) -> tuple:
        a = list(args)
        ok = False
        if a[:4] == ["rest", "--method", "get", "--url"] and len(a) == 5 and a[4].startswith(GRAPH):
            ok = any(a[4][len(GRAPH):].startswith(p) for p in READ_PREFIXES)
        elif a[:3] == ["role", "assignment", "list"] and "--all" in a:
            ok = not any(x in a for x in ("create", "delete"))
        if not ok:
            raise PermissionError(f"the Entra runner refuses {a[:5]}: only read-only Graph paths and `role assignment list` are read paths")
        r = subprocess.run(["az", *a, "-o", "json"], capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    return run


def _epoch(ts: Any) -> Optional[float]:
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
        except ValueError:
            return None
    return None


class _AppendOnly:
    table = ""
    ddl = ""

    def __init__(self, path: str, *, clock: Callable[[], float] = time.time) -> None:
        self.path, self.clock = path, clock
        with self._conn() as c:
            c.execute(self.ddl)
            for op in ("UPDATE", "DELETE"):
                c.execute(f"CREATE TRIGGER IF NOT EXISTS {self.table}_no_{op.lower()} BEFORE {op} ON {self.table} BEGIN SELECT RAISE(ABORT, 'this log is append-only'); END")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30, isolation_level=None)


class ReviewLog(_AppendOnly):
    """A reviewer's decision about one account's access. Written by the reviewer's tooling, not by the collector."""
    table = "reviews"
    ddl = "CREATE TABLE IF NOT EXISTS reviews (at REAL NOT NULL, account TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL, note TEXT NOT NULL)"

    def record(self, *, account: str, reviewer: str, decision: str, note: str, at: Optional[float] = None) -> None:
        if not account.strip() or not reviewer.strip():
            raise ValueError("a review names the account and the reviewer")
        if reviewer.strip().lower() == account.strip().lower():
            raise ValueError("an account cannot be reviewed by itself")
        if decision not in ("retain", "remove"):
            raise ValueError("a review decides `retain` or `remove`")
        if len(note.strip()) < 10:
            raise ValueError("a review needs a note of what was checked")
        with self._conn() as c:
            c.execute("INSERT INTO reviews VALUES (?,?,?,?,?)", (self.clock() if at is None else float(at), account.strip(), reviewer.strip(), decision, note.strip()))

    def latest(self, account: str, until: float) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute("SELECT at, reviewer, decision FROM reviews WHERE lower(account)=lower(?) AND at<=? ORDER BY at DESC LIMIT 1", (account, until)).fetchone()
        return {"at": r[0], "reviewer": r[1], "decision": r[2]} if r else None


class LeaverLog(_AppendOnly):
    """That a person left, and when. The HR fact the identity provider does not hold."""
    table = "leavers"
    ddl = "CREATE TABLE IF NOT EXISTS leavers (account TEXT NOT NULL, terminated_at REAL NOT NULL, declared_by TEXT NOT NULL, ticket TEXT NOT NULL, recorded_at REAL NOT NULL)"

    def record(self, *, account: str, terminated_at: float, declared_by: str, ticket: str) -> None:
        if not account.strip() or not declared_by.strip() or not ticket.strip():
            raise ValueError("a leaver record names the account, who declared it and the ticket")
        with self._conn() as c:
            c.execute("INSERT INTO leavers VALUES (?,?,?,?,?)", (account.strip(), float(terminated_at), declared_by.strip(), ticket.strip(), self.clock()))

    def all(self) -> list:
        with self._conn() as c:
            return [{"account": r[0], "terminated_at": r[1], "declared_by": r[2], "ticket": r[3]} for r in c.execute("SELECT account, terminated_at, declared_by, ticket FROM leavers ORDER BY terminated_at")]


class EntraAccess:
    kind = "access_review"
    source = "microsoft entra id (users, directory roles, directory audit) and azure rbac, with the review and leaver logs people write"

    def __init__(self, runner: Runner, reviews: ReviewLog, leavers: LeaverLog, *, directory_roles: tuple = PRIVILEGED_DIRECTORY_ROLES, azure_roles: tuple = PRIVILEGED_AZURE_ROLES, name: str = "entra-access", clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.runner, self.reviews, self.leavers, self.directory_roles, self.azure_roles, self.name = runner, reviews, leavers, directory_roles, azure_roles, name

    # ── reads: any failure is a gap, never a partial population ──
    def _get(self, url: str) -> Any:
        try:
            rc, out, err = self.runner(["rest", "--method", "get", "--url", url])
        except Exception as e:  # noqa: BLE001
            raise CollectionFailed(f"az could not be run ({type(e).__name__})") from None
        if rc != 0:
            raise CollectionFailed("graph read failed: " + " ".join(err.split())[:160])
        try:
            return json.loads(out)
        except ValueError:
            raise CollectionFailed("graph returned something that is not JSON") from None

    def _paged(self, url: str) -> list:
        items, pages = [], 0
        while url:
            pages += 1
            if pages > MAX_PAGES:
                raise CollectionFailed("the listing did not end within the page limit")
            d = self._get(url)
            if not isinstance(d, dict) or not isinstance(d.get("value"), list):
                raise CollectionFailed("graph returned an unexpected shape")
            items += d["value"]
            url = d.get("@odata.nextLink") or ""
            if url and not url.startswith(GRAPH):
                raise CollectionFailed("graph pointed at a page outside graph.microsoft.com")
        return items

    def _rbac(self, subscription: str = "") -> list:
        try:
            rc, out, err = self.runner(["role", "assignment", "list", "--all"] + (["--subscription", subscription] if subscription else []))
        except Exception as e:  # noqa: BLE001
            raise CollectionFailed(f"az could not be run ({type(e).__name__})") from None
        if rc != 0:
            raise CollectionFailed("role assignment list failed: " + " ".join(err.split())[:160])
        try:
            rows = json.loads(out)
        except ValueError:
            raise CollectionFailed("az returned something that is not JSON") from None
        if not isinstance(rows, list):
            raise CollectionFailed("az returned an unexpected shape for the role assignments")
        return rows

    def collect(self, window_from: float, window_to: float) -> Collected:
        users = self._paged(GRAPH + "users?$select=id,userPrincipalName,accountEnabled,userType,createdDateTime&$top=999")
        roles = self._get(GRAPH + "directoryRoles")
        if not isinstance(roles, dict) or not isinstance(roles.get("value"), list):
            raise CollectionFailed("graph returned an unexpected shape for the directory roles")
        privileged: dict = {}                                                 # object id -> sorted roles
        for r in roles["value"]:
            if r.get("displayName") in self.directory_roles:
                for m in self._paged(GRAPH + f"directoryRoles/{r['id']}/members"):
                    privileged.setdefault(m["id"], set()).add("directory:" + r["displayName"])
        sp_names: dict = {}
        for a in self._rbac():
            if a.get("roleDefinitionName") in self.azure_roles and a.get("principalId"):
                privileged.setdefault(a["principalId"], set()).add("azure:" + a["roleDefinitionName"])
                if a.get("principalType") and a["principalType"] != "User":
                    sp_names[a["principalId"]] = a["principalType"]
        since = datetime.fromtimestamp(max(0.0, min(window_from, min((x["terminated_at"] for x in self.leavers.all()), default=window_from)) - 86400), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        audits = self._paged(GRAPH + f"auditLogs/directoryAudits?$filter=category eq 'UserManagement' and activityDateTime ge {since}&$top=200")
        removed_at: dict = {}                                                 # object id or upn (lower) -> earliest time access was removed
        for ev in audits:
            if ev.get("result") != "success":
                continue
            when = _epoch(ev.get("activityDateTime"))
            disabled = ev.get("activityDisplayName") == "Delete user" or any(
                p.get("displayName") == "AccountEnabled" and "false" in str(p.get("newValue")).lower()
                for t in ev.get("targetResources") or [] for p in t.get("modifiedProperties") or [])
            if when is None or not disabled:
                continue
            for t in ev.get("targetResources") or []:
                for key in (t.get("id"), t.get("userPrincipalName")):
                    if key:
                        k = str(key).lower()
                        removed_at[k] = min(removed_at.get(k, when), when)
        leavers = {x["account"].lower(): x for x in self.leavers.all()}
        accounts, seen = [], set()
        for u in users:
            upn, oid = u.get("userPrincipalName") or "", u.get("id") or ""
            seen.add(upn.lower())
            lv = leavers.get(upn.lower())
            rv = self.reviews.latest(upn, self.clock())
            acct = {"id": upn, "object_id": oid, "kind": "user", "privileged": oid in privileged, "roles": sorted(privileged.get(oid, ())), "enabled": bool(u.get("accountEnabled")),
                    "status": "terminated" if lv else ("active" if u.get("accountEnabled") else "disabled"), "owner": upn,
                    "reviewed_by": rv["reviewer"] if rv else None, "reviewed_at": rv["at"] if rv else None, "review_decision": rv["decision"] if rv else None}
            if lv:
                acct.update(terminated_at=lv["terminated_at"], terminated_ticket=lv["ticket"], access_revoked_at=removed_at.get(oid.lower()) or removed_at.get(upn.lower()),
                            access_removal_basis="entra audit log" if (removed_at.get(oid.lower()) or removed_at.get(upn.lower())) else ("account still enabled" if u.get("accountEnabled") else "no audit event found: disabled, but when is not shown"))
            accounts.append(acct)
        for oid, rs in privileged.items():                                    # privileged principals that are not users (service principals, managed identities)
            if oid in {a["object_id"] for a in accounts}:
                continue
            rv = self.reviews.latest(oid, self.clock())
            accounts.append({"id": oid, "object_id": oid, "kind": sp_names.get(oid, "non-user"), "privileged": True, "roles": sorted(rs), "enabled": True, "status": "active", "owner": "",
                             "reviewed_by": rv["reviewer"] if rv else None, "reviewed_at": rv["at"] if rv else None, "review_decision": rv["decision"] if rv else None})
        for acct_id, lv in leavers.items():                                   # a leaver whose account no longer exists (deleted)
            if acct_id not in seen:
                t = removed_at.get(acct_id)
                accounts.append({"id": lv["account"], "object_id": "", "kind": "user", "privileged": False, "roles": [], "enabled": False, "status": "terminated", "owner": lv["account"], "terminated_at": lv["terminated_at"],
                                 "terminated_ticket": lv["ticket"], "access_revoked_at": t, "access_removal_basis": "entra audit log" if t else "account is gone but no deletion event was found in the audit log"})
        return Collected({"accounts": accounts, "population_complete": True, "tenant_note": "no Entra premium licence: sign-in activity and MFA registration are not available and are not claimed",
                          "audit_events_read": len(audits), "from": window_from, "to": window_to})

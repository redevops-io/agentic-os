"""Proposals, the human decision, and what happened: the Mission Runtime's gate, with the facts a client needs.

The approval is the runtime's: each proposal is a one-node mission bound to its capability and inputs, parked by the
mandatory gate, approved with ``MissionRuntime.approve`` (which records who, the capability and a digest of the inputs,
and expires old requests), and performed only when the node carries the runtime's ``_approval`` marker.

Beside the ledger sits one small private database: the inputs (the ledger records only their digest, so customer text can
be erased), the idempotency keys, and the outcome. The inputs are kept for ``retention_s`` after a request finishes, so a
person can still see exactly what was approved, then erased (0 erases them at once). A request is never
performed twice: the move from ``pending`` is atomic, and a write whose outcome is unknown is not sent again.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from agentic_os import app_gate
from agentic_os.agent_gateway.agent_tokens import envelope_key_for_requester
from agentic_os.governed_writes import authorization
from agentic_os.governed_writes.actions import ACTIONS, APP_GRANTS, Outcome, reserved_marker
from agentic_os.governed_writes.apps import NotApplied
from agentic_os.governed_writes.preauthorization import PreauthorizationError, Preauthorizations
from agentic_os.mission.approval import canonical
from agentic_os.mission.authority import Action, Verdict
from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import LocalOperatorClient, Operator, capability, pending_approval
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import ApprovalError, MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import MissionState
from agentic_os.schema_check import SchemaError, validate

OPERATOR = authorization.APP
PENDING, DECIDING = "pending", "deciding"
#: where a request ends. ``no_change``: what was asked for already holds. ``previewed``: shadow mode wrote nothing.
FINAL = ("executed_verified", "executed_unverified", "no_change", "previewed", "failed", "outcome_unknown", "rejected", "expired")
MAX_PENDING_PER_REQUESTER = 50
_STALE_DECIDING_S = 600.0


class WriteError(Exception):
    """A refusal with a stable ``code``: not_found | invalid | conflict | unavailable | refused | too_many | denied."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code, self.detail = code, detail


class GovernedWrites:
    def __init__(self, state_dir: "str | Path", writers: Mapping[str, Any], *, ttl_s: float = 86400.0,
                 clock: Callable[[], float] = time.time, authority: Any = None, retention_s: float = 7 * 86400.0,
                 single_owner: Optional[Callable[[], bool]] = None) -> None:
        self.mentions: Optional[Callable[[str, str], List[str]]] = None   # set by whoever can read the tenant's messages
        root = Path(state_dir)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.writers, self.ttl_s, self.clock = dict(writers), float(ttl_s), clock
        self._single_owner = single_owner                        # a declared single owner can also approve
        self.retention_s = max(0.0, float(retention_s))
        self._lock = threading.RLock()
        self._ran: Dict[str, "tuple[str, Any]"] = {}              # mission id -> what the handler really did
        db = root / "requests.sqlite"
        if not db.exists():
            os.close(os.open(db, os.O_CREAT | os.O_WRONLY, 0o600))
        self._db = sqlite3.connect(str(db), check_same_thread=False, isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA secure_delete=ON")              # erased inputs do not linger in free pages
        self._db.execute("""CREATE TABLE IF NOT EXISTS requests(
            id TEXT PRIMARY KEY, tenant TEXT NOT NULL, requester TEXT NOT NULL, action TEXT NOT NULL, app TEXT NOT NULL,
            idem TEXT NOT NULL, digest TEXT NOT NULL, intent TEXT NOT NULL, inputs TEXT, preview TEXT NOT NULL,
            created REAL NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL, approver TEXT, decided REAL,
            record TEXT, read_back TEXT, note TEXT, executed INTEGER, verified INTEGER, finished REAL,
            UNIQUE(tenant, requester, idem))""")
        columns = {r["name"] for r in self._db.execute("PRAGMA table_info(requests)").fetchall()}
        for column in ("evidence", "attested"):                  # added after the first release of this table
            if column not in columns:
                self._db.execute(f"ALTER TABLE requests ADD COLUMN {column} TEXT")
        self._journal: Any = None
        self._journal_dir = root
        self.preauthorizations = Preauthorizations(self._db, self.clock)
        if authority is None:
            from agentic_os.mission.authority import authority_from_env  # noqa: PLC0415
            authority = authority_from_env()
        operator = Operator(OPERATOR, [capability(
            a.name, self._handler(a.name), provides=[self._fact(a.name)], outputs={self._fact(a.name): "s"},
            side_effecting=True, approval_required=True, permissions=[self._permission(a.app)],
            irreversible="a person removes the record in the application itself") for a in ACTIONS.values()])
        # Gated like any app: its tools are classified in tool_actions["governed-writes"], and the Operator seam asks the
        # installed permissions gate before the handler runs (denied when permissions are requested and none is installed).
        # It is served on no HTTP route.
        self.operator = operator
        registry = CapabilityRegistry()
        registry.register(operator.manifest)
        self.runtime = MissionRuntime(registry, Executor(LocalOperatorClient({OPERATOR: operator})),
                                      store=EventStore(str(root / "events.jsonl")), authority=authority)
        # a write runs in the authority of the agent that asked for it: its stored envelope, never one made here
        self.runtime.authority_key = lambda mission: envelope_key_for_requester(str(getattr(mission, "principal", "") or ""))

    @staticmethod
    def _fact(action: str) -> str:
        return action.replace(".", "_") + "_done"

    @staticmethod
    def _permission(app: str) -> str:
        return f"writes:{app}"

    def configured(self, app: str) -> bool:
        return app in self.writers

    def authority_refusal(self, action: str, *, requester: str, tenant: str, inputs: Optional[Mapping[str, Any]] = None) -> str:
        """"" when the requesting agent's own stored authority (mission-authority/v1: given when a person connected it or
        issued its token; scoped to its grants; expiring; revocable) covers this change right now, else why not. A person's
        approval is a second, separate condition: it does not create this authority and cannot replace it."""
        guard = self.runtime.authority
        if guard is None:
            return ("this deployment keeps no authority records, so no agent can hold authority to change anything: set "
                    "AGENTIC_OS_AUTHORITY_DB, then connect the agent again (or issue its token with --authority-db)")
        try:
            found = guard.check(envelope_key_for_requester(requester), tenant, Action(
                capability=action, arguments=dict(inputs or {}), risk_tier=1, side_effecting=True))
        except Exception as e:  # noqa: BLE001 - an authority store that cannot be read refuses
            return f"the authority records cannot be read ({type(e).__name__})"
        if found.verdict is Verdict.DENY:
            return {"no_authority": "this agent holds no authority here (it was never given any, or its token was issued without "
                                    "--authority-db)",
                    "revoked": "this agent's authority was revoked",
                    "expired": "this agent's authority has expired"}.get(found.rule, f"this agent's authority does not cover it: {found.reason}")
        return ""

    def authorization(self, action: str, *, requester: str, tenant: str,
                      inputs: Optional[Mapping[str, Any]] = None) -> authorization.Authorization:
        """May this requester of this tenant have this action done? Never changed by an approval."""
        spec = ACTIONS[action]
        if spec.financial and not authorization.tenant_holds_financial(tenant, spec.app):
            return authorization.Authorization(authorization.Verdict.DENIED, "missing_grant",
                                               f"{action} concerns financial data: the tenant {tenant or '(none)'} needs egress:financial "
                                               "(policy_refs in AGENTIC_OS_TENANT_LIMITS)")
        verdict = authorization.authorize(action, app=spec.app, requester=requester, tenant=tenant,
                                          someone_else_can_approve=self._single_owner)
        if verdict.denied:
            return verdict
        why = self.authority_refusal(action, requester=requester, tenant=tenant, inputs=inputs)
        return authorization.Authorization(authorization.Verdict.DENIED, "no_authority", why[:400]) if why else verdict

    @property
    def state_dir(self) -> Path:
        return self._journal_dir

    @property
    def single_owner(self) -> Optional[Callable[[], bool]]:
        return self._single_owner

    def context(self, tenant: str, *, approver: str = "", evidence: Optional[Mapping[str, Any]] = None, requester: str = "") -> Any:
        """What an action with a ``prepare`` step needs besides the application: its journal and what was approved."""
        from agentic_os.governed_writes import payment_correction  # noqa: PLC0415
        with self._lock:
            if self._journal is None:
                self._journal = payment_correction.Journal(self._journal_dir, self.clock)
        return payment_correction.Context(tenant=tenant, journal=self._journal, approver=approver, evidence=evidence, mentions=self.mentions,
                                          requester=requester, state_dir=self._journal_dir, operators=self.writers)

    # ── the operator: runs only with the runtime's approval marker ───────────────
    def _handler(self, name: str) -> Callable[[dict], dict]:
        action = ACTIONS[name]

        def run(inputs: dict) -> dict:
            marker = inputs.get("_approval")
            if not isinstance(marker, dict) or marker.get("approved") is not True or not marker.get("by"):
                return pending_approval(name)
            mid = str(inputs.get("_mission") or "")
            args = {k: v for k, v in inputs.items() if k in action.input_schema["properties"]}
            footer = f"Proposed by an external agent and approved by {marker['by']} (request {mid})"
            try:
                validate(args, action.input_schema)
                row = self._row(mid)
                if row is None:
                    raise NotApplied("this execution belongs to no request")
                allowed = self.authorization(name, requester=row["requester"], tenant=row["tenant"], inputs=args)
                if allowed.denied:                               # checked again at the last moment, after the approval
                    raise NotApplied(allowed.error)
                writer = self.writers.get(action.app)
                if writer is None:
                    raise NotApplied(f"{action.app} is not connected")
                if action.prepare is None:
                    outcome: Outcome = action.perform(writer, args, footer, mid)
                else:
                    outcome = action.perform(writer, args, footer, mid, self.context(
                        row["tenant"], approver=str(marker["by"]), evidence=json.loads(row["evidence"] or "null"), requester=row["requester"]))
            except SchemaError:
                self._ran[mid] = ("failed", "the inputs are not valid; nothing was changed")
                raise
            except NotApplied as e:
                self._ran[mid] = ("failed", f"{e}; nothing was changed")
                raise
            except Exception as e:                           # OutcomeUnknown, and anything we did not foresee
                why = str(e) if type(e).__name__ == "OutcomeUnknown" else type(e).__name__
                self._ran[mid] = ("outcome_unknown", f"{why}; check the application before asking again")
                raise RuntimeError(why) from None
            self._ran[mid] = ("done", outcome)
            fact = {self._fact(name): outcome.record, "verified": outcome.verified}
            if outcome.executed:
                fact["executed"] = True
            elif not outcome.verified:
                fact["executed"] = False
            return fact
        return run

    # ── proposing ────────────────────────────────────────────────────────────────
    def request(self, *, tenant: str, requester: str, action: str, inputs: Mapping[str, Any], idempotency_key: str) -> Dict[str, Any]:
        spec = ACTIONS.get(action)
        if spec is None:
            raise WriteError("not_found", "unknown action")
        if not requester or not idempotency_key:
            raise WriteError("invalid", "a requester and an idempotency key are required")
        args = json.loads(json.dumps(dict(inputs)))
        try:
            validate(args, spec.input_schema)
        except SchemaError as e:
            raise WriteError("invalid", str(e)) from None
        marker = reserved_marker(args)
        if marker:
            raise WriteError("invalid", f"the text {marker!r} is reserved for approval records and cannot be written through a proposal")
        if not self.configured(spec.app):
            raise WriteError("unavailable", f"{spec.app} is not connected on this deployment")
        allowed = self.authorization(action, requester=requester, tenant=tenant, inputs=args)
        if allowed.denied:                                       # nothing is created for something that cannot be done
            raise WriteError("denied", allowed.error)
        intent = hashlib.sha256(canonical({"action": action, "inputs": args})).hexdigest()
        with self._lock:
            old = self._db.execute("SELECT * FROM requests WHERE tenant=? AND requester=? AND idem=?",
                                   (tenant, requester, idempotency_key)).fetchone()
            if old is not None:
                if old["intent"] != intent:
                    raise WriteError("conflict", "this idempotency key was used for a different request")
                return {**self._view(self._fresh(old)), "replayed": True}
            waiting = self._db.execute("SELECT count(*) FROM requests WHERE tenant=? AND requester=? AND state=? AND expires>?",
                                       (tenant, requester, PENDING, self.clock())).fetchone()[0]
            if waiting >= MAX_PENDING_PER_REQUESTER:
                raise WriteError("too_many", f"{waiting} requests are already waiting for a decision")
            evidence = None
            if spec.prepare is not None:                          # refused here: nothing is left for a person to approve
                try:
                    evidence = spec.prepare(self.context(tenant, requester=requester), self.writers[spec.app], args)
                except NotApplied as e:
                    raise WriteError("refused", f"{e}"[:900]) from None
                except Exception as e:  # noqa: BLE001
                    raise WriteError("unavailable", f"the evidence could not be gathered ({type(e).__name__})") from None
            mission = self.runtime.create_mission(f"governed write: {action}", policy_refs=[self._permission(spec.app)], inputs=dict(args),
                                                  principal=requester, tenant=tenant, selected_capability=action, bind_selected=True)
            if mission.state is not MissionState.FAILED:
                self.runtime.run(mission.id)
            parked = self.runtime.repo.pending_human(mission.id)
            if not parked or mission.id in self._ran:
                raise WriteError("unavailable", "the approval gate did not open, so nothing was requested")
            digest = hashlib.sha256(canonical({"request_id": mission.id, "tenant": tenant, "requester": requester,
                                               "action": action, "inputs": args,
                                               **({"evidence_sha256": hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()}
                                                  if evidence is not None else {})})).hexdigest()
            now = self.clock()
            self._db.execute(
                "INSERT INTO requests(id,tenant,requester,action,app,idem,digest,intent,inputs,preview,created,expires,state,evidence)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mission.id, tenant, requester, action, spec.app, idempotency_key, digest, intent, json.dumps(args),
                 ((evidence or {}).get("summary") or spec.preview(args))[:300], now, now + self.ttl_s, PENDING,
                 None if evidence is None else json.dumps(evidence)))
            self._try_preauthorized(mission.id, tenant=tenant, requester=requester, action=action, args=args, evidence=evidence,
                                    digest=digest)
            return {**self._view(self._row(mission.id)), "replayed": False}

    def _try_preauthorized(self, mid: str, *, tenant: str, requester: str, action: str, args: Mapping[str, Any],
                           evidence: Optional[Mapping[str, Any]], digest: str) -> None:
        """Developer mode (``preauthorization``): if a person preauthorized exactly this, decide it now, through the same
        ``decide`` a person's click uses. Anything that refuses leaves the request waiting for a person."""
        if evidence and evidence.get("attestation"):             # what a person must attest to, nothing stands in for
            return
        grant = self.preauthorizations.claim(tenant=tenant, requester=requester, action=action, inputs=args, evidence=evidence)
        if grant is None:
            return
        who = f"preauthorization:{grant['id']} (granted by {grant['granted_by']})"
        try:
            done = self.decide(mid, approve=True, digest=digest, approver=who, tenant=tenant)
        except WriteError as e:
            self.preauthorizations.record(grant["id"], mid, f"not decided: {e.code}: {e.detail}", give_back=grant["charged"])
            return
        self.preauthorizations.record(grant["id"], mid, done["state"])

    # ── developer mode: only a person's routes call these ─────────────────────────
    def preauthorize(self, **fields: Any) -> Dict[str, Any]:
        with self._lock:
            return self.preauthorizations.create(**fields)

    # ── reading ──────────────────────────────────────────────────────────────────
    def _row(self, request_id: str) -> Optional[sqlite3.Row]:
        return self._db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()

    def _fresh(self, row: sqlite3.Row) -> sqlite3.Row:
        """Apply what time has done: a pending request lapses; a decision nobody finished has an unknown outcome."""
        now = self.clock()
        if row["state"] == PENDING and now >= row["expires"]:
            self._finish(row["id"], "expired", from_state=PENDING, note="nobody decided in time; nothing was changed")
        elif row["state"] == DECIDING and now - (row["decided"] or now) > _STALE_DECIDING_S:
            self._finish(row["id"], "outcome_unknown", from_state=DECIDING,
                         note="the service stopped while performing it; check the application before asking again")
        else:
            return row
        return self._row(row["id"])

    def _finish(self, request_id: str, state: str, *, from_state: str = DECIDING, note: str = "", outcome: Optional[Outcome] = None) -> None:
        o = outcome or Outcome(False, False)
        self._db.execute("UPDATE requests SET state=?, finished=?, inputs=CASE WHEN ? THEN inputs END, note=?, record=?, read_back=?,"
                         " executed=?, verified=? WHERE id=? AND state=?",
                         (state, self.clock(), int(self.retention_s > 0), note or o.note, o.record, o.read_back,
                          None if state == "outcome_unknown" else int(o.executed), int(o.verified), request_id, from_state))

    def _erase_old(self) -> None:
        """Remove the inputs of requests that finished longer ago than the retention."""
        self._db.execute("UPDATE requests SET inputs=NULL WHERE inputs IS NOT NULL AND finished IS NOT NULL AND finished <= ?",
                         (self.clock() - self.retention_s,))

    def _view(self, row: sqlite3.Row, *, for_approver: bool = False) -> Dict[str, Any]:
        out = {"request_id": row["id"], "action": row["action"], "app": row["app"], "state": row["state"], "summary": row["preview"],
               "tenant": row["tenant"], "requested_by": row["requester"], "created_at": row["created"], "expires_at": row["expires"],
               "decided_by": row["approver"] or "",
               "executed": None if row["executed"] is None else bool(row["executed"]),
               "verified": bool(row["verified"]), "record": row["record"] or "", "read_back": row["read_back"] or "", "note": row["note"] or ""}
        if row["state"] == PENDING:
            out["executed"] = False
        if for_approver:                                       # for a person: exactly what would be, or was, written
            out["inputs"] = None if row["inputs"] is None else json.loads(row["inputs"])    # None: erased after the retention
            if row["evidence"]:                                 # kept with the decision: what the approver saw and attested to
                out["evidence"] = json.loads(row["evidence"])
                out["attested"] = row["attested"] or ""
            if row["state"] == PENDING:                        # and the digest a decision names
                allowed = self.authorization(row["action"], requester=row["requester"], tenant=row["tenant"])
                out["authorization"] = {"verdict": allowed.verdict.value, "reason": allowed.reason}
                out["digest"] = row["digest"]
        return out

    def get(self, request_id: str, *, tenant: Optional[str] = None, requester: Optional[str] = None,
            for_approver: bool = False) -> Dict[str, Any]:
        """``tenant`` None = a deployment operator (every tenant). A request outside the filters does not exist."""
        with self._lock:
            self._erase_old()
            row = self._row(request_id)
            if row is None or (tenant is not None and row["tenant"] != tenant) or (requester is not None and row["requester"] != requester):
                raise WriteError("not_found", "no such request")
            return self._view(self._fresh(row), for_approver=for_approver)

    def list(self, *, tenant: Optional[str] = None, requester: Optional[str] = None, app: str = "", state: str = "",
             limit: int = 50, for_approver: bool = False) -> List[Dict[str, Any]]:
        sql, args = "SELECT * FROM requests WHERE 1=1", []
        for column, value in (("tenant", tenant), ("requester", requester), ("app", app or None)):
            if value is not None:
                sql += f" AND {column}=?"
                args.append(value)
        with self._lock:
            self._erase_old()
            rows = [self._fresh(r) for r in self._db.execute(sql + " ORDER BY created DESC LIMIT 500", args).fetchall()]
            return [self._view(r, for_approver=for_approver) for r in rows if not state or r["state"] == state][: max(1, min(limit, 200))]

    # ── deciding: a person, never the requester ──────────────────────────────────
    def decide(self, request_id: str, *, approve: bool, digest: str, approver: str, tenant: Optional[str] = None,
               attested: bool = False) -> Dict[str, Any]:
        if not approver:
            raise WriteError("refused", "a decision needs an identified person")
        with self._lock:
            current = self.get(request_id, tenant=tenant)
            row = self._row(request_id)
            if approver == row["requester"] or approver.startswith("agent:"):
                raise WriteError("refused", "an agent or the requester cannot decide this request")
            if current["state"] != PENDING:
                raise WriteError("conflict", f"this request is already {current['state']}")
            if digest != row["digest"]:
                raise WriteError("conflict", "the digest is not this request's: read it again and decide on what is shown")
            statement = (json.loads(row["evidence"]) or {}).get("attestation", "") if row["evidence"] else ""
            if approve and statement and attested is not True:
                raise WriteError("refused", f"approving this needs your attestation: {statement}")
            if approve:
                if not self.configured(row["app"]):
                    raise WriteError("unavailable", f"{row['app']} is not connected now; the request is still waiting")
                # an approval never overrides a denial: not the requester's (checked again, as things stand now) and not
                # the deciding person's own, as the Operator seam would judge this execution. The request stays waiting.
                allowed = self.authorization(row["action"], requester=row["requester"], tenant=row["tenant"],
                                             inputs=json.loads(row["inputs"]))
                if allowed.denied:
                    raise WriteError("denied", allowed.error)
                own = app_gate.operator_denial(OPERATOR, row["action"], self.operator)
                if own:
                    raise WriteError("denied", f"not_authorized (permission_denied): you may not have this performed: {own}"[:400])
            taken = self._db.execute("UPDATE requests SET state=?, approver=?, decided=?, attested=? WHERE id=? AND state=?",
                                     (DECIDING, approver, self.clock(), statement if approve else None, request_id, PENDING)).rowcount
            if taken != 1:
                raise WriteError("conflict", "someone else is deciding this request")
            self._perform(request_id, json.loads(row["inputs"]), approve=approve, approver=approver)
            return self._view(self._row(request_id))

    def _perform(self, mid: str, args: dict, *, approve: bool, approver: str) -> None:
        try:
            if mid not in self.runtime._missions:                # after a restart: the ledger, plus the inputs checked against its digest
                self.runtime.rehydrate(mid, inputs=args)
            parked = self.runtime.repo.pending_human(mid)
        except Exception as e:  # noqa: BLE001
            self._finish(mid, "failed", note=f"the request could not be restored ({type(e).__name__}); nothing was changed by this decision")
            return
        if not parked:
            self._finish(mid, "outcome_unknown", note="the ledger shows an earlier decision on this request; check the application")
            return
        try:
            self.runtime.approve(mid, parked["node_id"], "approve" if approve else "reject", actor=approver)
        except ApprovalError:
            self._finish(mid, "expired", note="the runtime refused the approval as out of date; nothing was changed")
            return
        except Exception as e:  # noqa: BLE001
            if mid not in self._ran:
                self._finish(mid, "outcome_unknown", note=f"the runtime stopped ({type(e).__name__}); check the application")
                return
        if not approve:
            self._finish(mid, "rejected", note="rejected; nothing was changed")
            return
        ran = self._ran.pop(mid, None)
        if ran is None:
            self._finish(mid, "failed", note="the runtime did not perform it (see the mission's events); nothing was changed")
        elif ran[0] != "done":
            self._finish(mid, ran[0], note=ran[1])
        else:
            o: Outcome = ran[1]
            state = ("executed_verified" if o.verified else "executed_unverified") if o.executed else ("no_change" if o.verified else "previewed")
            self._finish(mid, state, outcome=o)


__all__ = ["GovernedWrites", "WriteError", "FINAL", "PENDING", "ACTIONS", "APP_GRANTS"]

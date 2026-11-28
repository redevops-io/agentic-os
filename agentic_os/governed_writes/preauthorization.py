"""Developer mode: a person preauthorizes ONE agent to have a few named, low-consequence changes performed without a
separate approval for each, for a short time and a limited number of times.

It changes who gives the approval, never what an approval can do:

* **Off** unless the deployment sets ``AGENTIC_OS_DEVELOPER_MODE=1`` AND the tenant is granted ``writes:preauthorize``
  (with ``writes:decide``) among its ``policy_refs``.
* **Only a person who may decide** the tenant's changes creates one, on the human-only routes. No agent credential
  reaches them.
* **Narrow.** One tenant, one agent, named actions, optionally exact values some inputs may take; at most
  ``MAX_USES`` times within ``MAX_TTL_S``; revocable at once.
* **Protected changes are never covered**, whatever a record says: only the actions in ``ELIGIBLE`` (each reversible
  and not published), and only for the inputs its rule accepts. Anything that moves money or the ledger, pays for a
  model, publishes or schedules publication, records a person's judgement, or needs an attestation waits for a person.
  An action added later is not eligible until someone adds it here.
* **Local model runs, on a budget.** The actions in ``INFERENCE`` run a model. One is covered only where ALL of these
  hold: the deployment declares itself an isolated developer environment (``AGENTIC_OS_DEVELOPER_ENVIRONMENT=isolated``);
  the tenant also holds ``inference:local``; the media service reports the model as on-premises with no charge per use,
  AND the deployment lists that model in ``AGENTIC_OS_LOCAL_INFERENCE_BACKENDS`` (the service's word is not enough);
  and the preauthorization carries a budget (seconds of video, number of images) with enough left. A hosted or paid
  model waits for a person, whose approval is what authorizes the spend.
* **Same path.** The change goes through the same decision, authorization, permissions gate and read-back as a person's
  approval. A denial leaves the request waiting for a person; an approval never overrides one.
* **Recorded.** The decision names the preauthorization and who granted it, and each use is kept with its request.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

ENV_ENABLED = "AGENTIC_OS_DEVELOPER_MODE"
ENV_ENVIRONMENT = "AGENTIC_OS_DEVELOPER_ENVIRONMENT"          # "isolated": nothing here is a production system
ENV_LOCAL_BACKENDS = "AGENTIC_OS_LOCAL_INFERENCE_BACKENDS"    # model names the deployment says are its own and unpaid
TENANT_REF = "writes:preauthorize"
INFERENCE_REF = "inference:local"
#: actions that run a model: never covered except as described above, and always against a budget
INFERENCE = frozenset({"media.request_generate"})
BUDGET_MAX = {"video_seconds": 300, "images": 100}
DEFAULT_VIDEO_SECONDS = 5.0                                    # what the media service makes when no length is asked
MAX_TTL_S = 8 * 3600
MAX_USES = 50
MAX_MATCH_VALUES = 20

_always: Callable[[Mapping[str, Any]], bool] = lambda inputs: True
#: action -> whether THESE inputs may be preauthorized. The closed list of what developer mode can ever cover.
ELIGIBLE: Dict[str, Callable[[Mapping[str, Any]], bool]] = {
    "erp.request_comment": _always,                                     # a comment on a record
    "erp.request_draft_purchase_order": _always,                        # a DRAFT: orders and books nothing
    "support.request_private_note": _always,                            # never sent to the customer
    "crm.request_company": _always,                                     # creates a record, only if none matches
    "crm.request_person": _always,
    "social.request_post": lambda i: i.get("when") == "draft",          # a draft; scheduling is publication
    "market.request_watch": _always,                                    # starts reading a listed public page
}


class PreauthorizationError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code, self.detail = code, detail


def deployment_enabled() -> bool:
    return os.environ.get(ENV_ENABLED, "").strip() == "1"


def tenant_enabled(tenant: str) -> bool:
    """Developer mode and deciding are both granted to the tenant now. Without tenants, the deployment switch is it."""
    from agentic_os.mission.control_auth import ENV_TENANT_LIMITS, ConfigError, tenant_limits  # noqa: PLC0415
    if not deployment_enabled():
        return False
    if not os.environ.get(ENV_TENANT_LIMITS, "").strip():
        return True
    try:
        refs, _ = tenant_limits(tenant)
    except ConfigError:
        return False
    return {TENANT_REF, "writes:decide"} <= set(refs)


def inference_enabled(tenant: str) -> bool:
    """Developer mode, an isolated developer environment and, with tenants, the tenant's ``inference:local``."""
    from agentic_os.mission.control_auth import ENV_TENANT_LIMITS, ConfigError, tenant_limits  # noqa: PLC0415
    if not tenant_enabled(tenant) or os.environ.get(ENV_ENVIRONMENT, "").strip() != "isolated":
        return False
    if not os.environ.get(ENV_TENANT_LIMITS, "").strip():
        return True
    try:
        refs, _ = tenant_limits(tenant)
    except ConfigError:
        return False
    return INFERENCE_REF in set(refs)


def declared_local_backends() -> frozenset:
    return frozenset(x.strip() for x in os.environ.get(ENV_LOCAL_BACKENDS, "").split(",") if x.strip())


def local_and_free(facts: Any) -> bool:
    """The service says the model runs on-premises with no charge per use, and the deployment names that model too.
    Anything missing or of the wrong type means no."""
    if not isinstance(facts, Mapping):
        return False
    cost = facts.get("cost_usd")
    return (facts.get("local") is True and facts.get("costly") is False and isinstance(cost, (int, float))
            and not isinstance(cost, bool) and cost == 0 and isinstance(facts.get("backend"), str)
            and facts["backend"] in declared_local_backends())


def cost_of(inputs: Mapping[str, Any]) -> Dict[str, float]:
    """What one model run takes from a budget."""
    if inputs.get("kind") == "image":
        return {"images": 1.0}
    seconds = inputs.get("duration")
    return {"video_seconds": float(DEFAULT_VIDEO_SECONDS if seconds is None else seconds)}


def eligible(action: str, inputs: Mapping[str, Any]) -> bool:
    rule = ELIGIBLE.get(action)
    try:
        return bool(rule and rule(inputs))
    except Exception:  # noqa: BLE001
        return False


def _checked_budget(budget: Any) -> Dict[str, float]:
    if not isinstance(budget, Mapping) or not budget or set(budget) - set(BUDGET_MAX):
        raise PreauthorizationError("invalid", "a budget names video_seconds and/or images")
    out: Dict[str, float] = {}
    for key, value in budget.items():
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= BUDGET_MAX[key]:
            raise PreauthorizationError("invalid", f"budget.{key} must be a whole number from 1 to {BUDGET_MAX[key]}")
        out[key] = float(value)
    return out


class Preauthorizations:
    """Kept beside the requests, in the same database, so a use and its request are one record."""

    def __init__(self, db: Any, clock: Callable[[], float]) -> None:       # the service's serialized connection
        self._db, self.clock = db, clock
        db.execute("""CREATE TABLE IF NOT EXISTS preauthorizations(
            id TEXT PRIMARY KEY, tenant TEXT NOT NULL, requester TEXT NOT NULL, actions TEXT NOT NULL, match TEXT NOT NULL,
            max_uses INTEGER NOT NULL, uses INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, expires REAL NOT NULL,
            created_by TEXT NOT NULL, reason TEXT NOT NULL, revoked REAL, revoked_by TEXT, budget TEXT, spent TEXT)""")
        columns = {r[1] for r in db.execute("PRAGMA table_info(preauthorizations)").fetchall()}
        for column in ("budget", "spent"):                       # a table made before budgets existed
            if column not in columns:
                db.execute(f"ALTER TABLE preauthorizations ADD COLUMN {column} TEXT")
        db.execute("CREATE TABLE IF NOT EXISTS preauthorization_uses(preauthorization TEXT NOT NULL, request_id TEXT NOT NULL,"
                   " at REAL NOT NULL, outcome TEXT NOT NULL)")

    def create(self, *, tenant: str, requester: str, actions: Sequence[str], max_uses: int, minutes: int, created_by: str,
               reason: str, match: Optional[Mapping[str, Sequence[str]]] = None,
               budget: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        if not tenant_enabled(tenant):
            raise PreauthorizationError("disabled", f"developer mode is not on for this tenant ({ENV_ENABLED}=1, and {TENANT_REF} with "
                                                    "writes:decide among its policy_refs)")
        if not created_by or created_by.startswith("agent:") or created_by.startswith("preauthorization:"):
            raise PreauthorizationError("refused", "only a person grants a preauthorization")
        if not requester.startswith("agent:") or requester == created_by:
            raise PreauthorizationError("invalid", "name the one agent it is for, as its requests name it (agent:...)")
        wanted = sorted({str(a) for a in actions})
        covered = set(ELIGIBLE) | (INFERENCE if inference_enabled(tenant) else set())
        never = [a for a in wanted if a not in covered]
        if not wanted or never:
            local_only = [a for a in never if a in INFERENCE]
            why = (f"{', '.join(local_only)} runs a model: it can be preauthorized only in an isolated developer environment "
                   f"({ENV_ENVIRONMENT}=isolated) for a tenant that holds {INFERENCE_REF}. " if local_only else
                   f"{', '.join(never)} can never be preauthorized. " if never else "name at least one action. ")
            raise PreauthorizationError("invalid", why + "Eligible: " + ", ".join(sorted(covered)))
        if bool(set(wanted) & INFERENCE) != (budget is not None):
            raise PreauthorizationError("invalid", "a budget goes with an action that runs a model, and only with one")
        allowance = _checked_budget(budget) if budget is not None else None
        if not isinstance(max_uses, int) or isinstance(max_uses, bool) or not 1 <= max_uses <= MAX_USES:
            raise PreauthorizationError("invalid", f"uses must be 1 to {MAX_USES}")
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes * 60 <= MAX_TTL_S:
            raise PreauthorizationError("invalid", f"minutes must be 1 to {MAX_TTL_S // 60}")
        if not isinstance(reason, str) or not 4 <= len(reason.strip()) <= 300:
            raise PreauthorizationError("invalid", "say why, in 4 to 300 characters: it is kept with every use")
        clean: Dict[str, List[str]] = {}
        for key, values in dict(match or {}).items():
            if (not isinstance(key, str) or not isinstance(values, (list, tuple)) or not 1 <= len(values) <= MAX_MATCH_VALUES
                    or not all(isinstance(v, (str, int)) and not isinstance(v, bool) for v in values)):
                raise PreauthorizationError("invalid", "match maps an input name to up to 20 exact values it may take")
            clean[key] = [str(v) for v in values]
        ident, now = "pre_" + secrets.token_hex(8), self.clock()
        self._db.execute("INSERT INTO preauthorizations(id,tenant,requester,actions,match,max_uses,created,expires,created_by,reason,"
                         "budget,spent) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (ident, tenant, requester, json.dumps(wanted), json.dumps(clean), max_uses, now, now + minutes * 60, created_by,
                          reason.strip(), None if allowance is None else json.dumps(allowance), None if allowance is None else "{}"))
        return self.get(ident, tenant=tenant)

    def _view(self, row: Any) -> Dict[str, Any]:
        now = self.clock()
        state = ("revoked" if row["revoked"] else "expired" if now >= row["expires"] else "used up" if row["uses"] >= row["max_uses"] else "active")
        uses = self._db.execute("SELECT request_id, at, outcome FROM preauthorization_uses WHERE preauthorization=? ORDER BY at",
                                (row["id"],)).fetchall()
        return {"id": row["id"], "tenant": row["tenant"], "agent": row["requester"], "actions": json.loads(row["actions"]),
                "match": json.loads(row["match"]), "max_uses": row["max_uses"], "uses": row["uses"], "state": state,
                "created_at": row["created"], "expires_at": row["expires"], "granted_by": row["created_by"], "reason": row["reason"],
                "revoked_by": row["revoked_by"] or "", "used_for": [{"request_id": u["request_id"], "at": u["at"], "outcome": u["outcome"]} for u in uses],
                **({"budget": json.loads(row["budget"]), "spent": json.loads(row["spent"] or "{}")} if row["budget"] else {})}

    def get(self, ident: str, *, tenant: Optional[str] = None) -> Dict[str, Any]:
        row = self._db.execute("SELECT * FROM preauthorizations WHERE id=?", (ident,)).fetchone()
        if row is None or (tenant is not None and row["tenant"] != tenant):
            raise PreauthorizationError("not_found", "no such preauthorization")
        return self._view(row)

    def list(self, *, tenant: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        sql, args = "SELECT * FROM preauthorizations", []
        if tenant is not None:
            sql, args = sql + " WHERE tenant=?", [tenant]
        return [self._view(r) for r in self._db.execute(sql + " ORDER BY created DESC LIMIT ?", [*args, limit]).fetchall()]

    def revoke(self, ident: str, *, by: str, tenant: Optional[str] = None) -> Dict[str, Any]:
        self.get(ident, tenant=tenant)
        self._db.execute("UPDATE preauthorizations SET revoked=?, revoked_by=? WHERE id=? AND revoked IS NULL", (self.clock(), by, ident))
        return self.get(ident, tenant=tenant)

    def claim(self, *, tenant: str, requester: str, action: str, inputs: Mapping[str, Any],
              evidence: Optional[Mapping[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """Take ONE use of a live preauthorization that covers this exact request, or None. Eligibility is asked here, of
        the action, its inputs and (for a model run) the gathered evidence, so no stored record can widen it. A model run
        also takes its cost from the budget; ``charged`` in the answer says how much."""
        cost: Dict[str, float] = {}
        if action in INFERENCE:
            if not inference_enabled(tenant) or not local_and_free((evidence or {}).get("inference")):
                return None
            cost = cost_of(inputs)
        elif not eligible(action, inputs) or not tenant_enabled(tenant):
            return None
        now = self.clock()
        rows = self._db.execute("SELECT * FROM preauthorizations WHERE tenant=? AND requester=? AND revoked IS NULL AND expires>?"
                                " AND uses<max_uses ORDER BY expires", (tenant, requester, now)).fetchall()
        for row in rows:
            if action not in json.loads(row["actions"]):
                continue
            match = json.loads(row["match"])
            if any(str(inputs.get(k)) not in values or inputs.get(k) is None for k, values in match.items()):
                continue
            spent = row["spent"]
            if cost:
                allowance, used = json.loads(row["budget"] or "{}"), json.loads(row["spent"] or "{}")
                if any(not 0 < v <= allowance.get(k, 0) - used.get(k, 0) + 1e-9 for k, v in cost.items()):
                    continue                                     # not enough left (or nothing of that kind was granted)
                spent = json.dumps({**used, **{k: round(used.get(k, 0) + v, 3) for k, v in cost.items()}})
            taken = self._db.execute("UPDATE preauthorizations SET uses=uses+1, spent=? WHERE id=? AND uses<max_uses AND revoked IS NULL"
                                     " AND expires>? AND spent IS ?", (spent, row["id"], now, row["spent"])).rowcount
            if taken == 1:
                return {**self.get(row["id"]), "charged": cost}
        return None

    def record(self, ident: str, request_id: str, outcome: str, *, give_back: Optional[Mapping[str, float]] = None) -> None:
        if give_back is not None:                        # nothing was decided: neither the use nor the budget was spent
            row = self._db.execute("SELECT spent FROM preauthorizations WHERE id=?", (ident,)).fetchone()
            spent = row["spent"] if row else None
            if spent is not None:
                used = json.loads(spent)
                spent = json.dumps({**used, **{k: round(max(0.0, used.get(k, 0) - v), 3) for k, v in give_back.items()}})
            self._db.execute("UPDATE preauthorizations SET uses=uses-1, spent=? WHERE id=? AND uses>0", (spent, ident))
        self._db.execute("INSERT INTO preauthorization_uses VALUES(?,?,?,?)", (ident, request_id, self.clock(), outcome[:300]))


__all__ = ["ENV_ENABLED", "ENV_ENVIRONMENT", "ENV_LOCAL_BACKENDS", "TENANT_REF", "INFERENCE_REF", "INFERENCE", "BUDGET_MAX",
           "MAX_TTL_S", "MAX_USES", "ELIGIBLE", "PreauthorizationError", "Preauthorizations", "deployment_enabled", "tenant_enabled",
           "inference_enabled", "local_and_free", "cost_of", "eligible"]

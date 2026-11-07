"""The decision a side-effecting operator ``POST /invoke`` must pass, in every execution profile.

Three things are required together, in this order, and none of them is implied by the execution profile:

1. an AUTHENTICATED IDENTITY: an :class:`~agentic_os.mission.principal.AuthenticatedPrincipal` produced by the authentication layer
   (``control_auth`` bearer / ``X-Control-Token``). The "no credential" caller that ``AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS=1`` allows in
   local development is not an identity. The only exception is the NAMED TEST IDENTITY of an explicit ``AuthorityTestPolicy`` with
   ``anonymous_operators=True`` (non-production only), which is recorded as such.
2. CURRENT, SCOPED AUTHORITY, evaluated when the call arrives with ``AuthorityGuard.check`` (revocation, expiry, delegation chain, scope,
   resources, tier, wall clock; no counter moves):
     * a verified execution authorization (``execution_auth``): the envelope its reservation was made against is read again;
     * or a verified human approval bound to a mission that has an envelope (an approval satisfies an ESCALATE, never a DENY);
     * or, outside production only, an explicit ``AuthorityTestPolicy`` that lists the capability and only while no envelope covers the
       call: a test policy never overrides a revoked, expired, out-of-scope or tampered envelope.
3. the APPLICABLE APPROVAL: a capability declared ``approval_required`` runs only with a verified approval token bound to its exact inputs
   (an execution authorization can never stand in for it); an envelope verdict of ESCALATE (tier or amount past the grant) needs one too.
   Only a test policy naming the capability in ``unapproved`` waives a mandatory approval, outside production.

A read-only capability (``side_effecting=False``, no ``approval_required``) needs no envelope outside production, the same rule the mission
runtime applies to a read-only node; it still needs a credential whenever one is configured, and production never runs the anonymous caller.

Every decision is recorded (who, capability, resource, rule, reason) in :class:`InvokeAudit`; a refusal's response carries fixed wording and
never says whether an envelope, a mission or a lease exists. A side-effecting call whose allow decision cannot be written to a configured
durable audit store is refused (503) rather than run unrecorded.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable

from .approval import inputs_hash
from .authority import Action, Decision, Verdict
from .authority_policy import AuthorityTestPolicy
from .integrity import IntegrityError
from .principal import ALL_TENANTS, AuthenticatedPrincipal, default_issuer, principal_of_caller

_log = logging.getLogger("agentic_os.operator")

ENV_TEST_POLICY = "AGENTIC_OS_OPERATOR_TEST_POLICY"     # path to a JSON file holding ONE AuthorityTestPolicy (never in production)
ENV_AUDIT_LOG = "AGENTIC_OS_OPERATOR_AUDIT_LOG"         # path of a durable JSONL event log for /invoke decisions
ENV_REMOVED_FLAG = "AGENTIC_OS_ALLOW_UNAPPROVED_INVOKE"  # replaced by AuthorityTestPolicy.unapproved

#: fixed response wording: the same text whatever the reason, so a refusal says nothing about what exists
MSG_IDENTITY = "authentication required: a side-effecting call needs an authenticated identity"
MSG_AUTHORITY = "execution authorization required: this call is not authorised"
MSG_APPROVAL = "this capability requires a human approval; an execution authorization cannot satisfy it"
MSG_AUDIT = "this call could not be recorded, so it was not run"


class InvokeRefused(Exception):
    """The gate refused the call: ``status`` and the fixed ``message`` are the whole response."""

    def __init__(self, status: int, message: str, rule: str = "", headers: "dict | None" = None):
        super().__init__(message)
        self.status, self.message, self.rule, self.headers = status, message, rule, headers or {}


@dataclass(frozen=True)
class Who:
    """The identity a decision is made for: an authenticated principal, or the named test identity of a test policy, or nothing (a read)."""
    principal: "AuthenticatedPrincipal | None" = None
    test_identity: str = ""

    def record(self) -> dict:
        if self.principal is not None:
            return {**self.principal.record(), "authenticated": True}
        if self.test_identity:
            return {"id": f"test-identity:{self.test_identity}", "kind": "test", "authenticated": False,
                    "method": "test-policy", "basis": f"test_policy:{self.test_identity}"}
        return {"id": "", "kind": "anonymous", "authenticated": False}


@dataclass(frozen=True)
class InvokeDecision:
    allow: bool
    status: int = 200
    message: str = ""
    rule: str = ""
    reason: str = ""
    basis: str = ""                  # envelope | execution_authorization | test_policy:<name> | read_only
    stage: bool = False              # approval_required and no approval: stage a preview, run nothing
    escalated: bool = False          # an envelope ESCALATE was satisfied by a verified approval
    approval: str = "not_required"   # not_required | verified | missing | waived:<policy>
    envelope_key: str = ""
    envelope_digest: str = ""
    tenant: str = ""
    mission_id: str = ""
    lease_id: str = ""


def _refuse(rule: str, reason: str, *, status: int = 403, message: str = MSG_AUTHORITY, **kw) -> InvokeDecision:
    return InvokeDecision(False, status, message, rule, reason, **kw)


# ── the explicit test policy ────────────────────────────────────────────────────────────────────────────────────────
def load_test_policy(path: str) -> AuthorityTestPolicy:
    with open(path, encoding="utf-8") as f:
        return AuthorityTestPolicy.from_dict(json.load(f))


def test_policy_from_env() -> "AuthorityTestPolicy | None":
    path = os.environ.get(ENV_TEST_POLICY, "").strip()
    return load_test_policy(path) if path else None


# ── audit ────────────────────────────────────────────────────────────────────────────────────────────────────────────
class InvokeAudit:
    """Where /invoke decisions go: a bounded in-memory ring (``recent``), the ``agentic_os.operator.audit`` logger and, when configured,
    the kernel's append-only hash-chained event log (the same ``EventStore`` the missions and the agent gateway use), one stream per
    operator. Arguments and outputs are never recorded: only a hash of the inputs."""

    TYPES = {"allowed": "OperatorInvokeAllowed", "staged": "OperatorInvokeStaged", "refused": "OperatorInvokeRefused"}

    def __init__(self, operator: str, store: Any = None, *, ring: int = 10_000) -> None:
        self.operator = operator
        self.store = store
        self.recent: "deque[dict]" = deque(maxlen=ring)
        self._lock = threading.Lock()
        self.stream = f"operator-invoke:{operator}"

    @classmethod
    def from_env(cls, operator: str) -> "InvokeAudit":
        path = os.environ.get(ENV_AUDIT_LOG, "").strip()
        store = None
        if path:
            from .store import EventStore  # noqa: PLC0415
            store = EventStore(path)
        return cls(operator, store)

    def write(self, outcome: str, payload: dict, *, durable: bool) -> None:
        """Record one decision. ``durable``: an exception from the store propagates (the caller then refuses the call)."""
        rec = {"outcome": outcome, **payload}
        with self._lock:
            self.recent.append(rec)
        _logging = logging.getLogger("agentic_os.operator.audit")
        _logging.log(logging.INFO if outcome != "refused" else logging.WARNING, "%s %s %s rule=%s", self.operator, outcome,
                     payload.get("capability", ""), payload.get("rule", ""))
        if self.store is None:
            return
        try:
            self.store.append(self.TYPES[outcome], self.stream, rec)
        except Exception:  # noqa: BLE001
            if durable:
                raise
            _log.error("operator %s: a %s decision could not be written to the audit store", self.operator, outcome)


# ── the gate ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def default_authority_keys(principal: "AuthenticatedPrincipal | None", mission_id: str) -> "list[str]":
    """The envelope keys to try for an approved call that carries no execution authorization: the mission's own envelope, then the
    tenant's direct-action envelope (the convention ``ActionAuthorizer`` uses)."""
    keys = [mission_id] if mission_id else []
    tenant = getattr(principal, "tenant", "") or ""
    if tenant and tenant != ALL_TENANTS:
        keys.append(f"{tenant}:direct")
    return keys


class InvokeGate:
    def __init__(self, operator: Any, *, audit: "InvokeAudit | None" = None,
                 authority_keys: "Callable[[AuthenticatedPrincipal | None, str], Iterable[str]] | None" = None,
                 principal_verifier: Any = None) -> None:
        self.op = operator
        self.audit = audit or InvokeAudit.from_env(operator.name)
        self.authority_keys = authority_keys or default_authority_keys
        self.verifier = principal_verifier

    # context ---------------------------------------------------------------------------------------------------
    @staticmethod
    def _production() -> bool:
        from .profile import is_production  # noqa: PLC0415
        return is_production()

    def policy(self) -> "AuthorityTestPolicy | None":
        """The explicit test policy in force, or None. Never in production, whatever the class or instance says."""
        return None if self._production() else getattr(self.op, "test_policy", None)

    def _spec(self, capability: str):
        return self.op._specs.get(capability)

    def classify(self, capability: str) -> "tuple[Any, bool, bool]":
        """(spec, side_effecting, approval_required). An unknown capability counts as side-effecting: a caller without authority
        cannot tell it from a real one."""
        spec = self._spec(capability)
        return spec, (spec is None or bool(spec.side_effecting)), bool(spec is not None and spec.approval_required)

    def _verify(self, principal: Any) -> bool:
        v = self.verifier or default_issuer()
        try:
            return bool(v.verify(principal))
        except Exception:  # noqa: BLE001
            return False

    # audit -----------------------------------------------------------------------------------------------------
    def _payload(self, capability: str, who: Who, dec: "InvokeDecision | None", inputs: "dict | None", key: str,
                 side: bool, claims: "dict | None") -> dict:
        try:
            from .profile import profile  # noqa: PLC0415
            prof = profile()
        except Exception:  # noqa: BLE001
            prof = "invalid"
        tp = self.policy()
        d = dec or InvokeDecision(False)
        return {"operator": self.op.name, "capability": capability, "side_effecting": side, "who": who.record(),
                "tenant": d.tenant or (claims or {}).get("tenant", ""), "mission_id": d.mission_id or (claims or {}).get("mission_id", ""),
                "lease_id": d.lease_id or (claims or {}).get("lease_id", ""), "envelope_key": d.envelope_key,
                "envelope_digest": d.envelope_digest, "rule": d.rule, "reason": d.reason, "authority_basis": d.basis,
                "approval": d.approval, "status": d.status, "profile": prof, "idempotency_key": key,
                "inputs_sha256": inputs_hash(inputs) if inputs is not None else "",
                "test_policy": ({"name": tp.name, "digest": tp.digest()} if tp is not None else None), "ts": time.time()}

    def _record(self, outcome: str, capability: str, who: Who, dec: "InvokeDecision | None", inputs: "dict | None", key: str,
                side: bool, needs_approval: bool, claims: "dict | None") -> None:
        durable = outcome != "refused" and (side or needs_approval)
        recorded = outcome == "refused" or side or needs_approval            # a plain read is kept in the ring only
        payload = self._payload(capability, who, dec, inputs, key, side, claims)
        if recorded:
            self.audit.write(outcome, payload, durable=durable)
        else:
            with self.audit._lock:
                self.audit.recent.append({"outcome": outcome, **payload})

    def _deny(self, capability: str, who: Who, dec: InvokeDecision, inputs: "dict | None", key: str, side: bool,
              needs_approval: bool, claims: "dict | None", headers: "dict | None" = None) -> InvokeRefused:
        try:
            self._record("refused", capability, who, dec, inputs, key, side, needs_approval, claims)
        except Exception:  # noqa: BLE001 - a refusal stays a refusal whatever happens to the log
            pass
        return InvokeRefused(dec.status, dec.message, dec.rule, headers)

    # 1. identity ---------------------------------------------------------------------------------------------------
    def identify(self, caller: Any, capability: str, key: str = "") -> Who:
        """Who is calling, before anything single-use is consumed. Raises ``InvokeRefused`` (401) for a side-effecting call with no
        identity. A read may run anonymously outside production (the credential-less caller cannot exist in production)."""
        spec, side, needs_approval = self.classify(capability)
        if not getattr(caller, "anonymous", False):
            return Who(principal=principal_of_caller(caller))
        if not side:
            return Who()
        tp = self.policy()
        if tp is not None and tp.anonymous_operators:
            return Who(test_identity=tp.name)
        dec = _refuse("no_identity", "the caller has no authenticated identity and no test policy accepts anonymous operators",
                      status=401, message=MSG_IDENTITY)
        raise self._deny(capability, Who(), dec, None, key, side, needs_approval, None, {"WWW-Authenticate": "Bearer"})

    # 2 + 3. authority and approval -------------------------------------------------------------------------------------
    def authorize(self, who: Who, capability: str, inputs: dict, *, claims: "dict | None" = None, approval: "dict | None" = None,
                  approval_mission: str = "", key: str = "") -> InvokeDecision:
        """Decide a call whose single-use proofs have already been verified. Returns the allow (or staged) decision, or raises
        ``InvokeRefused``. The decision is recorded either way."""
        spec, side, needs_approval = self.classify(capability)
        try:
            dec = self._decide(who, spec, capability, inputs, claims, approval, approval_mission, side, needs_approval)
        except Exception:  # noqa: BLE001 - anything unexpected is a refusal, never a pass
            _log.exception("operator %s: the invoke gate failed for %s", self.op.name, capability)
            dec = _refuse("gate_error", "the invoke gate failed")
        if not dec.allow:
            raise self._deny(capability, who, dec, inputs, key, side, needs_approval, claims)
        try:
            self._record("staged" if dec.stage else "allowed", capability, who, dec, inputs, key, side, needs_approval, claims)
        except Exception:  # noqa: BLE001 - the store refused the record: do not run unrecorded
            dec503 = _refuse("audit_unavailable", "the allow decision could not be recorded", status=503, message=MSG_AUDIT)
            raise self._deny(capability, who, dec503, inputs, key, side, needs_approval, claims)
        return dec

    def _decide(self, who: Who, spec: Any, capability: str, inputs: dict, claims: "dict | None", approval: "dict | None",
                approval_mission: str, side: bool, needs_approval: bool) -> InvokeDecision:
        tp = self.policy()
        approved = approval is not None
        if who.principal is not None and not self._verify(who.principal):
            return _refuse("forged_principal", "the principal was not issued by the authentication layer", status=401, message=MSG_IDENTITY)
        if side:
            auth = self._authority(who, spec, capability, inputs, claims, approval, approval_mission, tp)
            if not auth.allow:
                return auth
        else:
            auth = InvokeDecision(True, basis="read_only", rule="read_only", reason="no side effect to authorise")
        if needs_approval and not approved:
            if claims is not None:
                return _refuse("approval_required", "an execution authorization cannot satisfy a mandatory approval",
                               message=MSG_APPROVAL, basis=auth.basis, tenant=auth.tenant, mission_id=auth.mission_id, lease_id=auth.lease_id,
                               envelope_key=auth.envelope_key, envelope_digest=auth.envelope_digest, approval="missing")
            if tp is not None and tp.waives_approval(capability):
                return _carry(auth, approval=f"waived:{tp.name}")
            return _carry(auth, stage=True, approval="missing", rule="approval_required",
                          reason="the capability needs a human approval; nothing was run")
        if auth.escalated and not approved:
            return _refuse("escalation_unapproved", "the envelope needs a human approval for this call and none was presented")
        return _carry(auth, approval="verified" if approved else "not_required")

    def _tenant_ok(self, principal: "AuthenticatedPrincipal | None", tenant: str) -> bool:
        """A principal acts only for its own tenant; the unscoped admin credential (tenant ``*``) acts for any."""
        if principal is None:
            return False
        return principal.tenant == ALL_TENANTS or (bool(tenant) and principal.tenant == tenant)

    def _action(self, capability: str, inputs: dict, tier: int) -> Action:
        args = {k: v for k, v in inputs.items() if not str(k).startswith("_")}
        for k, v in ((inputs.get("_path_params") or {}).items() if isinstance(inputs.get("_path_params"), dict) else ()):
            args[f"path.{k}"] = v                     # REST routes: path parameters are selectable resources (as ActionAuthorizer does)
        return Action(capability=capability, arguments=args, risk_tier=tier, side_effecting=True)

    def _evaluate(self, guard: Any, key: str, tenant: str, action: Action) -> Decision:
        try:
            return guard.check(key, tenant, action)
        except IntegrityError as e:
            return Decision(Verdict.DENY, "authority_integrity", f"the authority store failed its integrity check: {e}")

    def _from_envelope(self, d: Decision, key: str, tenant: str, approved: bool, **ctx: str) -> InvokeDecision:
        common = dict(envelope_key=key, envelope_digest=d.envelope_digest, tenant=tenant, **ctx)
        if d.verdict is Verdict.ALLOW:
            return InvokeDecision(True, rule=d.rule, reason=d.reason, basis="envelope", **common)
        if d.verdict is Verdict.ESCALATE:
            if approved:
                return InvokeDecision(True, rule=d.rule, reason=f"{d.reason}; extended by a verified approval", basis="envelope",
                                      escalated=True, **common)
            return _refuse(d.rule, d.reason, basis="envelope", **common)
        return _refuse(d.rule, d.reason, basis="envelope", **common)

    def _authority(self, who: Who, spec: Any, capability: str, inputs: dict, claims: "dict | None", approval: "dict | None",
                   approval_mission: str, tp: "AuthorityTestPolicy | None") -> InvokeDecision:
        guard = self.op.authority
        production = self._production()
        principal = who.principal
        approved = approval is not None
        irreversible = spec is not None and not (getattr(spec, "undo", None) or getattr(spec, "compensation", None))
        default_tier = 2 if (spec is not None and irreversible) else 1

        if claims is not None:
            tenant = str(claims.get("tenant") or "")
            ctx = dict(mission_id=str(claims.get("mission_id") or ""), lease_id=str(claims.get("lease_id") or ""))
            if not self._tenant_ok(principal, tenant):
                return _refuse("tenant_mismatch", "the authenticated principal does not act for the tenant the authorization was issued to",
                               tenant=tenant, **ctx)
            if guard is None:
                if production:
                    return _refuse("authority_unavailable", "this operator has no authority store to evaluate the envelope against",
                                   tenant=tenant, **ctx)
                return InvokeDecision(True, rule="execution_authorization", basis="execution_authorization", tenant=tenant,
                                      reason="verified execution authorization; this operator has no authority store to re-evaluate it", **ctx)
            lease = guard.lease(ctx["lease_id"])
            meta = (lease or {}).get("meta") or {}
            if lease is None or meta.get("capability") != capability or (meta.get("tenant") and meta.get("tenant") != tenant):
                return _refuse("lease_unknown", "no matching reservation backs this authorization", tenant=tenant, **ctx)
            tier = int(meta.get("tier", default_tier))
            d = self._evaluate(guard, str(lease["key"]), tenant, self._action(capability, inputs, tier))
            return self._from_envelope(d, str(lease["key"]), tenant, approved, **ctx)

        if approved and guard is not None:
            mission = approval_mission
            for key in self.authority_keys(principal, mission):
                try:
                    env = guard.envelopes.get(key)
                except IntegrityError as e:
                    return _refuse("authority_integrity", f"the authority store failed its integrity check: {e}", envelope_key=key, mission_id=mission)
                if env is None:
                    continue
                if not self._tenant_ok(principal, env.tenant):
                    continue                                   # another tenant's envelope: indistinguishable from none
                d = self._evaluate(guard, key, env.tenant, self._action(capability, inputs, default_tier))
                return self._from_envelope(d, key, env.tenant, True, mission_id=mission)
        if production:
            return _refuse("no_authority", "no execution authorization or envelope covers this call")
        if tp is not None and tp.permits(capability):
            return InvokeDecision(True, rule="test_policy", basis=f"test_policy:{tp.name}",
                                  reason=f"allowed by test policy {tp.name} (no envelope covers this call)")
        return _refuse("no_authority", "no envelope covers this call and no explicit test policy is configured" if tp is None else
                       f"no envelope covers this call and test policy {tp.name!r} does not list the capability")


def _carry(d: InvokeDecision, **changes: Any) -> InvokeDecision:
    return replace(d, **changes)

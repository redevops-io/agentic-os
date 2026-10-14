"""Executor — runs ONE node durably by calling the owning operator's capability.

In production a node becomes a Dagster op that POSTs the operator's /invoke (the reel-job
pattern generalized); here an injectable `OperatorClient` runs it in-process. Two guarantees the
executor is responsible for:

  * exactly-once side effects — retries dedupe on the node's idempotency_key (Dagster gives
    retry, not exactly-once; the operator + this cache give exactly-once).
  * sagas — a failed/rolled-back side-effecting node is compensated via its `undo` capability.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import OrderedDict
from typing import Any, Callable, Protocol

from .types import Node


class OperatorError(Exception):
    pass


class OperatorClient(Protocol):
    # `secrets` is an EPHEMERAL, per-invocation channel of redeemed SecretMaterial keyed by requirement
    # name. It is never part of `inputs` (which may be persisted/logged) and never returned/stored.
    def invoke(self, operator: str, capability: str, inputs: dict, idempotency_key: str,
               *, secrets: "dict | None" = None) -> dict: ...


class InMemoryOperatorClient:
    """Operators as in-process callables — for the demo/tests. Each handler is
    fn(inputs) -> result_dict (or fn(inputs, secrets) if it needs redeemed material); raise to simulate a
    failure. Dedupes on idempotency_key so a retried side-effecting call returns the first result instead of
    running twice. Redeemed `secrets` are passed to the handler but NEVER stored in the dedupe cache."""

    def __init__(self, handlers: dict[str, Callable[..., dict]]):
        self._handlers = handlers
        self._seen: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []   # (capability, idempotency_key) — for assertions

    def invoke(self, operator: str, capability: str, inputs: dict, idempotency_key: str,
               *, secrets: "dict | None" = None) -> dict:
        if idempotency_key and idempotency_key in self._seen:
            return self._seen[idempotency_key]          # exactly-once: return the prior result
        self.calls.append((capability, idempotency_key))
        fn = self._handlers.get(capability)
        if fn is None:
            raise OperatorError(f"operator '{operator}' has no handler for '{capability}'")
        # A handler that declares a second parameter receives the ephemeral redeemed material.
        import inspect  # noqa: PLC0415
        try:
            arity = len(inspect.signature(fn).parameters)
        except (TypeError, ValueError):
            arity = 1
        result = (fn(inputs, secrets or {}) if arity >= 2 else fn(inputs)) or {}
        if idempotency_key:
            self._seen[idempotency_key] = result        # result only — never the secrets
        return result


class Sandbox(Protocol):
    """Opt-in isolation boundary. A capability that declares an isolation class runs its invoke through
    this instead of in-process. Duck-typed so the (enterprise) sandbox implementation is injected, never
    imported here. ``grants`` are the (unredeemed) credential grants the sandbox redeems INSIDE the
    boundary (§21) — the executor passes references, not material, across into confinement."""
    def invoke(self, operator: str, capability: str, inputs: dict, idempotency_key: str,
               *, isolation: str, grants: "list | None" = None) -> dict: ...


class SecurityMonitorSpi(Protocol):
    """Opt-in security-telemetry sink. The executor calls ``observe`` at the capability boundary — outside
    the operator/model's control — so a compromised agent cannot lie by omission about what it did."""
    def observe(self, node: Node, result: dict | None, *, isolation: str, error: str | None = None) -> None: ...


class CredentialBrokerSpi(Protocol):
    """Opt-in credential-broker seam. The executor issues an authority-scoped grant at the boundary and
    redeems it ONLY here (or inside the sandbox) — never in the planner/context. Duck-typed so the
    (enterprise Vault/OpenBao) broker is injected, never imported. ``assurance_level`` gates fail-closed
    admission of capabilities that declare ``production_broker_required``."""
    assurance_level: str

    def grant(self, request: "Any", *, authority_context: "Any") -> "Any": ...
    def redeem(self, grant: "Any", *, capability_id: str, mission_id: str) -> "Any": ...
    def revoke_grant(self, grant_id: str, *, reason: str) -> None: ...


class Executor:
    def __init__(self, client: OperatorClient, *, sandbox: "Sandbox | None" = None,
                 isolation_for: "Callable[[Node], str] | None" = None,
                 monitor: "SecurityMonitorSpi | None" = None,
                 authority: "Any | None" = None,
                 authority_for: "Callable[[Node], Any] | None" = None,
                 broker: "CredentialBrokerSpi | None" = None,
                 credentials_for: "Callable[[Node], Any] | None" = None):
        self.client = client
        # Opt-in isolation seam. `isolation_for(node)` reports the isolation class a capability requires
        # ("" | "in_process" | "sandbox" | "strict"), e.g. from its CapabilityDescriptor. When it requires
        # confinement, execution routes through `sandbox`. All default None → behaviour is unchanged.
        self.sandbox = sandbox
        self.isolation_for = isolation_for
        self.monitor = monitor
        # Opt-in delegated-authority seam. `authority` is the mission's leased AuthorityContext (already
        # narrowed to this mission's scope); `authority_for(node)` reports the permissions a capability
        # requires (e.g. its CapabilityDescriptor.required_authority). When BOTH are wired, a node whose
        # required authority is not covered by the leased chain is refused at the boundary — a capability
        # can never exercise authority the mission was not delegated. Both None → behaviour unchanged.
        self.authority = authority
        self.authority_for = authority_for
        # Opt-in credential-broker seam. `credentials_for(node)` reports the capability's declared
        # CredentialRequirements (e.g. resolved from CapabilityDescriptor.secrets). When BOTH are wired, the
        # executor admits (fail-closed on a declared production requirement), issues an authority-scoped
        # grant, and redeems it into ephemeral material ONLY at this boundary — never in the planner/context
        # — then destroys the material and revokes the grant after use. Both None → behaviour unchanged.
        self.broker = broker
        self.credentials_for = credentials_for
        # Compensation authorization. `evidence_probe(mission_id, node_id) -> dict` is wired by the runtime
        # and reads the DURABLE event log (restart-safe): {"executed", "capability", "approved",
        # "compensated_result"}. The in-memory ledger below is the fallback when no probe is wired.
        self.evidence_probe: "Callable[[str, str], dict] | None" = None
        # `undo_registry(capability) -> registered undo capability | None` is the TRUSTED declaration (the
        # capability registry); the plan node's `undo` is planner-supplied and is only honoured when equal.
        self.undo_registry: "Callable[[str], str | None] | None" = None
        self._ledger: "OrderedDict[tuple, dict]" = OrderedDict()      # bounded (LRU)
        self._compensated: "OrderedDict[tuple, dict]" = OrderedDict()  # bounded (LRU)

    _LEDGER_CAP = 4096

    def _bounded_put(self, d: "OrderedDict", k: tuple, v: dict) -> None:
        d[k] = v
        d.move_to_end(k)
        while len(d) > self._LEDGER_CAP:
            d.popitem(last=False)

    @staticmethod
    def _executed_ok(result: Any) -> bool:
        """The forward result proves the effect happened: not staged, not failure-shaped, not executed=False."""
        from .verify import result_failed, is_staged  # noqa: PLC0415
        if not isinstance(result, dict) or is_staged(result) or result.get("executed") is False:
            return False
        return not result_failed(result)[0]

    @staticmethod
    def _input_hash(inputs: dict) -> str:
        clean = {k: v for k, v in (inputs or {}).items() if not str(k).lstrip("_").startswith("approval")
                 and k not in ("_mission", "_goal", "_node")}
        try:
            blob = json.dumps(clean, sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            blob = repr(sorted(map(str, clean)))
        return hashlib.sha256(blob.encode()).hexdigest()

    def _mission_id(self) -> str:
        return getattr(self.monitor, "mission_id", "") or ""

    def _emit_credential(self, node: Node, event_type: str, *, grant: "Any | None" = None,
                         reason: str = "") -> None:
        """Emit a canonical credential event through the monitor if it supports it (duck-typed) — safe
        fields only (grant id, secret_ref fingerprint, authority ref); never material."""
        fn = getattr(self.monitor, "observe_credential", None)
        if callable(fn):
            fn(node, event_type, grant=grant, reason=reason)

    def _provision(self, node: Node) -> "tuple[list, dict]":
        """Admit → grant → redeem the node's declared credential requirements. Returns (grants, secrets).
        Raises OperatorError (after emitting a DENIED event) if admission or a grant is refused."""
        from runtime_contracts.protocol.secrets import CredentialRequest, DenyReason  # noqa: PLC0415
        from runtime_contracts.protocol.secrets import admit_credentials  # noqa: PLC0415
        from runtime_contracts import SecurityVerdict  # noqa: PLC0415

        requirements = tuple(self.credentials_for(node) or ())
        if not requirements:
            return [], {}

        # Fail-closed admission — a declared production requirement with no production broker does not run.
        admit = admit_credentials(requirements, self.broker)
        if admit.verdict != SecurityVerdict.ALLOW:
            self._emit_credential(node, "CREDENTIAL_GRANT_DENIED", reason=admit.reason)
            raise OperatorError(f"capability '{node.capability}' credential admission denied: {admit.reason}")

        mission_id = self._mission_id()
        tenant = getattr(getattr(self.authority, "principal", None), "tenant", "") or ""
        ctx_id = getattr(self.authority, "chain_ref", "") or getattr(self.authority, "authority_id", "") or ""
        node_id = node.idempotency_key or node.capability

        grants: list = []
        secrets: dict = {}
        try:
            for i, req in enumerate(requirements):
                request = CredentialRequest(
                    request_id=f"{mission_id or 'm'}:{node_id}:{i}", mission_id=mission_id, node_id=node_id,
                    capability_id=node.capability, tenant_id=tenant, authority_context_id=ctx_id,
                    requirement=req, requested_ttl_seconds=req.max_ttl_seconds)
                grant = self.broker.grant(request, authority_context=self.authority)
                grants.append(grant)
                self._emit_credential(node, "CREDENTIAL_GRANT_ISSUED", grant=grant)
                # Redeem at the boundary (in-process path); the sandbox path redeems inside instead.
                material = self.broker.redeem(grant, capability_id=node.capability, mission_id=mission_id)
                secrets[req.name] = material
                self._emit_credential(node, "CREDENTIAL_REDEEMED", grant=grant)
        except Exception as e:
            # any grant/redeem failure: release what we took and refuse the node
            self._release(node, grants, secrets, reason=f"provision failed: {e}")
            self._emit_credential(node, "CREDENTIAL_GRANT_DENIED", reason=str(e))
            raise OperatorError(f"capability '{node.capability}' credential provisioning failed: {e}") from e
        return grants, secrets

    def _release(self, node: Node, grants: list, secrets: dict, *, reason: str = "after use") -> None:
        """Destroy redeemed material and revoke the grants — best effort, never raises past the caller."""
        for m in secrets.values():
            try:
                m.destroy()
            except Exception:  # noqa: BLE001
                pass
        for g in grants:
            try:
                if getattr(g, "revocable", True):
                    self.broker.revoke_grant(g.grant_id, reason=reason)
                    self._emit_credential(node, "CREDENTIAL_GRANT_REVOKED", grant=g, reason=reason)
            except Exception:  # noqa: BLE001
                pass

    def run(self, node: Node, inputs: dict) -> dict:
        node.attempts += 1
        isolation = self.isolation_for(node) if self.isolation_for else ""
        if self.authority is not None and self.authority_for is not None:
            required = tuple(self.authority_for(node) or ())
            missing = [p for p in required if not self.authority.permits(p)]
            if missing:
                # Fail closed: the capability declares authority the leased chain does not cover. Refuse
                # before any side effect; the refusal is itself telemetry.
                err = (f"capability '{node.capability}' requires authority {missing} not covered by the "
                       f"leased authority chain {self.authority.chain_ref}")
                if self.monitor is not None:
                    self.monitor.observe(node, None, isolation=isolation, error=err)
                raise OperatorError(err)

        # Provision declared credentials at the boundary (fail-closed). grants/secrets are ephemeral and
        # released (material destroyed, grants revoked) in the finally below — planner/context never see them.
        grants: list = []
        secrets: dict = {}
        if self.broker is not None and self.credentials_for is not None:
            grants, secrets = self._provision(node)
        try:
            if isolation in {"sandbox", "strict"}:
                if self.sandbox is None:
                    # Fail closed: a capability that DECLARES isolation must not silently run in-process
                    # because a caller omitted the sandbox plane. Declaring isolation is the opt-in;
                    # enforcing it is not.
                    raise OperatorError(
                        f"capability '{node.capability}' requires isolation '{isolation}' but no sandbox is "
                        "wired — refusing to run it unconfined")
                # The sandbox redeems the grants INSIDE the boundary (§21); pass references, not material.
                # Only pass `grants` when there are any, so sandboxes without the kwarg stay compatible.
                if grants:
                    result = self.sandbox.invoke(node.operator, node.capability, inputs, node.idempotency_key,
                                                 isolation=isolation, grants=grants)
                else:
                    result = self.sandbox.invoke(node.operator, node.capability, inputs, node.idempotency_key,
                                                 isolation=isolation)
            elif secrets:
                # broker-provisioned material — pass the ephemeral channel to a broker-aware client
                result = self.client.invoke(node.operator, node.capability, inputs, node.idempotency_key,
                                            secrets=secrets)
            else:
                result = self.client.invoke(node.operator, node.capability, inputs, node.idempotency_key)
        except Exception as e:
            # Emit at the boundary even on failure — a refused/failed side effect is itself telemetry.
            if self.monitor is not None:
                self.monitor.observe(node, None, isolation=isolation, error=str(e))
            raise
        finally:
            # Always destroy redeemed material and revoke grants — after use or on failure.
            if grants or secrets:
                self._release(node, grants, secrets)
        if self.monitor is not None:
            self.monitor.observe(node, result, isolation=isolation)
        mid = str((inputs or {}).get("_mission") or "").strip()
        if mid and node.capability and self._executed_ok(result):
            appr = inputs.get("_approval")
            self._bounded_put(self._ledger, (mid, node.id, node.capability, node.idempotency_key), {
                "capability": node.capability, "input_sha256": self._input_hash(inputs),
                "approved": isinstance(appr, dict) and appr.get("approved") is True,
                "result": copy.deepcopy(result)})
        return result

    def _skip(self, node: Node, reason: str) -> dict:
        # Visible, non-executing outcome. `undo_error` makes the runtime record NodeCompensationFailed and
        # set compensation_incomplete, so humans are told the forward effect remains.
        return {"compensation_skipped": True, "status": "compensation_skipped", "executed": False,
                "undo_error": f"compensation skipped for '{node.capability}': {reason}"}

    def _evidence(self, node: Node, mission_id: str) -> dict:
        if self.evidence_probe is not None:
            return dict(self.evidence_probe(mission_id, node.id) or {})
        led = self._ledger.get((mission_id, node.id, node.capability, node.idempotency_key))
        if led is None:
            return {"executed": False}
        return {"executed": True, "capability": led["capability"], "approved": led["approved"],
                "result": led["result"],
                "compensated_result": self._compensated.get((mission_id, node.id, node.capability,
                                                              node.idempotency_key))}

    def compensate(self, node: Node, *, mission_id: str | None = None) -> dict | None:
        """Run the node's undo capability (saga) — best effort; never raises past the caller.

        Fails closed (docs/APPROVAL_TOKENS.md, "Compensation authorization"). The undo is invoked only if
        THIS node (mission + node id + capability) is shown to have EXECUTED in THIS mission: durable event
        log via ``evidence_probe`` (restart-safe), else this executor's ledger (same rules). Otherwise no
        operator call is made and a visible ``compensation_skipped`` result is returned. The signed
        compensation marker is minted only when, in addition, ``node.undo`` equals the capability registry's
        declared undo for the forward capability; a planner-attached undo is refused (``undo_not_registered``).
        The undo's inputs come from the logged/ledgered forward result, never from plan-supplied fields.
        Compensating twice returns the first outcome without a second undo call. Client-supplied
        ``_approval*`` is never consulted."""
        if not node.undo:
            return None
        mid = (mission_id or "").strip()
        if not mid:
            return self._skip(node, "forward_not_executed: no mission id")
        key = (mid, node.id, node.capability, node.idempotency_key)
        try:
            ev = self._evidence(node, mid)
        except Exception as e:  # noqa: BLE001
            return self._skip(node, f"forward_not_executed: evidence unavailable: {e}")
        if not ev.get("executed"):
            return self._skip(node, f"forward_not_executed: no record that node {node.id} executed in "
                                    f"mission {mid}")
        if not ev.get("capability") or ev.get("capability") != node.capability:
            return self._skip(node, f"forward_not_executed: node {node.id} executed capability "
                                    f"'{ev.get('capability')}', not '{node.capability}'")
        prior = ev.get("compensated_result")
        if prior is None:
            prior = self._compensated.get(key)
        if prior is not None:
            return prior                      # idempotent: already compensated, no second undo call
        registered = self.undo_registry(node.capability) if self.undo_registry is not None else None
        if self.undo_registry is not None and registered != node.undo:
            return self._skip(node, f"undo_not_registered: '{node.undo}' is not the registered undo of "
                                    f"'{node.capability}'")
        inputs: dict = {"undo_of": node.capability, "result": copy.deepcopy(ev.get("result")),
                        "_node": node.id, "_mission": mid}
        if self.undo_registry is not None:
            # Trusted registry declares this undo and the forward step demonstrably executed: the undo is
            # part of the scope of what ran, so it carries the compensation marker.
            inputs["_approval"] = {"approved": True, "compensation": True}
        try:
            out = self.client.invoke(node.operator, node.undo, inputs,
                                     f"{node.idempotency_key}:undo")
        except Exception as e:  # noqa: BLE001
            return {"undo_error": str(e)}
        from .verify import result_failed, is_staged  # noqa: PLC0415
        if isinstance(out, dict) and not out.get("undo_error") and not is_staged(out) \
                and not result_failed(out)[0]:
            self._bounded_put(self._compensated, key, out)
        return out

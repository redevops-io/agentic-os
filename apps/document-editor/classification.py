"""Classification policy engine: may these bytes leave the connector for this backend?  (see CLASSIFICATION.md)

Labels (ordered): PUBLIC < INTERNAL < CONFIDENTIAL < RESTRICTED. A document with NO label is treated as
CONFIDENTIAL (fail closed). Backend trust zones: LOCAL < PRIVATE_NETWORK < EXTERNAL.

Default matrix (the engine's floor):

    PUBLIC        any allowlisted backend
    INTERNAL      LOCAL or PRIVATE_NETWORK backends only
    CONFIDENTIAL  LOCAL or PRIVATE_NETWORK backends AND a human approval token bound to (tenant, document, document
                  sha256, backend)
    RESTRICTED    never sent

Preconditions for ANY transmission: the backend is on the tenant's explicit allowlist with a declared trust zone.
A tenant may override the matrix only toward STRICTER (fewer zones, approval added, label raised); a LOOSER override
is accepted only inside a policy signed with HMAC-SHA256 under ``DOCEDITOR_POLICY_SIGNING_KEY``. Even a signed
policy cannot send RESTRICTED unless it says ``allow_restricted: true`` explicitly. Every decision, allowed or
refused, produces a receipt dict for the immutable ledger.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from typing import Mapping, Optional

from .errors import ConfigError, PolicyDenied

LABELS = ("PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED")
ZONES = ("LOCAL", "PRIVATE_NETWORK", "EXTERNAL")
UNLABELED_AS = "CONFIDENTIAL"
ENV_POLICY_KEY = "DOCEDITOR_POLICY_SIGNING_KEY"
TRANSMIT_CAPABILITY = "document.transmit"
OPERATOR_NAME = "document-editor"


@dataclass(frozen=True)
class Rule:
    zones: frozenset            # zones a backend may be in
    needs_approval: bool = False
    never: bool = False

    def stricter_or_equal_to(self, base: "Rule") -> bool:
        if base.never and not self.never:
            return False
        if self.never:
            return True
        return self.zones <= base.zones and (self.needs_approval or not base.needs_approval)


DEFAULT_MATRIX: Mapping[str, Rule] = {
    "PUBLIC": Rule(frozenset(ZONES)),
    "INTERNAL": Rule(frozenset({"LOCAL", "PRIVATE_NETWORK"})),
    "CONFIDENTIAL": Rule(frozenset({"LOCAL", "PRIVATE_NETWORK"}), needs_approval=True),
    "RESTRICTED": Rule(frozenset(), never=True),
}


def normalize_label(label: Optional[str]) -> Optional[str]:
    if label is None:
        return None
    s = str(label).strip().upper()
    return s if s in LABELS else None      # an unrecognised label is treated as UNLABELED (-> CONFIDENTIAL)


def effective_label(label: Optional[str]) -> str:
    return normalize_label(label) or UNLABELED_AS


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def sign_policy(payload: dict, key: "str | bytes") -> str:
    k = key.encode() if isinstance(key, str) else key
    return hmac.new(k, _canon(payload), hashlib.sha256).hexdigest()


MIN_POLICY_KEY_LEN = 32


def verify_policy(payload: dict, signature: str, key: "str | bytes | None" = None, *, tenant: str,
                  now: Optional[float] = None) -> bool:
    key = key if key is not None else os.environ.get(ENV_POLICY_KEY, "")
    if not key or len(key) < MIN_POLICY_KEY_LEN or not isinstance(signature, str) or payload.get("tenant") != tenant:
        return False
    exp = payload.get("expires")
    if not isinstance(exp, (int, float)) or exp <= (time.time() if now is None else now):
        return False
    return hmac.compare_digest(sign_policy(payload, key), signature)


def _parse_rule(label: str, raw: Mapping) -> Rule:
    zones = raw.get("zones", [])
    bad = [z for z in zones if z not in ZONES]
    if bad:
        raise ConfigError(f"policy.matrix.{label}: unknown zone(s) {bad}")
    return Rule(frozenset(zones), bool(raw.get("needs_approval", False)), bool(raw.get("never", False)))


@dataclass(frozen=True)
class TenantPolicy:
    tenant: str
    backends: Mapping[str, str]                       # allowlisted backend id -> declared trust zone
    matrix: Mapping[str, Rule] = field(default_factory=lambda: dict(DEFAULT_MATRIX))
    default_label: str = UNLABELED_AS
    signed: bool = False

    @staticmethod
    def build(tenant: str, backends: Mapping[str, str], policy: Optional[Mapping] = None,
              signed_policy: Optional[Mapping] = None, signing_key: "str | bytes | None" = None,
              now: Optional[float] = None) -> "TenantPolicy":
        for b, z in backends.items():
            if z not in ZONES:
                raise ConfigError(f"tenant {tenant}: backend {b!r} has unknown trust zone {z!r}")
        matrix = dict(DEFAULT_MATRIX)
        default_label = UNLABELED_AS
        signed = False
        allow_restricted = False

        def apply(raw: Mapping, loosen_ok: bool):
            nonlocal default_label
            for label, r in (raw.get("matrix") or {}).items():
                if label not in LABELS:
                    raise ConfigError(f"policy.matrix: unknown label {label!r}")
                rule = _parse_rule(label, r)
                if not loosen_ok and not rule.stricter_or_equal_to(DEFAULT_MATRIX[label]):
                    raise ConfigError(f"policy.matrix.{label} is LOOSER than the default; only a signed policy may loosen")
                if label == "RESTRICTED" and not rule.never and not allow_restricted:
                    raise ConfigError("policy.matrix.RESTRICTED: sending RESTRICTED needs allow_restricted in a signed policy")
                matrix[label] = rule
            dl = raw.get("default_label")
            if dl is not None:
                dl = normalize_label(dl)
                if dl is None:
                    raise ConfigError("policy.default_label must be one of " + ", ".join(LABELS))
                if not loosen_ok and LABELS.index(dl) < LABELS.index(UNLABELED_AS):
                    raise ConfigError("policy.default_label may not be looser than CONFIDENTIAL without a signed policy")
                default_label = dl

        if policy:
            apply(policy, loosen_ok=False)
        if signed_policy:
            k = signing_key if signing_key is not None else os.environ.get(ENV_POLICY_KEY, "")
            if len(k) < MIN_POLICY_KEY_LEN:
                raise ConfigError(f"{ENV_POLICY_KEY} must be at least {MIN_POLICY_KEY_LEN} characters")
            payload, sig = signed_policy.get("payload"), signed_policy.get("signature")
            if not isinstance(payload, Mapping) or not verify_policy(dict(payload), sig, signing_key, tenant=tenant,
                                                                      now=now):
                raise ConfigError("signed policy failed verification (signature, tenant or expiry)")
            allow_restricted = bool(payload.get("allow_restricted", False))
            apply(payload, loosen_ok=True)
            signed = True
        return TenantPolicy(tenant, dict(backends), matrix, default_label, signed)


@dataclass
class TransmitDecision:
    allowed: bool
    reason: str
    tenant: str
    doc_id: str
    doc_sha256: str
    backend: str
    zone: str
    label: Optional[str]
    effective_label: str
    needs_approval: bool = False
    approved_by: str = ""

    def receipt(self) -> dict:
        return {"kind": "transmit_decision", "allowed": self.allowed, "reason": self.reason,
                "tenant": self.tenant, "doc_id": self.doc_id, "doc_sha256": self.doc_sha256,
                "backend": self.backend, "zone": self.zone, "label": self.label,
                "effective_label": self.effective_label, "needs_approval": self.needs_approval,
                "approved_by": self.approved_by}


def transmit_inputs(tenant: str, doc_id: str, doc_sha256: str, backend: str) -> dict:
    """The inputs an approver's token is bound to. Mint with ``mint_approval_token(operator='document-editor',
    capability='document.transmit', inputs=transmit_inputs(...), marker={...})``."""
    return {"tenant": tenant, "doc_id": doc_id, "doc_sha256": doc_sha256, "backend": backend}


def decide_transmit(policy: TenantPolicy, *, doc_id: str, doc_sha256: str, label: Optional[str], backend: str,
                    approval_token: Optional[str] = None, replay_guard=None,
                    now: Optional[float] = None) -> TransmitDecision:
    """Pure decision. Does not raise for a refusal: it returns ``allowed=False`` with the reason (the caller
    receipts it and raises ``PolicyDenied``)."""
    eff = (normalize_label(label) or policy.default_label)
    zone = policy.backends.get(backend, "")
    base = dict(tenant=policy.tenant, doc_id=doc_id, doc_sha256=doc_sha256, backend=backend, zone=zone, label=label,
                effective_label=eff)

    def deny(reason: str, needs: bool = False) -> TransmitDecision:
        return TransmitDecision(False, reason, needs_approval=needs, **base)

    if not zone:
        return deny(f"backend {backend!r} is not on the tenant allowlist")
    rule = policy.matrix[eff]
    if rule.never:
        return deny(f"{eff} documents are never sent to an external editor")
    if zone not in rule.zones:
        return deny(f"{eff} documents may not go to a {zone} backend")
    if rule.needs_approval:
        from agentic_os.mission.approval import verify_approval_token  # noqa: PLC0415
        marker = verify_approval_token(None, approval_token, operator=OPERATOR_NAME, capability=TRANSMIT_CAPABILITY,
                                       inputs=transmit_inputs(policy.tenant, doc_id, doc_sha256, backend),
                                       now=now, replay_guard=replay_guard)
        if marker is None:
            return deny(f"{eff} documents need a valid per-document approval token bound to this document hash "
                        f"and backend", needs=True)
        return TransmitDecision(True, "allowed with human approval", needs_approval=True,
                                approved_by=str(marker.get("by", "")), **base)
    return TransmitDecision(True, "allowed by policy", **base)


def require_transmit(decision: TransmitDecision) -> TransmitDecision:
    if not decision.allowed:
        raise PolicyDenied(decision.reason, needs_approval=decision.needs_approval,
                           effective_label=decision.effective_label)
    return decision

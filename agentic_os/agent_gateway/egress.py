"""Phase 4 — the data-egress enforcement engine (plan §6).

The external agent is outside the trust boundary, so every response passes an egress policy before
it leaves. The Phase 0 pipeline already *calls* an :class:`EgressPolicy` and records its decision;
this module replaces the permissive open-core default with a real classifier: per **data class**,
a rule says ALLOW / REDACT / SUMMARIZE / TOKENIZE / DENY / REQUIRE_APPROVAL, and a rule can be
*lifted* for a principal that carries a specific egress scope (so an explicitly-authorized caller
can receive PII while a default one cannot). The strictest matching action wins; the fired rule ids
are returned so they land in the audit + EXPLAIN.

Redaction/tokenization walk the output and mask sensitive-keyed fields and secret/PII-shaped string
values, so REDACT returns a safe object rather than dropping the whole response. DENY withholds it
entirely; REQUIRE_APPROVAL withholds it pending an export approval (the pipeline surfaces both).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Tuple

from .contracts import CapabilityManifest, DataClass, EgressAction, GatewayPrincipal

# field names whose values are always masked on REDACT/TOKENIZE, regardless of shape
SENSITIVE_KEYS = frozenset({
    "token", "access_token", "refresh_token", "id_token", "secret", "client_secret", "password",
    "passwd", "api_key", "apikey", "authorization", "auth", "credential", "credentials",
    "private_key", "ssn", "card", "card_number", "cvv"})

# secret/PII-shaped string values masked on REDACT
_PATTERNS = (
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),                        # email
    re.compile(r"\b(?:sk|rk|pk|xox[baprs])[-_][A-Za-z0-9]{8,}\b"), # provider secret keys / slack
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{8,}\b", re.I),          # bearer tokens
    re.compile(r"\beyJ[A-Za-z0-9._-]{10,}\b"),                     # JWT-ish
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),                           # long hex secrets
    re.compile(r"\b(?:\d[ -]*?){13,19}\b"),                        # card-like digit runs
)
_MASK = "«redacted»"


def _redact_str(s: str) -> str:
    for pat in _PATTERNS:
        s = pat.sub(_MASK, s)
    return s


def redact(value):
    """Mask sensitive-keyed fields and secret/PII-shaped strings, preserving structure."""
    if isinstance(value, dict):
        return {k: (_MASK if str(k).lower() in SENSITIVE_KEYS else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _redact_str(value)
    return value


def _tok(v) -> str:
    return "tok_" + hashlib.sha256(repr(v).encode()).hexdigest()[:12]


def tokenize(value):
    """Replace sensitive-keyed values with stable opaque tokens (referential, not readable)."""
    if isinstance(value, dict):
        return {k: (_tok(v) if str(k).lower() in SENSITIVE_KEYS else tokenize(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [tokenize(v) for v in value]
    return value


def summarize(value):
    """Withhold the content but keep a shape-preserving summary."""
    if isinstance(value, (list, tuple)):
        return {"summary": f"{len(value)} item(s) withheld by egress policy", "count": len(value)}
    if isinstance(value, dict):
        return {"summary": "content withheld by egress policy", "fields": sorted(map(str, value))}
    return _MASK


# strictness ranking — the most restrictive matching action wins
_RANK = {EgressAction.ALLOW: 0, EgressAction.TOKENIZE: 1, EgressAction.SUMMARIZE: 2,
         EgressAction.REDACT: 3, EgressAction.REQUIRE_APPROVAL: 4, EgressAction.DENY: 5}


@dataclass(frozen=True)
class EgressRule:
    data_class: DataClass
    action: EgressAction
    unless_scope: str = ""          # a principal carrying this scope lifts the rule to ALLOW
    rule_id: str = ""

    def resolved(self, principal: GatewayPrincipal) -> Tuple[EgressAction, str]:
        if self.unless_scope and self.unless_scope in principal.scopes:
            return EgressAction.ALLOW, ""
        return self.action, (self.rule_id or f"egress:{self.data_class.value}:{self.action.value}")


# Sensible open-core defaults: secrets never leave; PII/customer rows are redacted unless the
# caller holds an explicit egress scope; financial data needs an export approval.
DEFAULT_EGRESS_RULES: Tuple[EgressRule, ...] = (
    EgressRule(DataClass.SECRET, EgressAction.DENY, rule_id="egress:secret:deny"),
    EgressRule(DataClass.PII, EgressAction.REDACT, unless_scope="egress:pii", rule_id="egress:pii:redact"),
    EgressRule(DataClass.CUSTOMER_CONTENT, EgressAction.REDACT, unless_scope="egress:customer",
               rule_id="egress:customer:redact"),
    EgressRule(DataClass.FINANCIAL, EgressAction.REQUIRE_APPROVAL, unless_scope="egress:financial",
               rule_id="egress:financial:approve"),
    # PUBLIC / INTERNAL have no rule ⇒ ALLOW (e.g. aggregates tagged INTERNAL pass while
    # CUSTOMER_CONTENT rows are redacted/denied).
)


@dataclass
class PolicyEgressEngine:
    """A real :class:`EgressPolicy`: classify the output by its data classes and enforce the
    strictest matching rule (scope-lifted per principal)."""

    rules: Tuple[EgressRule, ...] = field(default_factory=lambda: DEFAULT_EGRESS_RULES)

    def _by_class(self):
        return {r.data_class: r for r in self.rules}

    def decide(self, output, data_classes, principal, manifest: CapabilityManifest):
        by_class = self._by_class()
        chosen, fired = EgressAction.ALLOW, []
        for dc in data_classes or ():
            rule = by_class.get(dc)
            if rule is None:
                continue
            action, rid = rule.resolved(principal)
            if action is EgressAction.ALLOW:
                continue
            fired.append(rid)
            if _RANK[action] > _RANK[chosen]:
                chosen = action
        rule_ids = tuple(fired)

        if chosen is EgressAction.ALLOW:
            return EgressAction.ALLOW, output, ()
        if chosen is EgressAction.DENY:
            return EgressAction.DENY, None, rule_ids
        if chosen is EgressAction.REQUIRE_APPROVAL:
            return EgressAction.REQUIRE_APPROVAL, None, rule_ids
        if chosen is EgressAction.REDACT:
            return EgressAction.REDACT, redact(output), rule_ids
        if chosen is EgressAction.TOKENIZE:
            return EgressAction.TOKENIZE, tokenize(output), rule_ids
        if chosen is EgressAction.SUMMARIZE:
            return EgressAction.SUMMARIZE, summarize(output), rule_ids
        return EgressAction.ALLOW, output, ()      # unreachable; fail open only to ALLOW-shaped

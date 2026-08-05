"""mission-policy/v1 — policy as a first-class, versioned, digest-pinned runtime object.

Today a mission's governance is *implicit*: `approval:<scope>` tokens in `constraints`, permission
`grants`, a budget and an autonomy mode, all evaluated at runtime with nothing carried onto the record.
This module elevates that into a **named, versioned `MissionPolicy`** — the single authority for a
mission — so the *exact policy that was evaluated* can be pinned onto each execution (`policy_digest`)
and replayed, exactly the way the runtime already pins parser identity, registry/snapshot digests and
contract versions.

The mental model is borrowed from resource-governance systems (an org declares named policies; deny
always wins; every block carries a human-readable reason), but the *subject* is different: this governs
**decisions** (may this mission spend $5? call this model? execute automatically? merge? publish?
proceed without verification?), not sandbox resources.

Two things the implicit model could not express and this one can:
  1. a hard **deny** (a capability the mission may never take, not merely gate), and
  2. an **actionable** block — a `reason` and a `suggested_action` on every deny/gate.

Backward compatible: a mission with no `MissionPolicy` keeps using `constraints`/`grants` unchanged.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256

#: The version is the contract this object conforms to (distinct from a MissionPolicy's own `version`,
#: which is the org's revision of *that* policy). Semantics + canonical serialization live here.
CONTRACT_VERSION = "mission-policy/v1"


class Effect(str, Enum):
    """A rule's effect. Precedence is explicit and total: DENY > REQUIRE_APPROVAL > ALLOW."""
    DENY = "deny"                        # hard-blocked — the mission may never take this action
    REQUIRE_APPROVAL = "require_approval"  # gated on a human (same gate the plane already builds)
    ALLOW = "allow"                     # explicitly permitted (overrides a broader gate below it)

    @property
    def rank(self) -> int:
        return {"deny": 2, "require_approval": 1, "allow": 0}[self.value]


# The selector vocabulary a rule matches against — a superset of the legacy `approval:<scope>` tokens
# so existing missions map 1:1. `cap:<name>` matches one capability; the rest are predicates on the node.
_SELECTORS = ("*", "side_effecting", "irreversible", "regulatory", "over_budget")


@dataclass(frozen=True)
class PolicyRule:
    """One allow/deny/gate rule. `match` is a selector (see `_SELECTORS`) or `cap:<capability>`.
    `max_usd` (when set) turns the rule into a spend predicate. `reason`/`suggested_action` are the
    human-readable, *actionable* message shown when this rule blocks — governance you can act on."""
    id: str
    effect: Effect = Effect.REQUIRE_APPROVAL
    match: str = "*"
    max_usd: float | None = None
    reason: str = ""
    suggested_action: str = ""

    def matches(self, ctx: "NodeContext") -> bool:
        m = self.match
        selector = (
            m == "*"
            or (m == "side_effecting" and ctx.side_effecting)
            or (m == "irreversible" and ctx.side_effecting and not ctx.reversible)
            or (m == "regulatory" and ctx.regulatory)
            or (m == "over_budget" and ctx.over_budget)
            or (m.startswith("cap:") and m[4:] == ctx.capability)
        )
        # A `max_usd` turns the rule into a spend predicate: it fires only when the selector matches
        # AND the action's cost exceeds the cap (so a broad `*` spend rule doesn't gate cheap actions).
        if self.max_usd is not None:
            return selector and ctx.cost_usd > self.max_usd
        return selector


@dataclass(frozen=True)
class NodeContext:
    """The decision inputs a policy is evaluated against — assembled by the policy plane from a node."""
    capability: str = ""
    side_effecting: bool = False
    reversible: bool = True
    regulatory: bool = False
    cost_usd: float = 0.0
    over_budget: bool = False
    permissioned: bool = True


@dataclass(frozen=True)
class PolicyOutcome:
    """The EXPLAIN of a single evaluation: the effect, the deciding rule, its actionable message, and
    the pinned identity of the policy that produced it. `trace` lists every rule that matched."""
    effect: Effect
    policy_ref: str                        # "finance-prod@12"
    policy_digest: str                     # "sha256:…" — the content identity that was evaluated
    matched_rule: str = ""                 # the deciding rule's id (highest-precedence match)
    reason: str = ""
    suggested_action: str = ""
    trace: tuple[dict, ...] = ()           # [{rule, effect, matched}] for every rule, in order

    @property
    def blocked(self) -> bool:
        return self.effect is Effect.DENY

    @property
    def gated(self) -> bool:
        return self.effect is Effect.REQUIRE_APPROVAL

    def message(self, support_message: str = "") -> str:
        """A Docker-style, actionable block message: what happened, why, what to do."""
        head = "Denied." if self.blocked else ("Approval required." if self.gated else "Allowed.")
        parts = [head]
        if self.reason:
            parts.append(f"\nReason:\n{self.reason}")
        if self.suggested_action:
            parts.append(f"\nSuggested action:\n{self.suggested_action}")
        if support_message:
            parts.append(f"\n{support_message}")
        parts.append(f"\n[policy {self.policy_ref} · rule {self.matched_rule or '—'} · {self.policy_digest}]")
        return "".join(parts)


@dataclass(frozen=True)
class MissionPolicy:
    """A named, versioned governance policy — the single authority for a mission it is attached to.

    Ordered `rules` are evaluated with **explicit precedence (deny wins)**: the highest-ranked *matching*
    rule decides. `grants`/`budget_usd` fold the mission's permission + spend envelope into the same
    object so one artifact — with one `digest()` — describes the whole governance posture.
    """
    id: str
    version: str = "1"
    scope: str = "mission"                 # mission | team | organization
    rules: tuple[PolicyRule, ...] = ()
    grants: tuple[str, ...] = ()           # permission grants the mission runs under (deny-by-default)
    budget_usd: float = 0.0
    support_message: str = ""              # org contact line appended to every block (Docker's idea)

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def digest(self) -> str:
        """Content-addressed identity — the thing pinned onto each execution for replay. Canonical
        JSON (sorted keys, no whitespace) hashed like every other digest in the runtime."""
        canonical = json.dumps(_jsonable(self), sort_keys=True, separators=(",", ":"))
        return "sha256:" + sha256(canonical.encode()).hexdigest()[:16]

    def evaluate(self, ctx: NodeContext) -> PolicyOutcome:
        """Deny-wins evaluation → the deciding `PolicyOutcome` (with a full per-rule trace)."""
        digest = self.digest()
        trace: list[dict] = []
        best: PolicyRule | None = None
        for r in self.rules:
            hit = r.matches(ctx)
            trace.append({"rule": r.id, "effect": r.effect.value, "matched": hit})
            if hit and (best is None or r.effect.rank > best.effect.rank):
                best = r
        if best is None:
            return PolicyOutcome(Effect.ALLOW, self.ref, digest, trace=tuple(trace))
        return PolicyOutcome(best.effect, self.ref, digest, matched_rule=best.id,
                             reason=best.reason, suggested_action=best.suggested_action,
                             trace=tuple(trace))


def _jsonable(obj):  # local, dependency-free canonicaliser (avoids importing types.py)
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "__dataclass_fields__"):
        from dataclasses import asdict
        return _jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


def from_constraints(constraints: list[str], grants: list[str] | None = None,
                     *, id: str = "mission-local", version: str = "0",
                     budget_usd: float = 0.0) -> MissionPolicy:
    """Lift the legacy implicit governance (the `approval:<scope>` tokens + grants) into an explicit,
    digest-able MissionPolicy — so even ad-hoc missions get a pinnable identity without a rewrite."""
    rules: list[PolicyRule] = []
    for c in constraints or []:
        if not isinstance(c, str) or not c.startswith("approval:"):
            continue
        scope = c.split(":", 1)[1]
        match = {"all": "*", "side_effecting": "side_effecting",
                 "irreversible": "irreversible"}.get(scope, scope if scope.startswith("cap:") else None)
        if match is None:
            continue
        rules.append(PolicyRule(id=f"legacy_{scope.replace(':', '_')}", effect=Effect.REQUIRE_APPROVAL,
                                match=match, reason=f"mission constraint approval:{scope}"))
    return MissionPolicy(id=id, version=version, rules=tuple(rules),
                         grants=tuple(grants or []), budget_usd=budget_usd)


# ── contract self-check (mirrors merge.py's `__main__` conformance runner) ──────────────────────────
if __name__ == "__main__":
    pol = MissionPolicy(
        id="finance-prod", version="12", scope="organization",
        support_message="Contact Platform Engineering if this workflow requires external write access.",
        grants=("read:market", "read:portfolio"),
        rules=(
            PolicyRule("external_write_requires_human", Effect.REQUIRE_APPROVAL, "side_effecting",
                       reason="External writes are gated in finance-prod.",
                       suggested_action="Approve in the cockpit, or run in shadow mode."),
            PolicyRule("no_irreversible", Effect.DENY, "irreversible",
                       reason="Irreversible actions are not permitted in finance-prod.",
                       suggested_action="Use a reversible capability or request a policy exception."),
            PolicyRule("spend_cap", Effect.REQUIRE_APPROVAL, "*", max_usd=5.0,
                       reason="Maximum external API spend reached.",
                       suggested_action="Increase budget or approve the escalation."),
        ),
    )
    checks = []
    d1 = pol.digest()
    checks.append(("digest stable", d1 == pol.digest()))
    checks.append(("digest content-addressed", d1 != MissionPolicy(id="x").digest()))
    o_rev = pol.evaluate(NodeContext(capability="billing.charge", side_effecting=True, reversible=True, cost_usd=1.0))
    checks.append(("side-effecting → gate", o_rev.gated and o_rev.matched_rule == "external_write_requires_human"))
    o_irr = pol.evaluate(NodeContext(capability="infra.destroy", side_effecting=True, reversible=False))
    checks.append(("irreversible → DENY wins", o_irr.blocked and o_irr.matched_rule == "no_irreversible"))
    o_spend = pol.evaluate(NodeContext(capability="llm.call", side_effecting=False, cost_usd=9.0))
    checks.append(("over-cap spend → gate", o_spend.gated and o_spend.matched_rule == "spend_cap"))
    o_ok = pol.evaluate(NodeContext(capability="read.market", side_effecting=False, cost_usd=0.1))
    checks.append(("benign → allow", o_ok.effect is Effect.ALLOW))
    checks.append(("actionable message", "Suggested action" in o_irr.message(pol.support_message)))
    checks.append(("from_constraints lifts tokens",
                   len(from_constraints(["approval:side_effecting", "approval:cap:x"]).rules) == 2))
    passed = sum(1 for _, ok in checks if ok)
    for name, ok in checks:
        print(f"  {'✓' if ok else '✗'} {name}")
    print(f"RESULT {passed}/{len(checks)}  (policy contract {CONTRACT_VERSION})")
    import sys
    sys.exit(0 if passed == len(checks) else 1)

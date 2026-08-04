"""First-class ``merge()`` — bounded hierarchical integration with structured ledgers.

This elevates context *compaction* (:mod:`agentic_os.mission.compaction`) into a governed integration
*operator*. Python is the executable specification; this mirrors the Kotlin v9 line
(``redevops-runtime-kotlin``) at field parity, and a Go port tracks both.

Four properties make it an operator, not a compactor — the first three are the deterministic public
core (verifiable in ``_demo`` below); the fourth is a *hook*:

1. **verification gates propagation** — a merge PASSES only if every inherited contract token survives,
   no inherited fact regressed, and no HIGH-severity conflict is unresolved; :pyattr:`MergeResult.propagatable`
   is load-bearing, so a merge that silently drops a fact fails and is not integrated upward;
2. **semantic conflicts** — signature-aware incompatibility (same symbol, different signature) + cross-source
   contradiction (same keyed fact, different value), not just duplicate names;
3. **planner-selected topology** — :class:`MergeTopologyPlanner` chooses the strategy deterministically
   from context size, coupling and risk (feasibility-first: DIRECT is excluded above the window);
4. **learning** *(hook — enterprise)* — an optional :class:`MergeAdvisor` biases the choice from observed
   verification pass-rates. The public planner accepts one but ships **no** learner; a production learner
   (contextual bandit over topologies) is an enterprise overlay injected here. With no advisor the planner
   is fully deterministic (``select`` == ``rule_choice``).

Contract version: ``merge/v9`` — the version is the **whitepaper that canonically specifies** the operator
(v9, the Kotlin/JVM-for-Spark line where ``merge()`` was introduced). Compatibility is additive within that
lineage (new enum members, new optional fields, new ledger kinds); a later whitepaper that redefines the
merge semantics or the wire shape of a :class:`LedgerBundle` bumps the version. The ``_demo`` self-test is
the executable golden fixture.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from typing import Callable, Protocol

#: Semantic version of the public merge contract. See the module docstring for the compatibility rule.
CONTRACT_VERSION = "merge/v9"


# ════════════════════════════ contracts ════════════════════════════
class Severity(str, Enum):
    INFO = "info"; LOW = "low"; MEDIUM = "medium"; HIGH = "high"; CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return ["info", "low", "medium", "high", "critical"].index(self.value)


class MergeScope(str, Enum):
    MODULE = "module"; SUBSYSTEM = "subsystem"; ARCHITECTURE = "architecture"; RELEASE = "release"


class MergeStrategy(str, Enum):
    DIRECT = "direct"; CONTRACT_FIRST = "contract_first"; HIERARCHICAL = "hierarchical"
    EVIDENCE_FIRST = "evidence_first"; SEQUENTIAL_VERIFIED = "sequential_verified"; HUMAN_GATED = "human_gated"


class LedgerKind(str, Enum):
    ENTITY = "entity"; SCHEMA = "schema"; LINEAGE = "lineage"; QUALITY = "quality"
    DECISION = "decision"; RISK = "risk"; EVIDENCE = "evidence"; CONFLICT = "conflict"


@dataclass(frozen=True)
class LedgerFact:
    key: str
    value: str
    source: str = ""


@dataclass
class Ledger:
    kind: LedgerKind
    facts: dict[str, LedgerFact] = field(default_factory=dict)

    def keys(self) -> set[str]:
        return set(self.facts.keys())


@dataclass
class LedgerBundle:
    """Structured facts merged by deterministic union; same key / different value → a CONFLICT fact."""
    ledgers: dict[LedgerKind, Ledger] = field(default_factory=dict)

    def ledger(self, kind: LedgerKind) -> Ledger:
        return self.ledgers.get(kind) or Ledger(kind)

    def with_fact(self, kind: LedgerKind, key: str, value: str | None = None, source: str = "") -> "LedgerBundle":
        facts = dict(self.ledger(kind).facts)
        facts[key] = LedgerFact(key, key if value is None else value, source)
        led = dict(self.ledgers); led[kind] = Ledger(kind, facts)
        return LedgerBundle(led)

    def merge(self, other: "LedgerBundle") -> "LedgerBundle":
        out: dict[LedgerKind, Ledger] = {}
        conflicts = dict(self.ledger(LedgerKind.CONFLICT).facts)
        conflicts.update(other.ledger(LedgerKind.CONFLICT).facts)
        for kind in LedgerKind:
            if kind is LedgerKind.CONFLICT:
                continue
            facts = dict(self.ledger(kind).facts)
            for k, bf in other.ledger(kind).facts.items():
                af = facts.get(k)
                if af is not None and af.value != bf.value:
                    ck = f"{kind.value}:{k}"
                    conflicts[ck] = LedgerFact(ck, f"'{af.value}' ({af.source}) vs '{bf.value}' ({bf.source})", "merge")
                if af is None:
                    facts[k] = bf
            if facts:
                out[kind] = Ledger(kind, facts)
        if conflicts:
            out[LedgerKind.CONFLICT] = Ledger(LedgerKind.CONFLICT, conflicts)
        return LedgerBundle(out)

    @staticmethod
    def of(kind: LedgerKind, keys, source: str = "") -> "LedgerBundle":
        b = LedgerBundle()
        for k in keys:
            b = b.with_fact(kind, k, k, source)
        return b


@dataclass(frozen=True)
class Artifact:
    id: str
    content_hash: str
    kind: str = "artifact"
    produced_by: str = ""


class ArtifactStore:
    def __init__(self) -> None:
        self._c: dict[str, str] = {}

    def get(self, aid: str) -> str | None:
        return self._c.get(aid)

    def put(self, kind: str, content: str, produced_by: str = "") -> Artifact:
        h = sha256(content.encode()).hexdigest()
        aid = f"art-{kind}-{h[:12]}"
        self._c[aid] = content
        return Artifact(aid, h, kind, produced_by)


@dataclass
class MergeConflict:
    kind: str
    detail: str
    artifacts: list[str] = field(default_factory=list)
    severity: Severity = Severity.MEDIUM


@dataclass
class VerificationCheck:
    name: str
    passed: bool
    detail: str = ""
    severity: Severity = Severity.MEDIUM


@dataclass
class VerificationResult:
    subject_id: str
    outcome: str                          # "pass" | "fail"
    checks: list[VerificationCheck] = field(default_factory=list)
    verifier: str = "merge-verify"


@dataclass
class MergeInput:
    scope: MergeScope
    artifacts: list[Artifact]
    contracts: list[str] = field(default_factory=list)
    strategy: MergeStrategy = MergeStrategy.DIRECT
    inherited_ledgers: LedgerBundle = field(default_factory=LedgerBundle)


@dataclass
class MergeResult:
    scope: MergeScope
    merged: Artifact
    conflicts: list[MergeConflict]
    verification: VerificationResult
    ledgers: LedgerBundle
    provenance: list[str]

    @property
    def propagatable(self) -> bool:
        no_high = all(c.severity.rank < Severity.HIGH.rank for c in self.conflicts)
        return no_high and self.verification.outcome == "pass"


# a semantic merger is Callable[[list[str], list[str], MergeScope], str]
def deterministic_merger(parts: list[str], contracts: list[str], scope: MergeScope) -> str:
    header = ("// contracts: " + ",".join(contracts) + "\n") if contracts else ""
    return header + "\n\n".join(parts)


# ════════════════════════════ the operator ════════════════════════════
_DECL = re.compile(r"^(fun|class|object|interface|val|var|def)\s+([A-Za-z_]\w*)")


class HierarchicalMerge:
    """One bounded merge at a scope: detect conflicts, integrate via a semantic merger, thread structured
    facts through ledgers, and verify against the contracts — the unit :class:`MergeMission` composes."""

    def __init__(self, store: ArtifactStore, merger: Callable = deterministic_merger,
                 fact_extractors: dict[LedgerKind, Callable[[str], set]] | None = None,
                 keyed_extractors: dict[LedgerKind, Callable[[str], dict]] | None = None) -> None:
        self.store = store
        self.merger = merger
        self.fact_extractors = fact_extractors or {}
        self.keyed_extractors = keyed_extractors or {}

    def merge(self, inp: MergeInput) -> MergeResult:
        parts = [self.store.get(a.id) or "" for a in inp.artifacts]
        conflicts = self._structural(inp, parts) + self._contradictions(inp, parts)
        content = self.merger(parts, inp.contracts, inp.scope)
        merged = self.store.put(f"merge-{inp.scope.value}", content, "merge()")
        ledgers = self._ledgers(inp, parts, conflicts)
        verification = self._verify(inp, merged, content, conflicts, ledgers)
        provenance = [a.content_hash for a in inp.artifacts] + [merged.content_hash]
        return MergeResult(inp.scope, merged, conflicts, verification, ledgers, provenance)

    # (1) real, contract-checking verification — makes propagatable load-bearing
    def _verify(self, inp, merged, content, conflicts, ledgers) -> VerificationResult:
        checks = [VerificationCheck("content-addressed", bool(merged.content_hash)),
                  VerificationCheck("non-empty", bool(merged.id))]
        for c in inp.contracts:
            ok = c in content
            checks.append(VerificationCheck(f"contract-preserved:{c}", ok, "" if ok else f"contract '{c}' dropped", Severity.HIGH))
        lost = []
        for kind in LedgerKind:
            if kind is LedgerKind.CONFLICT:
                continue
            lost += [f"{kind.value}:{k}" for k in (inp.inherited_ledgers.ledger(kind).keys() - ledgers.ledger(kind).keys())]
        checks.append(VerificationCheck("no-fact-regression", not lost, "" if not lost else f"{len(lost)} fact(s) lost: {lost[:3]}", Severity.HIGH))
        high = sum(1 for c in conflicts if c.severity.rank >= Severity.HIGH.rank)
        checks.append(VerificationCheck("no-unresolved-high-conflict", high == 0, "" if high == 0 else f"{high} high-severity conflict(s)", Severity.HIGH))
        outcome = "pass" if all(c.passed for c in checks) else "fail"
        return VerificationResult(merged.id, outcome, checks)

    def _ledgers(self, inp, parts, conflicts) -> LedgerBundle:
        bundle = inp.inherited_ledgers
        for i, a in enumerate(inp.artifacts):
            t = parts[i] if i < len(parts) else ""
            for kind, ex in self.fact_extractors.items():
                bundle = bundle.merge(LedgerBundle.of(kind, ex(t), a.id))
            for kind, ex in self.keyed_extractors.items():
                b = LedgerBundle()
                for k, v in ex(t).items():
                    b = b.with_fact(kind, k, v, a.id)
                bundle = bundle.merge(b)
        for c in conflicts:
            bundle = bundle.with_fact(LedgerKind.CONFLICT, f"{c.kind}:{','.join(c.artifacts)}", c.detail, "merge()")
        return bundle

    # (2a) signature-aware structural conflicts
    def _structural(self, inp, parts) -> list[MergeConflict]:
        by_name: dict[str, dict[str, list[str]]] = {}
        for i, a in enumerate(inp.artifacts):
            for name, sig in self._decls(parts[i] if i < len(parts) else ""):
                by_name.setdefault(name, {}).setdefault(sig, []).append(a.id)
        out: list[MergeConflict] = []
        for name, sigs in by_name.items():
            arts = [x for v in sigs.values() for x in v]
            if len(arts) <= 1:
                continue
            if len(sigs) > 1:
                out.append(MergeConflict("interface-incompatible", f"'{name}' declared with {len(sigs)} incompatible signatures across {len(arts)} parts", arts, Severity.HIGH))
            else:
                out.append(MergeConflict("redundant-duplicate", f"'{name}' duplicated identically in {len(arts)} parts", arts, Severity.LOW))
        merged_text = "\n".join(parts)
        for c in inp.contracts:
            if c not in merged_text:
                out.append(MergeConflict("contract-violation", f"required contract token '{c}' absent", [a.id for a in inp.artifacts], Severity.HIGH))
        return out

    # (2b) cross-source contradiction: same keyed fact, different value across parts or vs inherited
    def _contradictions(self, inp, parts) -> list[MergeConflict]:
        if not self.keyed_extractors:
            return []
        seen: dict[str, tuple[str, str]] = {}
        for kind in self.keyed_extractors:
            for k, f in inp.inherited_ledgers.ledger(kind).facts.items():
                seen[f"{kind.value}:{k}"] = (f.value, f.source)
        out: list[MergeConflict] = []
        for i, a in enumerate(inp.artifacts):
            for kind, ex in self.keyed_extractors.items():
                for k, v in ex(parts[i] if i < len(parts) else "").items():
                    kk = f"{kind.value}:{k}"
                    prev = seen.get(kk)
                    if prev is None:
                        seen[kk] = (v, a.id)
                    elif prev[0] != v:
                        out.append(MergeConflict("contradiction", f"{kk} = '{prev[0]}' ({prev[1]}) vs '{v}' ({a.id})", [prev[1], a.id], Severity.HIGH))
        return out

    def _decls(self, src: str) -> list[tuple[str, str]]:
        out = []
        for line in src.splitlines():
            t = line.strip()
            m = _DECL.match(t)
            if m:
                out.append((m.group(2), t.split("{")[0].strip()))
        return out


class MergeMission:
    """Bounded hierarchical mission: module → subsystem → architecture → release. Only *propagatable*
    (verified, conflict-free) results move upward; ledgers thread up every level. Optional planner-driven
    topology (``select_strategy``) + learning hook (``on_outcome``) — both ``None`` = legacy fixed topology."""

    def __init__(self, store: ArtifactStore, op: HierarchicalMerge | None = None,
                 select_strategy: Callable[[int, int, bool], MergeStrategy] | None = None,
                 on_outcome: Callable[[int, MergeStrategy, bool], None] | None = None) -> None:
        self.store = store
        self.op = op or HierarchicalMerge(store)
        self.select_strategy = select_strategy
        self.on_outcome = on_outcome

    def run(self, modules: dict[str, list[Artifact]], contracts: list[str]) -> list[MergeResult]:
        results: list[MergeResult] = []
        module_results = [self._merge_at(MergeScope.MODULE, arts, contracts, LedgerBundle(), MergeStrategy.HIERARCHICAL)
                          for arts in modules.values()]
        results += module_results
        inherited = LedgerBundle()
        for r in module_results:
            if r.propagatable:
                inherited = inherited.merge(r.ledgers)
        subsystem = self._merge_at(MergeScope.SUBSYSTEM, [r.merged for r in module_results if r.propagatable], contracts, inherited, MergeStrategy.HIERARCHICAL)
        results.append(subsystem); inherited = subsystem.ledgers
        arch = self._merge_at(MergeScope.ARCHITECTURE, [subsystem.merged] if subsystem.propagatable else [], contracts, inherited, MergeStrategy.HIERARCHICAL)
        results.append(arch); inherited = arch.ledgers
        release = self._merge_at(MergeScope.RELEASE, [arch.merged] if arch.propagatable else [], contracts, inherited, MergeStrategy.SEQUENTIAL_VERIFIED)
        results.append(release)
        return results

    def _merge_at(self, scope, arts, contracts, inherited, default) -> MergeResult:
        ctx = sum(len(self.store.get(a.id) or "") for a in arts) // 4
        strat = self.select_strategy(ctx, self._coupling(arts), scope is MergeScope.RELEASE) if self.select_strategy else default
        r = self.op.merge(MergeInput(scope, arts, contracts, strat, inherited))
        if self.on_outcome:
            self.on_outcome(ctx, strat, r.verification.outcome == "pass")
        return r

    def _coupling(self, arts) -> int:
        counts: dict[str, int] = {}
        for a in arts:
            names = {m.group(2) for line in (self.store.get(a.id) or "").splitlines() if (m := _DECL.match(line.strip()))}
            for n in names:
                counts[n] = counts.get(n, 0) + 1
        return sum(1 for v in counts.values() if v > 1)


# ════════════════════════════ (3) planner + (4) advisor hook ════════════════════════════
def context_band(context_tokens: int, window_tokens: int) -> str:
    if context_tokens <= int(window_tokens * 0.8):
        return "in-window"
    if context_tokens <= window_tokens * 4:
        return "moderate-overload"
    return "heavy-overload"


@dataclass
class MergeSignals:
    context_tokens: int
    window_tokens: int
    coupling_refs: int = 0
    risk_high: bool = False
    decentralized: bool = False


class MergeAdvisor(Protocol):
    """The learning hook. An enterprise overlay implements this (e.g. a contextual bandit over
    topologies) and registers it (below); the public runtime ships none, so without an advisor the
    planner is fully deterministic. ``advise`` returns a pass-rate per strategy value for the band."""

    def advise(self, band: str) -> dict[str, float]: ...


# ── overlay registration hook ──
# The one seam an enterprise overlay uses to attach the learned router. Public ships NO advisor, so a
# public-only deployment is fully deterministic. An overlay package (agentic-os-enterprise), when
# installed alongside, calls `set_default_advisor(MergeLearner(...))` on import — the public core never
# imports anything private; it only offers this hook. This is the pattern every extension point follows.
_DEFAULT_ADVISOR: "MergeAdvisor | None" = None


def set_default_advisor(advisor: "MergeAdvisor | None") -> None:
    """Register the process-wide default merge advisor (enterprise overlay). None restores determinism."""
    global _DEFAULT_ADVISOR
    _DEFAULT_ADVISOR = advisor


def get_default_advisor() -> "MergeAdvisor | None":
    return _DEFAULT_ADVISOR


class MergeTopologyPlanner:
    """Selects the merge strategy from signals (feasibility-first — DIRECT is impossible above the
    window). Deterministic by default; if an optional :class:`MergeAdvisor` is injected and has evidence
    for ≥2 feasible strategies at the band, its verdict refines the rule choice."""
    COUPLING_HI = 6

    def __init__(self, advisor: MergeAdvisor | None = None) -> None:
        # explicit injection wins; else the registered overlay default (None in a public-only deploy).
        self.advisor = advisor if advisor is not None else get_default_advisor()

    def feasible(self, sig: MergeSignals) -> list[MergeStrategy]:
        fits = sig.context_tokens <= int(sig.window_tokens * 0.8)
        return list(MergeStrategy) if fits else [s for s in MergeStrategy if s is not MergeStrategy.DIRECT]

    def rule_choice(self, sig: MergeSignals) -> MergeStrategy:
        feasible = set(self.feasible(sig))
        fits = MergeStrategy.DIRECT in feasible
        if fits and not sig.risk_high and sig.coupling_refs == 0:
            c = MergeStrategy.DIRECT
        elif sig.risk_high:
            c = MergeStrategy.SEQUENTIAL_VERIFIED
        elif sig.coupling_refs >= self.COUPLING_HI:
            c = MergeStrategy.CONTRACT_FIRST
        else:
            c = MergeStrategy.HIERARCHICAL
        return c if c in feasible else MergeStrategy.HIERARCHICAL

    def select(self, sig: MergeSignals) -> MergeStrategy:
        feasible = {s.value for s in self.feasible(sig)}
        band = context_band(sig.context_tokens, sig.window_tokens)
        advice = {k: v for k, v in (self.advisor.advise(band) if self.advisor else {}).items() if k in feasible}
        if len(advice) >= 2:
            return MergeStrategy(max(advice, key=advice.get))
        return self.rule_choice(sig)


# ════════════════════════════ self-test / golden fixture (deterministic core) ════════════════════════════
def _demo() -> int:
    checks: list[tuple[str, bool]] = []

    def store(*contents):
        s = ArtifactStore()
        return s, [s.put("part", c, f"p{i}") for i, c in enumerate(contents)]

    # 1. verification is load-bearing
    s, arts = store("payload carrying INVARIANT-X and more", "second part with INVARIANT-X")
    inp = MergeInput(MergeScope.MODULE, arts, contracts=["INVARIANT-X"])
    dropped = HierarchicalMerge(s, lambda p, c, sc: "consolidated summary (details omitted)").merge(inp)
    kept = HierarchicalMerge(s, deterministic_merger).merge(inp)
    checks.append(("verify FAILS when a contract is dropped", not dropped.propagatable))
    checks.append(("verify PASSES when the contract survives", kept.propagatable))

    # 2. interface incompatibility vs benign duplicate
    s1, a1 = store("def alpha(): return 1", "def alpha(x): return x")
    incompat = HierarchicalMerge(s1).merge(MergeInput(MergeScope.MODULE, a1))
    s2, a2 = store("def beta(): return 2", "def beta(): return 2")
    dup = HierarchicalMerge(s2).merge(MergeInput(MergeScope.MODULE, a2))
    checks.append(("interface-incompatible flagged HIGH + blocks", any(c.kind == "interface-incompatible" for c in incompat.conflicts) and not incompat.propagatable))
    checks.append(("identical duplicate is benign (LOW, propagates)", all(c.kind == "redundant-duplicate" for c in dup.conflicts) and dup.propagatable))

    # 3. cross-source contradiction
    rev = lambda t: {m.group(1): m.group(2) for m in re.finditer(r"(\w+)=(\d+)", t)}
    s1, a1 = store("customerA=100", "customerA=110")
    contra = HierarchicalMerge(s1, keyed_extractors={LedgerKind.QUALITY: rev}).merge(MergeInput(MergeScope.MODULE, a1))
    s2, a2 = store("customerA=100", "customerB=200")
    ok = HierarchicalMerge(s2, keyed_extractors={LedgerKind.QUALITY: rev}).merge(MergeInput(MergeScope.MODULE, a2))
    checks.append(("cross-source contradiction detected + blocks", any(c.kind == "contradiction" for c in contra.conflicts) and not contra.propagatable))
    checks.append(("no contradiction when values agree", not any(c.kind == "contradiction" for c in ok.conflicts)))

    # 4. deterministic planner selection (no advisor — the public default)
    W = 32768
    p = MergeTopologyPlanner()
    checks.append(("in-window low-coupling -> DIRECT", p.select(MergeSignals(10000, W)) is MergeStrategy.DIRECT))
    checks.append(("overload -> HIERARCHICAL (DIRECT infeasible)", p.select(MergeSignals(60000, W)) is MergeStrategy.HIERARCHICAL))
    checks.append(("high coupling -> CONTRACT_FIRST", p.select(MergeSignals(60000, W, coupling_refs=8)) is MergeStrategy.CONTRACT_FIRST))
    checks.append(("high risk -> SEQUENTIAL_VERIFIED", p.select(MergeSignals(60000, W, risk_high=True)) is MergeStrategy.SEQUENTIAL_VERIFIED))

    # 4b. the deterministic planner drives a real bounded MergeMission (module → release), no learner
    s = ArtifactStore()
    modules = {f"mod{m}": [s.put("post", f"Product Alpha{m}{i} uses Unity Catalog.", f"p{m}{i}") for i in range(3)] for m in range(3)}
    pl = MergeTopologyPlanner()
    entity = lambda t: set(re.findall(r"Product Alpha\w+|Unity Catalog", t))
    mission = MergeMission(s, HierarchicalMerge(s, fact_extractors={LedgerKind.ENTITY: entity}),
                           select_strategy=lambda ctx, cpl, risk: pl.select(MergeSignals(ctx, W, cpl, risk)))
    results = mission.run(modules, contracts=[])
    checks.append(("planner drives a real MergeMission; release propagates", bool(results) and results[-1].propagatable))

    for name, ok in checks:
        print(f"  check {name} = {ok}")
    passed = sum(1 for _, ok in checks if ok)
    print(f"RESULT {passed}/{len(checks)}  (merge contract {CONTRACT_VERSION})")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    import sys
    sys.exit(_demo())

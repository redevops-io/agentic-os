"""Context Runtime — the single resolution door for ALL context (ADR: context-runtime-context-os).

The layering is Application → **Mission Runtime → Context Runtime** → Capabilities → Models → External
Systems. The Mission Runtime is pure orchestration: it plans, gates, executes, compensates and replays,
and it **never decides** retrieval, representations, embeddings, routing or model selection. When it needs
context — to bind a capability, resolve a belief, check a policy, select a model — it states the *need*:

    ContextRuntime.resolve(ContextIntent(kind, goal, world, mission, capability, ...)) -> ContextBundle

and the Context Runtime **owns the decision**: which source, which representation, which engine, which
model. "Owns all context" means *single resolution interface* — the Mission Runtime still writes mission
and world state (it is the execution system-of-record); the Context Runtime resolves *over* those stores
plus the capability registry, policies, evidence, knowledge engines, memory, observability and external
APIs, through one optimizer.

This `LocalContextRuntime` is the dependency-light in-repo default (wraps the capability registry, the
policy plane, and belief fusion). Production injects the real Context Runtime — HippoRAG · graph · SQL ·
DuckDB · Elastic · vision · capability registry · world state · policies · evidence · mission history ·
model router — behind the SAME Protocol. Every resolution carries **Provenance**: the EXPLAIN surface for
*why this context* (which resolver, representation, score, alternatives).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

# ── context kinds — every context need the Mission Runtime has ──
BIND_CAPABILITY = "bind_capability"     # which capability satisfies this outcome/need
RESOLVE_BELIEF = "resolve_belief"       # the fused belief for a world-state key
CHECK_POLICY = "check_policy"           # the approval decision for a node
SELECT_MODEL = "select_model"           # which model answers this step
RETRIEVE_KNOWLEDGE = "retrieve_knowledge"   # knowledge, routed across engines by query shape
# recall_memory · read_observability resolve against the real CR in production.

_MIN_BIND_SCORE = 0.2  # a discovery match below this is NOT a real provider (fail closed, don't paper over)


@dataclass
class ContextIntent:
    """A statement of *need*, not a retrieval instruction. The Mission Runtime fills this in and hands
    it to the Context Runtime; it never says how to fetch, represent, embed, route or which model."""
    kind: str
    goal: str = ""
    need: str = ""
    outcome: str = ""
    prefer: str | None = None       # bind: force a specific provider (active planner enumeration)
    node: Any = None                # policy: the node to gate
    world: Any = None
    mission: Any = None
    graph: Any = None
    key: str | None = None          # belief: the world-state key
    extra: dict = field(default_factory=dict)


@dataclass
class Provenance:
    """Why this context — the EXPLAIN surface. Which resolver answered, over which representation."""
    resolver: str
    representation: str = ""        # provides · embedding · fused-belief · policy · model
    score: float | None = None
    alternatives: list = field(default_factory=list)
    reason: str = ""


@dataclass
class ContextBundle:
    value: Any
    provenance: Provenance
    candidates: list = field(default_factory=list)


class ContextRuntime(Protocol):
    """The one door. Production swaps LocalContextRuntime for the real Context Runtime — same contract."""
    def resolve(self, intent: ContextIntent) -> ContextBundle: ...


# ── knowledge retrieval: one optimizer, routed by query shape ──────────────────────────────────
class KnowledgeRetriever(Protocol):
    """A retrieval engine (pgvector · HippoRAG · graph · SQL/DuckDB · Elastic · vision). Production wires
    the real Context Runtime engines behind this; the in-repo `KeywordRetriever` is the default."""
    def retrieve(self, query: str, k: int = 5) -> list[dict]: ...


# The router IS the optimizer: no single retrieval regime wins across query shapes (temporal · multi-hop ·
# structured · visual · semantic), so pick the engine from the shape of the need. This is the seam where the
# real Context Runtime plugs in a learned/cost-based router; the default is a transparent heuristic.
_ROUTES = [
    ("graph",      ("when", "before", "after", "timeline", "over time", "history", "evolve", "trend")),        # temporal
    ("hipporag",   ("relate", "related", "connection", "how does", "multi-hop", "chain", "because",
                    "leads to", "depends on", "path from")),                                                   # multi-hop
    ("analytical", ("count", "sum", "average", "group by", "how many", "total", "top", "per", "aggregate")),   # structured
    ("vision",     ("image", "photo", "video", "frame", "picture", "visual", "diagram", "screenshot")),        # multimodal
    ("code",       ("definition", "define", "defined", "declared", "references", "reference", "callers",       # code structure
                    "function", "class", "method", "import", "symbol", "implementation", "where is")),
]


def route_representation(query: str, hint: str | None = None) -> tuple[str, str]:
    """Pick the retrieval engine/representation for a knowledge need → (engine, why). Returns the EXPLAIN
    reason so every routing decision is auditable — the redevops.io/explain thesis for retrieval. Matches on
    word boundaries (so 'summarize' doesn't count as 'sum')."""
    if hint:
        return hint, f"caller pinned {hint}"
    q = (query or "").lower()
    for engine, kws in _ROUTES:
        hit = next((k for k in kws if re.search(r"\b" + re.escape(k) + r"\b", q)), None)
        if hit:
            return engine, f"query shape matched {engine} (on {hit!r})"
    return "vector", "no special shape — semantic vector default"


class KeywordRetriever:
    """Dependency-free retriever over an in-memory corpus (list of {id, text}) — token-overlap scoring, the
    same role HashingEmbedder plays for discovery. Stands in for a real engine so retrieval resolves
    in-process; swap for pgvector / HippoRAG / SQL / … in production."""

    def __init__(self, corpus: list[dict] | None = None):
        self.corpus = corpus or []

    def retrieve(self, query: str, k: int = 5) -> list[dict]:
        qt = set(re.findall(r"[a-z0-9]+", (query or "").lower()))
        if not qt:
            return []
        scored = []
        for d in self.corpus:
            dt = set(re.findall(r"[a-z0-9]+", (d.get("text", "")).lower()))
            ov = len(qt & dt)
            if ov:
                scored.append({**d, "score": round(ov / len(qt), 3)})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:k]


def _ext(name: str) -> str:
    i = name.rfind(".")
    return name[i:] if i >= 0 else ""


class CodeGraphRetriever:
    """Dependency-light code scope-graph — a *structural* retrieval arm (definitions · references ·
    symbols) over a codebase, the code analog of the graph arm. Inspired by tree-sitter scope-graph
    indexers (xai-codebase-graph): index the symbols, then answer 'definition of X' / 'references to
    X' structurally instead of by vector similarity — ideal for a coding/DevOps Sidekick. Python is
    parsed with the stdlib ``ast``; other languages fall back to a signature regex. Swap for a
    tree-sitter engine in production. Files over 5 MB are skipped (AST blow-up), like the original."""

    _MAX = 5 * 1024 * 1024
    _LANG = {".py": "py", ".js": "js", ".jsx": "js", ".ts": "ts", ".tsx": "ts", ".go": "go"}
    _SIG = {
        "js": re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", re.M),
        "go": re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", re.M),
    }
    _WORD = re.compile(r"[A-Za-z_]\w*")

    def __init__(self):
        self.symbols: list[dict] = []          # {name, kind, file, line, sig}
        self._refs: dict[str, list] = {}       # name → [(file, line)]

    def index(self, filename: str, text: str) -> "CodeGraphRetriever":
        lang = self._LANG.get(_ext(filename))
        if lang is None or len(text.encode("utf-8", "ignore")) > self._MAX:
            return self
        (self._index_python if lang == "py" else self._index_regex)(filename, text, lang)
        return self

    def index_dir(self, root: str) -> "CodeGraphRetriever":
        import os
        skip = ("/.git", "/node_modules", "/__pycache__", "/.venv", "/dist", "/build")
        for dp, _dn, fns in os.walk(root):
            if any(s in dp for s in skip):
                continue
            for fn in fns:
                if _ext(fn) in self._LANG:
                    p = os.path.join(dp, fn)
                    try:
                        with open(p, encoding="utf-8", errors="ignore") as fh:
                            self.index(p, fh.read())
                    except OSError:
                        pass
        return self

    def _index_python(self, filename: str, text: str, _lang: str) -> None:
        import ast
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return self._index_regex(filename, text, "py")
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.symbols.append({"name": node.name, "kind": "function", "file": filename,
                                     "line": node.lineno, "sig": f"def {node.name}(...)"})
            elif isinstance(node, ast.ClassDef):
                self.symbols.append({"name": node.name, "kind": "class", "file": filename,
                                     "line": node.lineno, "sig": f"class {node.name}"})
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                self._refs.setdefault(node.id, []).append((filename, node.lineno))

    def _index_regex(self, filename: str, text: str, lang: str) -> None:
        pat = self._SIG.get(lang, self._SIG["js"])
        for m in pat.finditer(text):
            self.symbols.append({"name": m.group(1), "kind": "function", "file": filename,
                                 "line": text.count("\n", 0, m.start()) + 1, "sig": m.group(0).strip()})

    def retrieve(self, query: str, k: int = 5) -> list[dict]:
        """Answer a structural code query: 'references/callers of X' → reference sites; otherwise →
        the definitions whose symbol name the query names. Ranked, EXPLAIN-friendly rows."""
        q = (query or "").lower()
        qwords = set(self._WORD.findall(query or ""))
        if any(w in q for w in ("reference", "references", "caller", "callers", "usage", "used by", "who calls")):
            rows = [{"id": f"{f}:{ln}", "name": n, "kind": "reference", "file": f, "line": ln,
                     "text": f"reference to {n}", "score": 1.0}
                    for n in qwords for (f, ln) in self._refs.get(n, [])]
            return rows[:k]
        scored = []
        for s in self.symbols:
            score = 1.0 if s["name"] in qwords else (0.6 if s["name"].lower() in q else 0.0)
            if score:
                scored.append({"id": f"{s['file']}:{s['line']}", "name": s["name"], "kind": s["kind"],
                               "file": s["file"], "line": s["line"], "text": s["sig"], "score": score})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:k]


class LocalContextRuntime:
    """In-repo default resolver over the capability registry, policy plane and belief fusion. Not a real
    optimizer — but the same Protocol as production, so wiring the real Context Runtime is a swap."""

    def __init__(self, registry, policy_plane=None, model: str = "local:template",
                 retrievers: dict[str, KnowledgeRetriever] | None = None, trust=None):
        self.registry = registry
        self.policy_plane = policy_plane
        self.model = model
        self.trust = trust     # TrustStore | None (lazy-loaded on first untrusted-source capability)
        # engine → retriever. Production injects the real Context Runtime engines here (pgvector · HippoRAG ·
        # graph · SQL/DuckDB · Elastic · vision); the router below picks among them by query shape.
        self.retrievers = retrievers or {}

    def resolve(self, intent: ContextIntent) -> ContextBundle:
        if intent.kind == BIND_CAPABILITY:
            return self._bind(intent)
        if intent.kind == CHECK_POLICY:
            return self._policy(intent)
        if intent.kind == RESOLVE_BELIEF:
            return self._belief(intent)
        if intent.kind == RETRIEVE_KNOWLEDGE:
            return self._retrieve(intent)
        if intent.kind == SELECT_MODEL:
            return ContextBundle(self.model, Provenance("model-router", "model",
                                 reason=f"configured model {self.model}"))
        raise ValueError(f"unknown context kind {intent.kind!r}")

    # ── resolver: knowledge retrieval, routed across engines by query shape ──
    def _retrieve(self, intent: ContextIntent) -> ContextBundle:
        query = intent.need or intent.goal
        engine, why = route_representation(query, intent.extra.get("representation"))
        retriever = self.retrievers.get(engine)
        if retriever is None:
            return ContextBundle([], Provenance("context-runtime", engine, reason=(
                f"routed to {engine} — {why}; no {engine} retriever wired (inject the real Context Runtime)")))
        results = retriever.retrieve(query, k=intent.extra.get("k", 5))
        alts = [e for e, _ in _ROUTES if e != engine]
        return ContextBundle(results, Provenance("context-runtime", engine,
                             score=(results[0]["score"] if results else None),
                             alternatives=alts, reason=f"routed to {engine} — {why}"), candidates=results)

    # ── resolver #1: capability binding (was compiler._bind) ──
    def _bind(self, intent: ContextIntent) -> ContextBundle:
        from . import cost
        candidates = self.registry.providing(intent.outcome)
        rep, score = "provides", None
        if not candidates:
            hits = [(c, s) for c, s in self.registry.discover(intent.need, k=3) if s >= _MIN_BIND_SCORE]
            candidates, rep = [c for c, _ in hits], "embedding"
            score = hits[0][1] if hits else None
        if not candidates:
            return ContextBundle(None, Provenance("capability-registry", rep,
                                 reason=f"no capability provides '{intent.outcome}'"))
        # Trust boundary: untrusted-source capabilities are discoverable (in `candidates`) but NOT
        # bindable — the bind door fails closed until the source is trusted. Built-in caps (source
        # "") are trusted, so nothing existing changes.
        bindable = [c for c in candidates if self._trusted(c)]
        if not bindable:
            srcs = sorted({c.source for c in candidates if getattr(c, "source", "")})
            return ContextBundle(None, Provenance("capability-registry", rep,
                                 alternatives=[c.name for c in candidates][:4],
                                 reason=(f"provider(s) of '{intent.outcome}' are from untrusted source(s) "
                                         f"{srcs} — trust the source to bind")), candidates)
        chosen = next((c for c in bindable if c.name == intent.prefer), None) if intent.prefer else None
        if chosen is None:
            chosen = cost.rank_candidates(bindable)[0]
        alts = [c.name for c in bindable if c.name != chosen.name][:4]
        reason = (f"sole provider of '{intent.outcome}'" if rep == "provides" and len(bindable) == 1
                  else f"top of {len(bindable)} candidates by cost")
        return ContextBundle(chosen, Provenance("capability-registry", rep, score, alts, reason), candidates)

    def _trusted(self, cap) -> bool:
        """Is this capability's source trusted? Built-in ("") always is; otherwise consult the trust
        store (lazily loaded, so the common all-built-in case never touches disk)."""
        src = getattr(cap, "source", "")
        if not src:
            return True
        if self.trust is None:
            from .trust import TrustStore
            self.trust = TrustStore()
        return self.trust.is_trusted(src)

    # ── resolver: policy decision ──
    def _policy(self, intent: ContextIntent) -> ContextBundle:
        d = self.policy_plane.decide(intent.node, intent.world, intent.mission, intent.graph)
        return ContextBundle(d, Provenance("policy-plane", "policy",
                             reason=("approval required" if d.required else "no gate")))

    # ── resolver: belief (state estimation) ──
    def _belief(self, intent: ContextIntent) -> ContextBundle:
        b = intent.world.belief(intent.key)
        return ContextBundle(b, Provenance("belief-fusion", "fused-belief",
                             score=(b.confidence if b else None),
                             reason=("conflicting sources" if (b and b.conflict) else "fused belief")))

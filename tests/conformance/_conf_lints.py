"""Pure AST/regex lint functions for the static conformance gate (tests/conformance/test_conformance_static.py).

Each lint takes ``(source: str)`` and returns a list of ``Violation`` -- no filesystem, no imports of app code --
so it can be unit-tested against tiny synthetic sources (a lint that cannot be shown to bite is not a lint).
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Violation:
    rule: str
    line: int
    snippet: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.rule}:{self.line}: {self.snippet}"


def _parse(src: str) -> ast.AST:
    return ast.parse(src)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(n, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _str_constants(tree: ast.AST):
    doc = _docstring_nodes(tree)
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc:
            yield n


def _snip(s: str, n: int = 70) -> str:
    return " ".join(s.split())[:n]


# ── (a) N7: no direct model HTTP clients ────────────────────────────────────────────────────────────────────
N7_STRINGS = ("/chat/completions", "api.openai.com", "api.anthropic.com")
N7_NAMES = ("OpenAICompatibleModel",)
# AgentConsole is allowed ONLY when it is handed an explicit ``model=`` (a governed model object that routes
# through agentic_os.app.llm.governed_text). Left to its default it builds its own OpenAI-compatible client
# straight from the environment -- an ungoverned model call.
N7_CONSOLE = "AgentConsole"


def lint_n7_direct_model_clients(src: str) -> list[Violation]:
    tree = _parse(src)
    out: list[Violation] = []
    for c in _str_constants(tree):
        for needle in N7_STRINGS:
            if needle in c.value:
                out.append(Violation("N7", c.lineno, f"string mentions {needle}"))
                break
    for n in ast.walk(tree):
        name = None
        if isinstance(n, ast.Name):
            name = n.id
        elif isinstance(n, ast.Attribute):
            name = n.attr
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                if a.name.split(".")[-1] in N7_NAMES:
                    name = a.name.split(".")[-1]
        if name in N7_NAMES:
            out.append(Violation("N7", n.lineno, f"references {name}"))
        elif isinstance(n, ast.Call):
            fn = n.func
            fname = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if fname.lstrip("_") == N7_CONSOLE and not any(k.arg == "model" for k in n.keywords):
                out.append(Violation("N7", n.lineno, f"{N7_CONSOLE}(...) without an explicit governed model="))
    return out


# ── (b) N5: no record_outcome with a literal numeric reward ─────────────────────────────────────────────────
def _is_num(node: ast.AST) -> bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        node = node.operand
    return isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
        and not isinstance(node.value, bool)


def lint_n5_literal_reward(src: str) -> list[Violation]:
    out: list[Violation] = []
    for n in ast.walk(_parse(src)):
        if isinstance(n, ast.Call):
            fn = n.func
            nm = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if nm == "record_outcome" and (any(_is_num(a) for a in n.args)
                                           or any(_is_num(k.value) for k in n.keywords)):
                out.append(Violation("N5", n.lineno, "record_outcome(...) with a numeric literal reward"))
    return out


# ── (c) N9: no hard-coded 'X.XX vs Y.YY' benchmark banners ──────────────────────────────────────────────────
_VS = re.compile(r"\d\.\d{2}\s+vs\.?\s+\d\.\d{2}")


def lint_n9_hardcoded_benchmark(src: str) -> list[Violation]:
    return [Violation("N9", c.lineno, _snip(m.group(0)))
            for c in _str_constants(_parse(src)) if (m := _VS.search(c.value))]


# ── (d) approval forgery: raw request bodies + route-level _approval reads ──────────────────────────────────
_ROUTE_VERBS = {"get", "post", "put", "patch", "delete", "route", "api_route", "websocket"}


def _is_json_call(n: ast.AST) -> bool:
    return (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "json"
            and isinstance(n.func.value, ast.Name) and n.func.value.id in {"request", "req"})


def lint_d_raw_request_json(src: str) -> list[Violation]:
    """Every ``request.json()`` / ``req.json()`` must be the direct argument of ``_strip_approval(...)``."""
    tree = _parse(src)
    parent: dict[int, ast.AST] = {}
    for p in ast.walk(tree):
        for ch in ast.iter_child_nodes(p):
            parent[id(ch)] = p
    out: list[Violation] = []
    for n in ast.walk(tree):
        if _is_json_call(n):
            p = parent.get(id(n))
            if isinstance(p, ast.Await):
                p = parent.get(id(p))
            wrapped = isinstance(p, ast.Call) and isinstance(p.func, ast.Name) and p.func.id == "_strip_approval"
            if not wrapped:
                out.append(Violation("D1", n.lineno, "request.json() not passed through _strip_approval()"))
    return out


def _reads_approval(fn: ast.AST) -> list[int]:
    lines = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in {"get", "pop"} \
                and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == "_approval":
            lines.append(n.lineno)
        elif isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) and n.slice.value == "_approval":
            lines.append(n.lineno)
        elif isinstance(n, ast.Compare) and isinstance(n.left, ast.Constant) and n.left.value == "_approval" \
                and any(isinstance(o, (ast.In, ast.NotIn)) for o in n.ops):
            lines.append(n.lineno)
    return lines


def lint_d_route_reads_approval(src: str) -> list[Violation]:
    """A route handler -- or a same-file function reachable from one -- must not read ``_approval`` out of a
    request-derived dict: the marker is injected in-process only, so an HTTP-facing function that reads it
    is either dead code or a forgery path."""
    tree = _parse(src)
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def is_route(f) -> bool:
        return any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in _ROUTE_VERBS
                   for d in f.decorator_list)

    seen: set[str] = set()
    stack = [f.name for f in funcs.values() if is_route(f)]
    while stack:
        nm = stack.pop()
        if nm in seen or nm not in funcs:
            continue
        seen.add(nm)
        for c in ast.walk(funcs[nm]):
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name):
                stack.append(c.func.id)
    out: list[Violation] = []
    for nm in sorted(seen):
        for ln in _reads_approval(funcs[nm]):
            out.append(Violation("D2", ln, f"{nm}() is reachable from a route and reads '_approval'"))
    return out


# ── (e) N8: approval_required capability handlers must consult the approval marker ──────────────────────────
def handler_mentions_approval(source: str) -> bool:
    return "_approval" in source or "pending_approval" in source


# ── (f) secrets: no default credential literals ─────────────────────────────────────────────────────────────
CRED_LITERALS = {"umami", "postiz", "demo-only-change-me"}
_CRED_KEY = re.compile(r"PASS|PWD|SECRET|TOKEN|KEY|USER", re.I)


def lint_f_default_credentials(src: str) -> list[Violation]:
    tree = _parse(src)
    out: list[Violation] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and len(n.args) >= 2:
            fn = n.func
            nm = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
            k, d = n.args[0], n.args[1]
            if nm in {"get", "getenv"} and isinstance(k, ast.Constant) and isinstance(k.value, str) \
                    and _CRED_KEY.search(k.value) and isinstance(d, ast.Constant) and d.value in CRED_LITERALS:
                out.append(Violation("F", n.lineno, f"default credential {d.value!r} for {k.value}"))
    for c in _str_constants(tree):
        if "demo-only-change-me" in c.value:
            out.append(Violation("F", c.lineno, "demo-only-change-me literal"))
    return out

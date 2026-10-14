"""Unknown must stay unknown: no CSAT inferred from resolution, no placeholder score for unverified email."""
import ast, pathlib

from agentic_os.world import enrichment as E

ROOT = pathlib.Path(__file__).resolve().parents[1]


import pytest


@pytest.mark.parametrize("cls", [E.HunterProvider, E.ApolloProvider, E.ClearbitProvider, E.PeopleDataLabsProvider])
def test_verify_email_with_no_provider_score_is_none_never_a_number(cls, monkeypatch):
    """Every provider path actually executed against fake transports (no swallowed exceptions)."""
    monkeypatch.setattr(E, "_get", lambda *a, **k: {"data": {}})
    monkeypatch.setattr(E, "_post", lambda *a, **k: {"data": {}})
    r = cls(key="k").verify_email("a@b.co")
    assert "score" in r and r["score"] is None, f"{cls.__name__}: {r}"


def test_hunter_reports_only_the_score_it_was_given(monkeypatch):
    monkeypatch.setattr(E, "_get", lambda *a, **k: {"data": {"status": "valid", "score": 80}})
    assert E.HunterProvider(key="k").verify_email("a@b.co")["score"] == 0.8
    monkeypatch.setattr(E, "_get", lambda *a, **k: {"data": {"status": "valid"}})
    r = E.HunterProvider(key="k").verify_email("a@b.co")
    assert r["score"] is None and r["verified"] is True


def test_resolve_contact_confidence_not_inflated_by_placeholder():
    class P:
        name = "p"
        def find_email(self, **k): return {"email": "x@y.co", "confidence": 0.4}
        def verify_email(self, e): return {"status": "unknown", "score": None, "verified": None}
    r = E.resolve_verified_email(domain="y.co", first_name="a", last_name="b", provider=P())
    assert r["confidence"] == 0.4 and r["verified"] is False


def test_resolve_verified_email_handles_hunter_missing_score(monkeypatch):
    def fake_get(url, *a, **k):
        if "email-finder" in url:
            return {"data": {"email": "x@y.co", "score": 70}}
        return {"data": {"status": "unknown"}}
    monkeypatch.setattr(E, "_get", fake_get)
    r = E.resolve_verified_email(domain="y.co", first_name="a", last_name="b", provider=E.HunterProvider(key="k"))
    assert r["confidence"] == 0.7 and r["verified"] is False


# ---- name-heuristic regression guard (NOT a proof) ---------------------------------------------
# KNOWN LIMITATIONS (deliberately not chased): a generic helper called with a renamed variable;
# sum()/mean() over a hardcoded list; a constant imported from another module; dict(zip(..)); values
# loaded from json/files; function defaults; setattr(); positional constructor args; synonym keys.
# A bare 0 / float(0) is treated as "no data", not as a score.
# Flags satisfaction-like targets (csat/nps/satisfaction) that are assigned a literal number or an
# expression built from numeric literals or from resolution/closure-proxy names. Covers: plain names,
# attributes, subscripts, dict literals, kwargs, .update(), walrus, tuple-unpack, lambdas, helper returns.
# Allow-listed labelled simulators are listed explicitly in ALLOW.
import re as _re
_SAT = _re.compile(r"(csat|\bnps\b|nps_|_nps|satisfaction)", _re.I)
_META = _re.compile(r"(_n|_count|_source|_truncated|_responses|_kpi|_ttl|_pages|_cache|_limit|_flag)$", _re.I)
_PROXY = _re.compile(r"(resolv|resolution|ratio|_rate|rate_|done|closed|solved)", _re.I)
_PASS = {"round", "float", "int", "max", "min", "abs"}


def _is_sat(name):
    return isinstance(name, str) and bool(_SAT.search(name)) and not _META.search(name)


def _num(v):
    if isinstance(v, ast.Constant):
        return isinstance(v.value, (int, float)) and not isinstance(v.value, bool) and v.value != 0
    if isinstance(v, ast.Call) and getattr(v.func, "id", "") in ("float", "int") and v.args \
            and isinstance(v.args[0], ast.Constant) and v.args[0].value == 0:
        return False
    if isinstance(v, ast.UnaryOp): return _num(v.operand)
    if isinstance(v, ast.BinOp): return _num(v.left) or _num(v.right)
    if isinstance(v, ast.IfExp): return _num(v.body) or _num(v.orelse)
    if isinstance(v, ast.BoolOp): return any(_num(x) for x in v.values)
    if isinstance(v, ast.Lambda): return _num(v.body)
    if isinstance(v, ast.NamedExpr): return _num(v.value)
    if isinstance(v, ast.Call) and getattr(v.func, "id", getattr(v.func, "attr", "")) in _PASS and v.args:
        return _num(v.args[0])
    return False


def _proxy(v):
    return any((isinstance(x, ast.Name) and _PROXY.search(x.id)) or
               (isinstance(x, ast.Attribute) and _PROXY.search(x.attr)) for x in ast.walk(v))


def _bad_value(v):
    return v is not None and (_num(v) or _proxy(v))


def _tname(t):
    if isinstance(t, ast.Name): return t.id
    if isinstance(t, ast.Attribute): return t.attr
    if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant): return t.slice.value
    return None


def _numlike(v):
    if isinstance(v, ast.Constant):
        if isinstance(v.value, str):
            return bool(_re.fullmatch(r"[\s$+\-]*\d[\d,.]*\s*[%xX]?", v.value))
        return isinstance(v.value, (int, float)) and not isinstance(v.value, bool)
    return False


def sat_violations(src, skip=()):
    """`skip`: names of top-level assignments / functions whose bodies are an allow-listed simulator."""
    tree = ast.parse(src)
    skipped = []
    for n in tree.body:
        nm = n.name if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) else \
            next((t.id for t in getattr(n, "targets", []) if isinstance(t, ast.Name)), None)
        if nm in skip:
            skipped.append((n.lineno, n.end_lineno))
    out = []
    def chk(name, val, line):
        if _is_sat(name) and _bad_value(val): out.append(line)
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, (ast.Tuple, ast.List)) and isinstance(n.value, (ast.Tuple, ast.List)) \
                        and len(t.elts) == len(n.value.elts):
                    for tt, vv in zip(t.elts, n.value.elts): chk(_tname(tt), vv, n.lineno)
                elif isinstance(t, (ast.Tuple, ast.List)):
                    if any(_is_sat(_tname(e)) for e in t.elts) and _proxy(n.value): out.append(n.lineno)
                else:
                    chk(_tname(t), n.value, n.lineno)
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign)):
            chk(_tname(n.target), n.value, n.lineno)
        elif isinstance(n, ast.NamedExpr):
            chk(_tname(n.target), n.value, n.lineno)
        elif isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant): chk(k.value, v, n.lineno)
            # tile rule: {"label": "CSAT", "value": "4.8", ...}
            d = {k.value: v for k, v in zip(n.keys, n.values) if isinstance(k, ast.Constant)}
            lab = d.get("label")
            if isinstance(lab, ast.Constant) and _is_sat(lab.value) and _numlike(d.get("value")):
                out.append(n.lineno)
        elif isinstance(n, ast.Tuple):
            e = n.elts
            if len(e) >= 2 and isinstance(e[0], ast.Constant) and _is_sat(e[0].value) \
                    and any(_numlike(x) for x in e[1:]):
                out.append(n.lineno)
        elif isinstance(n, ast.keyword):
            chk(n.arg, n.value, getattr(n.value, "lineno", 0))
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_sat(n.name):
            for r in ast.walk(n):
                if isinstance(r, ast.Return) and _bad_value(r.value): out.append(r.lineno)
    return sorted({l for l in out if not any(a <= l <= b for a, b in skipped)})



ALLOW = set()   # labelled simulators allowed to carry satisfaction constants (repo-relative)

EVASIONS = {
    "renamed": "csat = 4.2 + 0.8 * done_ratio", "helper": "def csat_score():\n    return 4.6",
    "subscript": "d['csat'] = 4.6", "attribute": "self.csat = 4.6", "lambda": "csat = lambda: 4.6",
    "walrus": "(csat := 4.6)", "tuple": "csat, n = 4.6, 3", "constant": "csat = 4.6",
    "dict_const": "x = {'csat_90d': 4.6}", "update": "d.update(csat=4.6)",
    "dict_expr": "x = {'csat': 4 + 0.5 * y}", "kwarg": "f(csat=4.6)",
    "proxy_name": "nps = resolved_count / total", "proxy_unpack": "csat, n = compute(resolved_rate)",
}

@pytest.mark.parametrize("src", ["csat_rate_limit = 5", "csat = float(0)", "csat_resolved_flag = resolved",
                                 "x = {'label': 'CSAT', 'value': 'unknown'}"])
def test_guard_known_false_positives_fixed(src):
    assert not sat_violations(src)


@pytest.mark.parametrize("name", sorted(EVASIONS))
def test_guard_catches_evasion(name):
    assert sat_violations(EVASIONS[name]), name


def test_guard_allows_honest_patterns():
    assert not sat_violations("csat = None\ncsat_n = 0\nd['csat'] = None\ncsat = round(sum(r) / len(r), 2)\n"
                              "f(csat=mean, csat_n=len(r))\ncsat, n, src = fetch()")


def test_guard_repo_clean():
    """Name-heuristic regression guard (not a proof)."""
    for base in ("agentic_os", "apps", "deploy"):
        for f in (ROOT / base).rglob("*.py"):
            rel = str(f.relative_to(ROOT))
            if rel in ALLOW:
                continue
            assert not sat_violations(f.read_text()), f"{rel}: {sat_violations(f.read_text())}"


def test_module_service_csat_tile_is_unknown():
    src = (ROOT / "deploy" / "module_service.py").read_text()
    assert '"label": "CSAT", "value": "unknown"' in src and "4.8" not in src.split('"CSAT"')[1][:80]


def test_module_service_every_numeric_tile_is_marked_demo():
    pytest.importorskip("fastapi")
    import importlib.util, re, sys
    spec = importlib.util.spec_from_file_location("module_service_f3", ROOT / "deploy" / "module_service.py")
    m = importlib.util.module_from_spec(spec); sys.modules["module_service_f3"] = m; spec.loader.exec_module(m)
    assert m.MODULE_DATA
    for name, d in m.MODULE_DATA.items():
        html_ = m._kpi_tiles(d["kpis"])
        tiles = re.findall(r"<div class='tile'[^>]*>.*?</div></div>", html_, flags=re.S)
        assert len(tiles) == len(d["kpis"]), name
        for t, k in zip(tiles, d["kpis"]):
            if str(k["value"]).lower() != "unknown":
                assert "data-simulated=\"true\"" in t and "DEMO DATA" in t, (name, k)
        page = m.render(d)
        assert "Illustrative demo data - not real measurements" in page

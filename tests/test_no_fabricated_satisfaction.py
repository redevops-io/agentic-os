"""Unknown must stay unknown: no CSAT inferred from resolution, no placeholder score for unverified email."""
import ast, pathlib

from agentic_os.world import enrichment as E

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_unverified_email_score_is_unknown_not_a_number():
    for name in dir(E):
        c = getattr(E, name)
        if isinstance(c, type) and "verify_email" in vars(c) and name.endswith("Provider"):
            try:
                inst = c.__new__(c)
                r = c.verify_email(inst, "a@b.co")
            except Exception:
                continue
            if r.get("status") == "unknown":
                assert r["score"] is None, f"{name}: unknown must not carry a numeric score"


def test_resolve_contact_confidence_not_inflated_by_placeholder():
    class P:
        name = "p"
        def find_email(self, **k): return {"email": "x@y.co", "confidence": 0.4}
        def verify_email(self, e): return {"status": "unknown", "score": None, "verified": None}
    r = E.resolve_verified_email(domain="y.co", first_name="a", last_name="b", provider=P())
    assert r["confidence"] == 0.4 and r["verified"] is False


def _bad(src):
    out = []
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and "csat" in t.id.lower() for t in n.targets):
            if any(isinstance(x, ast.Name) and ("resolv" in x.id.lower() or "resolution" in x.id.lower())
                   for x in ast.walk(n.value)):
                out.append(n.lineno)
    return out


def test_guard_no_csat_computed_from_resolution_rate():
    for base in ("apps", "agentic_os"):
        for f in (ROOT / base).rglob("*.py"):
            assert not _bad(f.read_text()), f"{f}: CSAT derived from resolution proxy"
    assert _bad("csat = round(4.2 + 0.8 * resolved_rate, 1)")

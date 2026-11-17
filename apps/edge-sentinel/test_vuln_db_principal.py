"""P7: the Doris vuln-DB principal and the operator principal are two different things.

`app.py` defined `_principal` twice at module level: the Doris row-scope principal (no required args) and
the authenticated operator name (`_principal(request)`). Python binds the LAST definition, so the two
vuln-DB call sites got the operator helper: `_principal()` raised
`TypeError: _principal() missing 1 required positional argument: 'request'` every time Doris was reachable —
swallowed by the bare `except Exception` in scan enrichment (advisory corroboration silently never appeared)
and surfaced as a 400 on the `vuln_db_lookup` action.

These tests fail on that code and pass on the fix (the Doris helper is now `_vuln_principal`). They need no
Doris, no network and no docker: the vuln-DB client is replaced by a fake that refuses a non-principal, so a
regression cannot pass quietly.
"""
from __future__ import annotations

import ast
import base64
import importlib
import pathlib

import pytest

app = importlib.import_module("edge-sentinel.app")
auth = importlib.import_module("edge-sentinel.auth")
HERE = pathlib.Path(__file__).resolve().parent


class _Req:
    """The only thing `auth.require_operator` reads is `request.headers`."""

    def __init__(self, headers=None):
        self.headers = headers or {}


class _Principal:
    """Stands in for `context_runtime.integrations.vuln_db.Principal`."""

    def __init__(self, roles=frozenset(), owns_rows_of=frozenset()):
        self.roles, self.owns_rows_of = frozenset(roles), frozenset(owns_rows_of)

    def __repr__(self):
        return f"Principal(roles={sorted(self.roles)}, owns={sorted(self.owns_rows_of)})"


class _VDB:
    """A Doris stand-in that fails loudly on the bug: a principal that is not a Principal is rejected."""

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []
        self.seen: list = []

    def available(self):
        return True

    def _check(self, principal):
        self.seen.append(principal)
        if not isinstance(principal, _Principal):
            raise TypeError(f"lookup needs a Principal, got {type(principal).__name__}: {principal!r}")

    def lookup(self, package=None, cve=None, principal=None, limit=None):
        self._check(principal)
        return self.rows

    def enrich(self, findings, principal=None):
        self._check(principal)
        return {f["id"]: [{}] for f in findings}


# ── the collision itself ─────────────────────────────────────────────────────

def test_app_module_defines_principal_exactly_once_at_module_level():
    """The bug was structural, so it is guarded structurally: a second module-level `_principal` re-binds
    the first silently — no test of the first one's behaviour can notice."""
    tree = ast.parse((HERE / "app.py").read_text(encoding="utf-8"))
    top = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert top.count("_principal") == 1, f"module-level `_principal` defined {top.count('_principal')} times"
    assert "_vuln_principal" in top, "the Doris row-scope principal must not share the operator helper's name"


def test_no_vuln_db_call_site_passes_the_operator_principal():
    """`_VDB.*` may only be handed `_vuln_principal()`; `_principal(` there is the P7 regression."""
    src = (HERE / "app.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        if "_VDB." in line and "_principal(" in line:
            assert "_vuln_principal(" in line, f"vuln-DB call handed the operator principal: {line.strip()}"


# ── the Doris principal (row-scope + masking), unchanged in behaviour ────────

def test_vuln_principal_defaults_to_the_security_role(monkeypatch):
    monkeypatch.setattr(app, "_Principal", _Principal)
    monkeypatch.delenv("VULN_DB_ROLE", raising=False)
    monkeypatch.delenv("VULN_DB_SCOPE", raising=False)
    p = app._vuln_principal()
    assert p.roles == frozenset({"security"}) and p.owns_rows_of == frozenset()


def test_vuln_principal_carries_the_analyst_row_scope_from_the_environment(monkeypatch):
    monkeypatch.setattr(app, "_Principal", _Principal)
    monkeypatch.setenv("VULN_DB_ROLE", "Analyst")           # case-insensitive, as documented
    monkeypatch.setenv("VULN_DB_SCOPE", "osv, nvd")
    p = app._vuln_principal()
    assert p.roles == frozenset({"analyst"})
    assert p.owns_rows_of == frozenset({"osv", "nvd"})      # whitespace-tolerant, masking depends on it


def test_vuln_principal_is_none_without_the_doris_client(monkeypatch):
    """No Doris, no principal — the caller must not build one out of thin air."""
    monkeypatch.setattr(app, "_Principal", None)
    assert app._vuln_principal() is None


# ── the operator principal still works (the name that survived) ──────────────

def test_operator_principal_reads_credentials_never_the_body(monkeypatch):
    monkeypatch.setattr(app, "_Principal", _Principal)      # prove the two no longer interfere
    monkeypatch.setenv("SENTINEL_BASIC_AUTH_USER", "sec")
    monkeypatch.setenv("SENTINEL_BASIC_AUTH_PASS", "hunter2")
    good = base64.b64encode(b"sec:hunter2").decode()
    assert app._principal(_Req({"authorization": f"Basic {good}"})) == "sec"
    assert app._principal(_Req({"authorization": "Basic " + base64.b64encode(b"sec:no").decode()})) is None
    assert app._principal(_Req({})) is None                 # anonymous stays anonymous


def test_operator_principal_is_none_when_no_credential_is_configured(monkeypatch):
    for e in ("SENTINEL_BASIC_AUTH_USER", "SENTINEL_BASIC_AUTH_PASS",
              "PARTNERS_BASIC_AUTH_USER", "PARTNERS_BASIC_AUTH_PASS"):
        monkeypatch.delenv(e, raising=False)
    assert app._principal(_Req({"authorization": "Basic " + base64.b64encode(b"sec:hunter2").decode()})) is None


# ── the two paths that broke: lookup, and enrichment that failed silently ────

def test_vuln_lookup_action_hands_doris_a_principal(monkeypatch):
    vdb = _VDB([{"cve_id": "CVE-2021-44228", "package": "log4j-core", "ecosystem": "Maven",
                 "cvss": 10.0, "fixed_version": "2.17.1", "source": "osv"}])
    monkeypatch.setattr(app, "_Principal", _Principal)
    monkeypatch.setattr(app, "_VDB", vdb)
    out = app._t_vuln_lookup({"cve": "CVE-2021-44228"})
    assert "1 record(s)" in out["text"] and "CVE-2021-44228" in out["text"]
    assert all(isinstance(p, _Principal) for p in vdb.seen), vdb.seen


def test_scan_enrichment_reports_corroboration_instead_of_failing_silently(monkeypatch):
    """The old failure mode was a bare `except Exception: enrich = {}` — the scan looked fine and simply
    never corroborated anything, so the assertion is on the result, not on an exception reaching us."""
    vdb = _VDB()
    monkeypatch.setattr(app, "_Principal", _Principal)
    monkeypatch.setattr(app, "_VDB", vdb)
    findings = [{"id": "CVE-2023-36053", "severity": "high"}, {"id": "CVE-2021-45046", "severity": "high"}]

    try:
        enrich = app._VDB.enrich(findings, app._vuln_principal())   # the exact call from the scan tool
    except Exception as e:  # noqa: BLE001 — a swallowed failure IS the bug being tested
        pytest.fail(f"scan enrichment must work against a reachable vuln-DB, raised {type(e).__name__}: {e}")
    assert len(enrich) == 2 and all(isinstance(p, _Principal) for p in vdb.seen)


def test_vuln_lookup_action_still_reports_an_unreachable_database(monkeypatch):
    """The honest-degradation wording the UI-claims tests rely on must survive the rename."""
    class _Down(_VDB):
        def available(self):
            return False

    monkeypatch.setattr(app, "_VDB", _Down())
    out = app._t_vuln_lookup({"cve": "CVE-2021-44228"})
    assert "isn't reachable" in out["text"] and out["data"] == {}


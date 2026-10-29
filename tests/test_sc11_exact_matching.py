"""SC-11: authorisation-sensitive attributes are compared EXACTLY. `in`, `eq`, ownership and column names never fold case: `EU` does not
match `eu`. A business field that needs case-insensitivity is normalised by an explicit trusted schema-level rule BEFORE authorisation
evaluation (documented), never by a hidden fold inside the plane."""
from __future__ import annotations

import base64
import pathlib
import re

import pytest

from agentic_os.permissions import AccessDecision, GrantStore, Identity, PermissionsPlane, make_authorizer

KEY = base64.b64encode(b"q" * 32).decode()
ROOT = pathlib.Path(__file__).resolve().parents[1]
ROWS = [{"id": 1, "region": "eu", "owner": "ana", "email": "a"}, {"id": 2, "region": "EU", "owner": "Ana", "email": "b"},
        {"id": 3, "region": "Eu", "owner": "ANA", "email": "c"}, {"id": 4, "region": "us", "owner": "bob", "email": "d"}]


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    (tmp_path / "p").mkdir()
    return GrantStore(str(tmp_path / "p"))


def _ids(plane, ident):
    d = plane.authorize(ident, "table", "t")
    assert d.allowed, d.reason
    return sorted(r["id"] for r in plane.slice(ROWS, d))


def test_in_is_exact_eu_does_not_match_EU(store):
    store.add("role", "r", "table", "t", row_scope="in", row_column="region", row_values=["eu"])
    assert _ids(PermissionsPlane(store), Identity(roles=["r"])) == [1]               # not rows 2 (EU) and 3 (Eu)


def test_in_with_an_upper_case_value_does_not_match_lower_case_rows(store):
    store.add("role", "r", "table", "t", row_scope="in", row_column="region", row_values=["EU"])
    assert _ids(PermissionsPlane(store), Identity(roles=["r"])) == [2]


def test_own_is_exact(store):
    store.add("role", "r", "table", "t", row_scope="own", row_column="owner")
    plane = PermissionsPlane(store)
    assert _ids(plane, Identity(user="ana", roles=["r"])) == [1]
    assert _ids(plane, Identity(user="Ana", roles=["r"])) == [2]
    assert _ids(plane, Identity(user="ANA", roles=["r"])) == [3]


def test_eq_in_a_hand_built_filter_is_exact(store):
    plane = PermissionsPlane(store)
    d = AccessDecision(True, "x", row_filter={"column": "region", "op": "eq", "value": "EU"})
    assert [r["id"] for r in plane.slice(ROWS, d)] == [2]
    d = AccessDecision(True, "x", row_filter={"column": "region", "op": "in", "values": ["eu", "us"]})
    assert [r["id"] for r in plane.slice(ROWS, d)] == [1, 4]


def test_the_or_of_predicates_is_exact_in_every_term(store):
    store.add("role", "r", "table", "t", row_scope="own", row_column="owner")
    store.add("role", "r", "table", "t", row_scope="in", row_column="region", row_values=["us"])
    assert _ids(PermissionsPlane(store), Identity(user="ana", roles=["r"])) == [1, 4]


def test_row_allowed_masked_for_row_and_the_gate_agree(store):
    store.add("role", "r", "table", "t", row_scope="in", row_column="region", row_values=["eu"], masked_columns=["email"])
    plane = PermissionsPlane(store)
    d = plane.authorize(Identity(roles=["r"]), "table", "t")
    assert plane.row_allowed(d, {"region": "eu"}) and not plane.row_allowed(d, {"region": "EU"})
    assert plane.masked_for_row(d, {"region": "EU", "email": "x"}) == []
    authz = make_authorizer(store, tool_resources={"t": ("table", "t")}, tool_actions={"t": "read"}, scoped_tools=["t"])

    class P:
        app, user, roles = "", "", ("r",)
    assert authz.permits_row(P(), "t", {"region": "eu"}) and not authz.permits_row(P(), "t", {"region": "EU"})


def test_mask_and_column_names_are_exact(store):
    store.add("role", "r", "table", "t", masked_columns=["email"])
    plane = PermissionsPlane(store)
    out = plane.slice([{"email": "a", "Email": "b", "EMAIL": "c"}], plane.authorize(Identity(roles=["r"]), "table", "t"))[0]
    assert out["email"] is None and out["Email"] == "b" and out["EMAIL"] == "c"       # exact: normalise the schema BEFORE authorisation
    store.add("role", "s", "table", "t", row_scope="in", row_column="Region", row_values=["eu"])
    d = plane.authorize(Identity(roles=["s"]), "table", "t")
    assert plane.slice([{"region": "eu"}], d) == []                                    # the column name `Region` is not `region`


def test_the_kernel_never_folds_case_in_an_authorisation_comparison():
    src = (ROOT / "agentic_os" / "permissions.py").read_text()
    code = [l for l in src.splitlines() if not l.strip().startswith(("#", '"""'))]
    hits = [l.strip() for l in code if re.search(r"\.lower\(\)|\.casefold\(\)|\.upper\(\)", l) and "_YES" not in l and "_env_yes" not in l
            and "v.strip().lower() in _YES" not in l]
    assert hits == []
    ui = (ROOT / "agentic_os" / "views.py").read_text()
    section = ui[ui.index("def permissions_page"):ui.index("def permissions_page") + 12000]
    assert "toLowerCase" not in section and "localeCompare" not in section


def test_the_docs_state_the_rule():
    text = (ROOT / "docs" / "PERMISSIONS_MIGRATION.md").read_text().replace("**", "")
    assert "compared exactly" in text and "trusted schema-level rule" in text

"""A comma inside a role, owner-column, column, mask or action name rejects the WHOLE collection / grant (a comma-packed string such as ``admin,ops`` is a
role-string smuggling vector: whoever splits on commas downstream reads two roles). Same rule as the CR planes (Python and Go)."""
from __future__ import annotations

import base64

import pytest

from agentic_os.permissions import GrantError, GrantStore, Identity, PermissionsPlane, make_authorizer, grant_problem, Grant

KEY = base64.b64encode(b"c" * 32).decode()
PACKED = ["a,b", ",", ",x", "x,", "admin,ops", "x, y", "a,,b"]


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    return GrantStore(str(tmp_path / "p"))


@pytest.mark.parametrize("bad", PACKED)
class TestIdentity:
    def test_a_comma_in_a_role_invalidates_the_principal_whole(self, bad):
        assert Identity(user="u", roles=["clerk", bad]).invalid_reason() == "roles is not a valid role collection"
        assert Identity(user="u", roles=[bad]).is_valid() is False

    def test_the_other_roles_are_not_honoured_either(self, store, bad):
        store.add("role", "clerk", "table", "t")
        plane = PermissionsPlane(store)
        assert plane.authorize(Identity(user="u", roles=["clerk"]), "table", "t").allowed is True
        d = plane.authorize(Identity(user="u", roles=["clerk", bad]), "table", "t")
        assert d.allowed is False and "invalid principal" in d.reason

    def test_a_packed_role_gets_no_admin_bypass(self, store, bad):
        store.add("role", "clerk", "table", "t")
        d = PermissionsPlane(store).authorize(Identity(user="u", roles=["admin,ops" if "," in bad else bad, "admin"]), "table", "t")
        assert d.allowed is False

    def test_the_gate_applies_the_same_rule(self, store, bad):
        store.add("role", "clerk", "table", "t")
        auth = make_authorizer(store, tool_resources={"tool": ("table", "t")}, tool_actions={"tool": "read"})
        spec = type("S", (), {"name": "tool", "side_effecting": False})()
        ok = type("P", (), {"app": "", "user": "u", "roles": ["clerk"]})()
        packed = type("P", (), {"app": "", "user": "u", "roles": ["clerk", bad]})()
        assert auth(ok, spec, {}) is None and auth(packed, spec, {}) is not None


@pytest.mark.parametrize("bad", PACKED)
class TestGrants:
    def test_a_role_subject_with_a_comma_is_refused(self, store, bad):
        with pytest.raises(GrantError):
            store.add("role", bad, "table", "t")

    @pytest.mark.parametrize("field", ["actions", "masked_columns"])
    def test_actions_and_masks_with_a_comma_reject_the_whole_grant(self, store, bad, field):
        with pytest.raises(GrantError):
            store.add("role", "clerk", "table", "t", **{field: ["read" if field == "actions" else "email", bad]})
        assert store.list() == []

    def test_an_owner_or_row_column_with_a_comma_is_refused(self, store, bad):
        with pytest.raises(GrantError):
            store.add("role", "clerk", "table", "t", row_scope="own", row_column=bad)
        with pytest.raises(GrantError):
            store.add("role", "clerk", "table", "t", row_scope="in", row_column=bad, row_values=["x"])

    def test_stored_grants_with_a_comma_are_unusable_and_counted(self, store, bad):
        good = store.add("role", "clerk", "table", "t")
        doc = store._load_raw()
        planted = [dict(good.to_dict(), id="g-planted-role", subject=bad),
                   dict(good.to_dict(), id="g-planted-mask", masked_columns=["email", bad]),
                   dict(good.to_dict(), id="g-planted-action", actions=["read", bad]),
                   dict(good.to_dict(), id="g-planted-col", row_scope="own", row_column=bad)]
        store._write(doc.items + planted, doc.meta)
        st = store.status()
        assert st["malformed"] == 4 and st["count"] == 1
        assert [g.id for g in store.list()] == [good.id]
        assert all(grant_problem(Grant.from_dict(d)) for d in planted)


def test_a_comma_in_a_user_or_app_name_is_not_a_role_vector_and_stays_valid():
    """The rule is about collections that something may split on: roles, mask / action / column names. A user or app name is one string compared
    exactly (an email-ish ``Doe, John`` is a name, not two principals)."""
    assert Identity(app="a,b", user="Doe, John", roles=[]).is_valid()


def test_the_migration_guide_documents_the_comma_rule():
    import pathlib
    t = (pathlib.Path(__file__).resolve().parents[1] / "docs" / "PERMISSIONS_MIGRATION.md").read_text()
    assert "comma" in t.lower() and "whole" in t.lower() and "audit-identifiers" in t

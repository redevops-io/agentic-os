"""B3 hardening: a grant that carries a TENANT label is persisted so that a reader which predates the field can never read it as an
UNTENANTED grant (the implicit wildcard); and this reader refuses a store format it does not know. A grant without a tenant stays in the
flat legacy format even when it carries a revocation epoch (the owner's lighter rule), which leaves one documented residual: see
``test_residual_a_rollback_can_revive_an_epoch_revoked_untenanted_grant``.

The regression for the downgrade is the one measured against the 0.6.0 release: that release copies the keys it knows from a stored entry
and drops the rest, so ``{"tenant": "t1", "revocation_epoch": 5, ...flat...}`` was read back as a grant for every tenant. Here the same
store is read by a model of that reader (``_release_read``, a transcription of ``Grant.from_dict`` / ``_usable`` / ``Grant.matches`` at
52bbd5a8), and, when ``AGENTIC_OS_RELEASE_TREE`` points at a checkout of the release, by the release's own code in a subprocess.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from agentic_os import permissions as P
from agentic_os.permissions import GrantStore, Identity, PermissionsPlane

T, R = "table", "crm.customers"


@pytest.fixture()
def st(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", "k" * 32)
    monkeypatch.delenv("PERMISSIONS_STRICT_TENANT", raising=False)
    return GrantStore(str(tmp_path))


@pytest.fixture()
def plain(tmp_path, monkeypatch):
    """A plaintext (development) store, so the test can read and rewrite the file the way an older binary would."""
    for k in ("PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE", "PERMISSIONS_ALLOW_GENERATED_KEY_DEV", "PERMISSIONS_STRICT_TENANT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_ALLOW_PLAINTEXT_DEV", "1")
    return GrantStore(str(tmp_path))


def _doc(store) -> dict:
    return json.loads(store.path.read_text())


def _release_read(doc: dict) -> list:
    """What the 0.6.0 release makes of a stored document: (subject_kind, subject, resource_kind, resource_name) of every entry it honours."""
    out = []
    for d in doc.get("grants", []):
        try:
            g = dict(id=d["id"], subject_kind=d.get("subject_kind", "app"), subject=d.get("subject", ""),
                     resource_kind=d.get("resource_kind", "database"), resource_name=d.get("resource_name", ""))
        except Exception:  # noqa: BLE001 - a non-dict entry, a missing id: unreadable, not honoured
            continue
        out.append((g["subject_kind"], g["subject"], g["resource_kind"], g["resource_name"]))
    return out


# ───────────────────────────── layout ─────────────────────────────

def test_a_store_without_tenant_grants_is_written_exactly_as_the_legacy_format(plain):
    plain.add("app", "a", T, R, revocation_epoch=0)
    plain.add("role", "r", T, R, revocation_epoch=0)
    plain.add("app", "b", T, R)                                            # B1 auto-stamps an epoch: still flat, still untenanted
    doc = _doc(plain)
    assert doc["version"] == 1 and len(doc["grants"]) == 3
    assert all("scoped_grant" not in e and "format" not in e and "tenant" not in e and "id" in e for e in doc["grants"])
    assert [e.get("revocation_epoch") for e in doc["grants"]] == [None, None, 1]      # the legacy shape plus the one extra key
    assert _release_read(doc) == [("app", "a", T, R), ("role", "r", T, R), ("app", "b", T, R)]


def test_only_a_tenant_grant_is_stored_in_the_versioned_envelope(plain):
    plain.add("app", "a", T, R, revocation_epoch=0)                       # flat, legacy
    plain.add("app", "a", T, R, tenant="t1")                               # tenant (+ auto epoch): enveloped
    plain.add("app", "b", T, R)                                            # epoch only: flat
    doc = _doc(plain)
    assert doc["version"] == 2
    flat, scoped = [e for e in doc["grants"] if "id" in e], [e for e in doc["grants"] if "id" not in e]
    assert len(flat) == 2 and len(scoped) == 1
    assert scoped[0]["format"] == 2 and "tenant" not in scoped[0] and "revocation_epoch" not in scoped[0]
    assert scoped[0]["scoped_grant"]["tenant"] == "t1" and scoped[0]["scoped_grant"]["revocation_epoch"] == 1
    assert all("tenant" not in e for e in flat)


def test_the_new_reader_round_trips_everything_through_the_envelope(plain):
    g = plain.add("app", "a", T, R, tenant="t1")
    assert plain.get(g.id) == g and plain.get(g.id).tenant == "t1" and plain.get(g.id).revocation_epoch == 1
    assert PermissionsPlane(plain).authorize(Identity(app="a", tenant="t1"), T, R, "read").allowed
    up = plain.update(g.id, actions=["read", "write"])
    assert up.tenant == "t1" and up.revocation_epoch == 1 and plain.get(g.id).actions == ["read", "write"]
    assert plain.revoke_epoch("app", "a", "t1") == 1
    assert not PermissionsPlane(plain).authorize(Identity(app="a", tenant="t1"), T, R, "read").allowed
    assert plain.remove(g.id) is True and plain.list() == []
    assert _doc(plain)["version"] == 1                                     # nothing needs the new format any more


def test_a_flat_entry_with_tenant_keys_is_moved_into_the_envelope_the_next_time_the_store_is_written(plain):
    plain.add("app", "a", T, R, revocation_epoch=0)
    doc = _doc(plain)
    doc["grants"][0].update(tenant="t1", revocation_epoch=4)               # what a raw writer, or a #743 store, leaves behind
    plain.path.write_text(json.dumps(doc))
    assert plain.list()[0].tenant == "t1"
    plain.add("app", "z", T, R, revocation_epoch=0)
    doc = _doc(plain)
    assert [e for e in doc["grants"] if e.get("tenant")] == [] and doc["version"] == 2


# ───────────────────────────── downgrade: the 0.6.0 release reads the store ─────────────────────────────

def test_rollback_to_the_release_reads_no_tenant_grant_as_a_wildcard(plain):
    """probe_rel_downgrade: the release read ``tenant: t1`` as no tenant at all and answered ``True`` for any identity."""
    plain.add("app", "support", T, R, tenant="t1")
    plain.add("app", "legacy", T, R, revocation_epoch=0)
    seen = _release_read(_doc(plain))
    assert ("app", "support", T, R) not in seen                            # not honoured: denied, never widened
    assert seen == [("app", "legacy", T, R)]                               # the legacy grant is still served


def test_rollback_to_the_release_does_not_revive_an_epoch_revoked_TENANT_grant(plain):
    plain.add("app", "support", T, R, tenant="t1")
    plain.revoke_epoch("app", "support", "t1")
    assert not PermissionsPlane(plain).authorize(Identity(app="support", tenant="t1"), T, R, "read").allowed
    assert _release_read(_doc(plain)) == []                                # enveloped: not read at all, so not live


def test_residual_a_rollback_can_revive_an_epoch_revoked_untenanted_grant(plain):
    """DOCUMENTED RESIDUAL (owner's lighter rule; docs/PERMISSIONS_MIGRATION.md section 7). An untenanted grant stays flat and carries
    ``revocation_epoch`` as an extra key. The 0.6.0 reader copies the keys it knows and ignores the rest, and it has no epoch store
    semantics, so a grant revoked by ``revoke_epoch`` is read as LIVE after a rollback. This test pins that behaviour so it cannot change
    unnoticed in either direction; the mitigation is to revoke an untenanted grant by removing it before a rollback."""
    g = plain.add("app", "support", T, R)
    assert g.revocation_epoch == 1
    plain.revoke_epoch("app", "support")
    assert not PermissionsPlane(plain).authorize(Identity(app="support"), T, R, "read").allowed      # this branch: revoked
    assert _release_read(_doc(plain)) == [("app", "support", T, R)]                                    # 0.6.0: readable as live
    plain.remove(g.id)                                                                                 # the documented mitigation
    assert _release_read(_doc(plain)) == []


def test_a_store_mixing_flat_and_tenant_grants_serves_each_correctly_on_both_readers(plain):
    plain.add("app", "legacy", T, R, revocation_epoch=0)
    plain.add("app", "stamped", T, R)
    plain.add("app", "support", T, R, tenant="t1")
    plain.add("app", "support", T, R, tenant="t2")
    pl = PermissionsPlane(plain)
    assert pl.authorize(Identity(app="legacy"), T, R, "read").allowed and pl.authorize(Identity(app="stamped"), T, R, "read").allowed
    assert pl.authorize(Identity(app="support", tenant="t1"), T, R, "read").allowed
    assert pl.authorize(Identity(app="support", tenant="t2"), T, R, "read").allowed
    assert not pl.authorize(Identity(app="support"), T, R, "read").allowed
    assert not pl.authorize(Identity(app="support", tenant="t3"), T, R, "read").allowed
    assert _release_read(_doc(plain)) == [("app", "legacy", T, R), ("app", "stamped", T, R)]          # 0.6.0: tenant grants denied


def test_upgrade_then_rollback_then_upgrade_loses_nothing_and_widens_nothing(plain):
    plain.add("app", "legacy", T, R, revocation_epoch=0)                   # written before the upgrade
    plain.add("app", "support", T, R, tenant="t1")                         # written after the upgrade
    # rollback: the 0.6.0 writer adds a grant and rewrites the file at version 1 with every entry as stored
    doc = _doc(plain)
    doc["version"] = 1
    doc["grants"].append({"id": "g-rolled", "subject_kind": "app", "subject": "during", "resource_kind": T, "resource_name": R,
                          "actions": ["read"], "row_scope": "all", "row_column": "owner", "row_values": [], "masked_columns": [],
                          "created_by": "", "at": 0.0})
    plain.path.write_text(json.dumps(doc))
    assert _release_read(doc) == [("app", "legacy", T, R), ("app", "during", T, R)]    # on 0.6.0 the tenant grant is denied
    # upgrade again
    again = GrantStore(plain.dir)
    pl = PermissionsPlane(again)
    assert pl.authorize(Identity(app="support", tenant="t1"), T, R, "read").allowed
    assert not pl.authorize(Identity(app="support"), T, R, "read").allowed
    assert pl.authorize(Identity(app="legacy"), T, R, "read").allowed and pl.authorize(Identity(app="during"), T, R, "read").allowed
    again.add("app", "z", T, R, revocation_epoch=0)                        # the next write restores the version marker
    assert _doc(again)["version"] == 2


def test_a_release_rewrite_of_the_file_keeps_the_scoped_entries_and_they_stay_unread(plain):
    """The release rewrites the file with version 1 and every entry as stored; the scoped entry must survive that and still read right."""
    g = plain.add("app", "support", T, R, tenant="t1")
    doc = _doc(plain)
    doc["version"] = 1                                                     # the release's _write
    doc["grants"].append({"id": "g-new", "subject_kind": "app", "subject": "late", "resource_kind": T, "resource_name": R,
                          "actions": ["read"], "row_scope": "all", "row_column": "owner", "row_values": [], "masked_columns": [],
                          "created_by": "", "at": 0.0})
    plain.path.write_text(json.dumps(doc))
    pl = PermissionsPlane(plain)
    assert pl.authorize(Identity(app="support", tenant="t1"), T, R, "read").allowed
    assert not pl.authorize(Identity(app="support"), T, R, "read").allowed
    assert pl.authorize(Identity(app="late"), T, R, "read").allowed
    assert plain.get(g.id).tenant == "t1"


_PROBE = textwrap.dedent("""
    import sys, os
    sys.path.insert(0, os.environ["TREE"])
    from agentic_os import permissions as P
    st = P.GrantStore(os.environ["DIR"])
    pl = P.PermissionsPlane(st)
    print("tenant" in P.Grant.__dataclass_fields__,
          pl.authorize(P.Identity(app="support"), "table", "crm.customers", "read").allowed,
          pl.authorize(P.Identity(app="legacy"), "table", "crm.customers", "read").allowed,
          pl.authorize(P.Identity(app="stamped"), "table", "crm.customers", "read").allowed)
""")


@pytest.mark.skipif(not os.environ.get("AGENTIC_OS_RELEASE_TREE"), reason="set AGENTIC_OS_RELEASE_TREE to a checkout of release/0.6.0")
def test_the_real_release_code_denies_a_tenant_grant_in_a_store_this_code_wrote(plain, tmp_path):
    plain.add("app", "support", T, R, tenant="t1")
    plain.add("app", "legacy", T, R, revocation_epoch=0)
    plain.add("app", "stamped", T, R)                                      # untenanted, epoch-stamped: flat, honoured by the release
    env = {"PATH": os.environ.get("PATH", ""), "TREE": os.environ["AGENTIC_OS_RELEASE_TREE"], "DIR": plain.dir,
           "PERMISSIONS_ALLOW_PLAINTEXT_DEV": "1"}
    out = subprocess.run([sys.executable, "-I", "-c", _PROBE], env=env, capture_output=True, text=True, check=True).stdout.split()
    # release has no tenant field; tenant grant: DENIED; legacy grant: allowed; untenanted epoch-stamped grant: allowed (flat)
    assert out == ["False", "False", "True", "True"], out


@pytest.mark.skipif(not os.environ.get("AGENTIC_OS_RELEASE_TREE"), reason="set AGENTIC_OS_RELEASE_TREE to a checkout of release/0.6.0")
def test_the_real_release_code_shows_the_documented_residual_for_an_epoch_revoked_untenanted_grant(plain):
    plain.add("app", "stamped", T, R)
    plain.add("app", "legacy", T, R, revocation_epoch=0)
    plain.add("app", "support", T, R, tenant="t1")
    plain.revoke_epoch("app", "stamped")
    plain.revoke_epoch("app", "support", "t1")
    env = {"PATH": os.environ.get("PATH", ""), "TREE": os.environ["AGENTIC_OS_RELEASE_TREE"], "DIR": plain.dir,
           "PERMISSIONS_ALLOW_PLAINTEXT_DEV": "1"}
    out = subprocess.run([sys.executable, "-I", "-c", _PROBE], env=env, capture_output=True, text=True, check=True).stdout.split()
    assert out == ["False", "False", "True", "True"], out   # the revoked tenant grant stays denied; the revoked untenanted one is LIVE on 0.6.0


# ───────────────────────────── a newer format is refused ─────────────────────────────

@pytest.mark.parametrize("version", [3, 99, "2", None, 2.0, True, 0, -1])
def test_a_store_version_this_reader_does_not_know_is_sealed_and_honours_nothing(plain, version):
    plain.add("app", "a", T, R, revocation_epoch=0)
    doc = _doc(plain)
    doc["version"] = version
    plain.path.write_text(json.dumps(doc))
    fresh = GrantStore(plain.dir)
    snap, sealed = fresh.snapshot()
    assert sealed and snap == []
    assert not PermissionsPlane(fresh).authorize(Identity(app="a"), T, R, "read").allowed
    with pytest.raises(RuntimeError):
        fresh.add("app", "b", T, R)                                        # and it will not overwrite what it cannot read


def test_an_envelope_from_a_newer_format_seals_the_store(plain):
    plain.add("app", "a", T, R, tenant="t1")
    doc = _doc(plain)
    scoped = next(e for e in doc["grants"] if "scoped_grant" in e)
    scoped["format"] = 3
    plain.path.write_text(json.dumps(doc))
    fresh = GrantStore(plain.dir)
    assert fresh.snapshot() == ([], True)
    assert not PermissionsPlane(fresh).authorize(Identity(app="a", tenant="t1"), T, R, "read").allowed


def test_a_malformed_envelope_is_unusable_not_a_wildcard(plain):
    plain.add("app", "a", T, R, tenant="t1")
    doc = _doc(plain)
    for e in doc["grants"]:
        if "scoped_grant" in e:
            e["scoped_grant"] = "t1"                                       # not a grant record
    plain.path.write_text(json.dumps(doc))
    fresh = GrantStore(plain.dir)
    assert fresh.list() == [] and fresh.status()["malformed"] == 1
    assert not PermissionsPlane(fresh).authorize(Identity(app="a"), T, R, "read").allowed
    assert not PermissionsPlane(fresh).authorize(Identity(app="a", tenant="t1"), T, R, "read").allowed


def test_the_encrypted_store_uses_the_same_layout_and_guard(st):
    st.add("app", "a", T, R, tenant="t1")
    st.add("app", "b", T, R, revocation_epoch=0)
    assert len(st.list()) == 2 and st.status()["count"] == 2 and not st.sealed
    assert PermissionsPlane(st).authorize(Identity(app="a", tenant="t1"), T, R, "read").allowed
    assert not PermissionsPlane(st).authorize(Identity(app="a"), T, R, "read").allowed

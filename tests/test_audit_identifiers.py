"""``agentic-os permissions audit-identifiers``: a READ-ONLY report of the stored grants whose names break the CURRENT identifier contract. It lists the
grant id, the field and the reason (never the offending value), counts, changes and deletes nothing (no migration, no key generation, no rewrite), and
leaves the remedy to the operator: remove the grant and add it again under a corrected name."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import subprocess
import sys

import pytest

from agentic_os.permissions import GrantStore

ROOT = pathlib.Path(__file__).resolve().parents[1]
KEY = base64.b64encode(b"u" * 32).decode()
SECRET_BITS = ("zero​width-SECRET", "pad ", "a,b-SECRET")


def _cli(*args, **env):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("PERMISSIONS_", "APPROVAL_", "AGENTIC_OS_"))}
    e.update(PYTHONPATH=str(ROOT), **env)
    return subprocess.run([sys.executable, "-m", "agentic_os.cli", "permissions", "audit-identifiers", *args], env=e, cwd=str(ROOT),
                          capture_output=True, text=True, timeout=120)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    return GrantStore(str(tmp_path / "p"))


def _snapshot(d):
    return {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in sorted(pathlib.Path(d).iterdir())}


def _plant(store):
    good = store.add("role", "clerk", "table", "t")
    doc = store._load_raw()
    bad = [dict(good.to_dict(), id="g-bad-subject", subject=SECRET_BITS[0]),
           dict(good.to_dict(), id="g-bad-resource", resource_name="t" + SECRET_BITS[1]),
           dict(good.to_dict(), id="g-bad-action", actions=["read", SECRET_BITS[2]]),
           dict(good.to_dict(), id="g-bad-mask", masked_columns=["email‮"]),
           dict(good.to_dict(), id="g-bad-owner", row_scope="own", row_column="owner "),
           dict(good.to_dict(), id="g-bad-role-comma", subject="admin,ops"),
           dict(good.to_dict(), id="g-private-use", subject="xy"),
           "not a record"]
    store._write(doc.items + bad, doc.meta)
    return good


def test_it_reports_each_violating_grant_with_its_field_and_reason_and_never_the_value(store, tmp_path):
    _plant(store)
    r = _cli("--dir", store.dir, PERMISSIONS_KEY=KEY)
    assert r.returncode == 1
    out = r.stdout
    for gid in ("g-bad-subject", "g-bad-resource", "g-bad-action", "g-bad-mask", "g-bad-owner", "g-bad-role-comma", "g-private-use"):
        assert gid in out, gid
    assert "subject:" in out and "resource_name:" in out and "actions[1]:" in out and "masked_columns[0]:" in out and "row_column:" in out
    assert "comma" in out and "private-use" in out and "line-separator" in out and "is not a grant record" in out
    for secret in ("SECRET", "​", "‮", "", "admin,ops"):
        assert secret not in out + r.stderr, secret
    assert "violating: 8" in out and "grants checked: 9" in out
    assert "nothing was changed" in out and "agentic-os permissions revoke" in out


def test_a_clean_store_reports_zero_and_exits_0(store):
    store.add("role", "clerk", "table", "t")
    store.add("user", "名前", "table", "t2", masked_columns=["メール"])
    r = _cli("--dir", store.dir, PERMISSIONS_KEY=KEY)
    assert r.returncode == 0 and "violating: 0" in r.stdout


def test_it_is_read_only_the_store_directory_is_byte_and_mtime_identical_afterwards(store):
    _plant(store)
    before = _snapshot(store.dir)
    _cli("--dir", store.dir, PERMISSIONS_KEY=KEY)
    _cli("--dir", store.dir, "--json", PERMISSIONS_KEY=KEY)
    assert _snapshot(store.dir) == before


def test_the_json_report_carries_ids_fields_reasons_and_the_table_version_only(store):
    _plant(store)
    r = _cli("--dir", store.dir, "--json", PERMISSIONS_KEY=KEY)
    rep = json.loads(r.stdout)
    assert rep["total"] == 9 and rep["violations"] == 8 and rep["contract"]["unicode_version"] == "17.0.0"
    assert {f["id"] for f in rep["findings"]} >= {"g-bad-subject", "g-bad-action"} and any(f["id"] is None for f in rep["findings"])
    assert "SECRET" not in r.stdout


def test_the_audit_matches_what_the_store_calls_malformed(store):
    _plant(store)
    rep = store.audit_identifiers()
    assert rep["violations"] == store.status()["malformed"] and rep["total"] == 9


def test_a_wrong_key_or_a_missing_store_is_an_error_not_a_report(store, tmp_path):
    store.add("role", "clerk", "table", "t")
    r = _cli("--dir", store.dir, PERMISSIONS_KEY=base64.b64encode(b"z" * 32).decode())
    assert r.returncode == 2 and "nothing to audit" in r.stderr
    r = _cli("--dir", str(tmp_path / "nothing"), PERMISSIONS_KEY=KEY)
    assert r.returncode in (0, 2)
    assert not (tmp_path / "nothing" / "grants.enc").exists()


def test_it_never_migrates_a_legacy_plaintext_file_or_generates_a_key(tmp_path):
    d = tmp_path / "legacy"
    d.mkdir()
    legacy = d / "grants.jsonl"
    legacy.write_text(json.dumps({"version": 1, "grants": []}))
    before = _snapshot(d)
    r = _cli("--dir", str(d), PERMISSIONS_KEY=KEY)
    assert _snapshot(d) == before and not (d / "grants.enc").exists() and not (d / ".permissions.key").exists()
    assert r.returncode in (0, 2)


def test_the_audit_command_is_documented_with_its_remedy():
    t = (ROOT / "docs" / "PERMISSIONS_MIGRATION.md").read_text()
    assert "audit-identifiers" in t and "revoke" in t and "corrected name" in t

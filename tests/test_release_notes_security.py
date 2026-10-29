"""RELEASE_NOTES_security.md carries sections 28-31 of the migration guide verbatim, the CHANGELOG points at it, and the factual claims of the limitations section are true of the code."""
from __future__ import annotations

import pathlib
import re

import pytest

from agentic_os import app_gate
from agentic_os.permissions_setup import init_local_profile

ROOT = pathlib.Path(__file__).resolve().parents[1]
GUIDE = (ROOT / "docs" / "PERMISSIONS_MIGRATION.md").read_text()
NOTES = (ROOT / "RELEASE_NOTES_security.md").read_text()


def section(n: int) -> str:
    m = re.search(rf"^## {n}\. .*?(?=^## \d+\. |\Z)", GUIDE, re.S | re.M)
    assert m, n
    return m.group(0).rstrip() + "\n"


@pytest.mark.parametrize("n", [28, 29, 30, 31, 32])
def test_the_release_notes_carry_the_guide_section_verbatim(n):
    assert section(n) in NOTES


def test_the_notes_state_the_claim_and_the_changelog_points_at_them():
    assert "The permissions plane and the app execution paths were reviewed in a multi-stage, AI-assisted security review with separate reviewer agents working from the code diff; this is not an external audit or third-party certification. Deployment configuration may still run unenforced when the setting is absent." in NOTES
    assert "RELEASE_NOTES_security.md" in (ROOT / "CHANGELOG.md").read_text()
    assert not re.search(r"(?i)secure[ -]by[ -]default", NOTES)


def test_the_review_claim_is_factual_and_no_document_claims_an_audit_or_certification():
    """The reviews were AI-assisted; the documents say so and never claim an independent/external audit, a certification or a verified-secure state."""
    assert "AI-assisted" in NOTES and "not an external audit or third-party certification" in NOTES
    for name in ("RELEASE_NOTES_security.md", "docs/PERMISSIONS_MIGRATION.md", "CHANGELOG.md", "docs/TOOL_CLASSIFICATION_INVENTORY.md"):
        text = (ROOT / name).read_text()
        assert not re.search(r"(?i)independent(ly)? (review|audit)|independently reviewed|verified secure|\bcertified\b", text), name


def test_every_limitation_is_in_the_section():
    s = section(31)
    for needle in ("Public intake can be filled", "identifies its caller by the frame", "tell anonymous callers whether authorization is enforced", "ticket seal is reachable", "revoke race", "`enforced` with no grants", "PRIVACY_PENDING_MAX", "PRIVACY_INTAKE_GLOBAL_PER_HOUR", "frame's `__name__`", "app_gate._SEAL", "approval token", "`enforced` with no grants"):
        assert needle in s, needle


def test_enforced_versus_restricted_is_what_section_31_says(monkeypatch, tmp_path):
    for k in ("CR_PERMISSIONS", "PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    app_gate.reset_state()
    app_gate.install_gate("guide")
    assert app_gate.authorization_fields("guide")["authorization"] == "restricted"          # nothing provisioned
    init_local_profile(str(tmp_path / "perm"), key_only=True)
    pathlib.Path(tmp_path / "perm" / "profile.json").write_text('{"version": 1, "user": "local"}')
    (tmp_path / "perm" / "profile.json").chmod(0o600)
    app_gate.install_gate("guide")
    assert app_gate.authorization_fields("guide")["authorization"] == "enforced"           # a key and a profile, no grant: everything is denied but the gate is enforcing
    assert app_gate.tool_denial("guide", "guide.retrieve") is not None
    app_gate.reset_state()

"""The artwork provenance record is current, states facts only, and lists the images that have no provenance."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REC = ROOT / "docs" / "legal" / "ARTWORK_PROVENANCE.md"


def test_the_record_is_current():
    r = subprocess.run([sys.executable, "-I", str(ROOT / "scripts/build_artwork_provenance.py"), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_record_asserts_no_authorship_or_rights_and_lists_unverified_images():
    t = REC.read_text()
    assert "no statement about authorship or rights" in t and "not an attestation" in t
    low = t.lower()
    for bad in ("first-party artwork", "we own", "the owner holds", "copyright (c)"):
        assert bad not in low, bad
    assert "apps/social-autopilot/reel/assets/hero.jpg" in t and "deploy/assets/repos/agentic-os.png" in t
    assert "Black Forest Labs" in t and "REMOVED from the product (D-LIC-8, option c)" in t
    assert "absent (checked now)" in t and "no image member" in t and "**PRESENT" not in t and "**IMAGE MEMBERS" not in t

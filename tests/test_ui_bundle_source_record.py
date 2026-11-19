"""packaging/ui-bundle-source.json records where the vendored Projects UI bundle comes from, and must match it.

The bytes of ``agentic_os/projects_ui`` (index.html, the JavaScript, the CSS) are pinned by sha256. As of the
2026-11-19 re-pin, the recorded UI source commit is a PUBLISHED commit of the public redevops-projects repository
(the previous local, unpublished commit this bundle was built from was lost in the 2026-11-07 scratchpad wipe and
could not be recovered); the record, the source offer and the SBOM must say so (decisions pins-04 and pins-10 are
resolved by this fact; D-LIC-12 -- which AGPL section 6 option to use -- stays OPEN, an owner/counsel choice this
record does not make).
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "agentic_os" / "projects_ui"
REC = json.loads((ROOT / "packaging" / "ui-bundle-source.json").read_text(encoding="utf-8"))


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_the_recorded_hashes_are_the_vendored_files_and_no_other_build_file_is_present():
    for rel, want in REC["files"].items():
        assert _sha(UI / rel) == want, rel
    built = {str(p.relative_to(UI)).replace("\\", "/") for p in UI.rglob("*") if p.is_file() and p.suffix in (".js", ".css", ".html")}
    assert built == set(REC["files"]), built ^ set(REC["files"])           # no stale bundle next to the recorded one
    html = (UI / "index.html").read_text(encoding="utf-8")
    for rel in REC["files"]:
        if rel != "index.html":
            assert "/" + rel in html, rel                                    # index.html loads exactly the recorded files


def test_the_record_says_the_source_commit_is_published():
    assert re.fullmatch(r"[0-9a-f]{40}", REC["source_commit"]) and re.fullmatch(r"[0-9a-f]{40}", REC["base_commit"])
    assert REC["source_commit"] != REC["base_commit"]
    assert REC["source_commit_published"] is True and REC["base_commit_public"] is True
    assert "public" in REC["source_status"] and "redevops-io/redevops-projects/commit/" + REC["source_commit"] in REC["source_status"]
    assert REC["build"]["commands"] == ["npm ci", "VITE_PROJECTS_API=/ npm run build -- --base=/"]
    text = json.dumps(REC).lower()
    assert not re.search(r"open[ -]source|tamper-proof|soc ?2|production-ready|certified", text)


def test_the_source_offer_row_names_the_commit_and_states_publication(tmp_path):
    sbom = {"serialNumber": "urn:uuid:test", "specVersion": "1.5", "metadata": {"component": {"properties": []}},
            "components": [{"type": "data", "name": "Projects UI bundle", "properties": [{"name": "redevops:key", "value": "ui-bundle"}]}]}
    (tmp_path / "sbom.json").write_text(json.dumps(sbom))
    out = tmp_path / "out.md"
    r = subprocess.run([sys.executable, "-I", str(ROOT / "packaging" / "sbom" / "generate_source_offer.py"), "--sbom", str(tmp_path / "sbom.json"),
                        "--out", str(out), "--template", str(ROOT / "packaging" / "SOURCE-OFFER.md")], capture_output=True, text=True, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    row = next(l for l in out.read_text(encoding="utf-8").splitlines() if l.startswith("| Projects UI bundle"))
    assert REC["source_commit"] in row and REC["package_lock_git_blob"][:8] in row
    assert "published" in row and "NOT PUBLISHED" not in row and "D-LIC-12" in row
    assert "NOT RECORDED" not in row

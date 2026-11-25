"""The published-release supply-chain check must fail a release that drops an SBOM, the manifest, or a listed hash."""
import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_assets_check", ROOT / "packaging/acceptance/release_assets_check.py")
rac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rac)

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
COMMIT = "a" * 40


def sbom(commit=COMMIT, components=1):
    return json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.5", "components": [{"name": "x"}] * components,
                       "metadata": {"component": {"properties": [{"name": "redevops:source-commit", "value": commit}]}}}).encode()


@pytest.fixture
def release(tmp_path, monkeypatch):
    """A complete release: two installers, a signed manifest that lists them and their SBOMs."""
    d = tmp_path / "assets"
    d.mkdir()
    key = tmp_path / "key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    shutil.copy(str(key) + ".pub", d / "release-signing-key.pub")
    monkeypatch.setattr(rac, "RELEASE_KEY_FPR", rac.fingerprint(d / "release-signing-key.pub"))
    files = {"App 1_aarch64.dmg": b"dmg", "App 1_amd64.deb": b"deb"}
    sboms = {"sbom-macos-arm64.cdx.json": sbom(), "sbom-linux-amd64.cdx.json": sbom()}
    h = lambda b: hashlib.sha256(b).hexdigest()

    def build(manifest_sboms=True, tag="r0prime-v9.9.9"):
        doc = {"release_commit": COMMIT, "platforms": {"macos": [{"name": "App 1_aarch64.dmg", "sha256": h(files["App 1_aarch64.dmg"])}],
                                                       "linux": [{"name": "App 1_amd64.deb", "sha256": h(files["App 1_amd64.deb"])}]}}
        if manifest_sboms:
            doc["sbom"] = [{"name": n, "sha256": h(b)} for n, b in sboms.items()]
        (d / "release-manifest.json").write_text(json.dumps(doc))
        (d / "release-manifest.json.sig").unlink(missing_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", rac.SSHSIG_NS, str(d / "release-manifest.json")], check=True)
        for n, b in sboms.items():
            (d / n).write_bytes(b)
        assets = [{"name": n.replace(" ", "."), "digest": "sha256:" + h(b)} for n, b in files.items()]
        assets += [{"name": p.name} for p in d.iterdir()]
        return tag, d, {"assets": assets}
    return build, files, sboms


def status(rows):
    return {r["test_id"]: r["status"] for r in rows}


def test_a_complete_release_passes_every_row(release):
    build, _, _ = release
    st = status(rac.check(*build()))
    assert set(st.values()) == {"passed"} and "sbom.signed.sbom-linux-amd64.cdx.json" in st


def test_a_release_without_one_sbom_fails(release):
    build, _, _ = release
    tag, d, rel = build()
    (d / "sbom-linux-amd64.cdx.json").unlink()
    rel["assets"] = [a for a in rel["assets"] if a["name"] != "sbom-linux-amd64.cdx.json"]
    assert status(rac.check(tag, d, rel))["sbom.present"] == "failed"


def test_an_sbom_of_another_commit_an_empty_one_and_a_swapped_one_fail(release):
    build, _, sboms = release
    tag, d, rel = build()
    (d / "sbom-linux-amd64.cdx.json").write_bytes(sbom(commit="b" * 40))
    st = status(rac.check(tag, d, rel))
    assert st["sbom.commit.sbom-linux-amd64.cdx.json"] == "failed" and st["sbom.signed.sbom-linux-amd64.cdx.json"] == "failed"
    (d / "sbom-linux-amd64.cdx.json").write_bytes(sbom(components=0))
    assert status(rac.check(tag, d, rel))["sbom.valid.sbom-linux-amd64.cdx.json"] == "failed"


def test_an_unsigned_sbom_is_only_tolerated_for_the_two_named_old_releases_and_never_counted_as_passed(release):
    build, _, _ = release
    st = status(rac.check(*build(manifest_sboms=False)))
    assert st["sbom.signed.sbom-macos-arm64.cdx.json"] == "failed"
    st = status(rac.check(*build(manifest_sboms=False, tag="r0prime-v0.1.5")))
    assert st["sbom.signed.sbom-macos-arm64.cdx.json"] == "unsupported" and "failed" not in st.values()


def test_a_missing_manifest_a_bad_signature_and_a_changed_installer_fail(release):
    build, _, _ = release
    tag, d, rel = build()
    rel2 = {"assets": [dict(a, digest="sha256:" + "0" * 64) if a["name"].endswith(".deb") else a for a in rel["assets"]]}
    assert status(rac.check(tag, d, rel2))["installers.digests"] == "failed"
    rel3 = {"assets": rel["assets"] + [{"name": "Extra_x64.msi", "digest": "sha256:" + "1" * 64}]}
    assert status(rac.check(tag, d, rel3))["installers.all_listed"] == "failed"
    (d / "release-manifest.json").write_text((d / "release-manifest.json").read_text() + " ")
    assert status(rac.check(tag, d, rel))["manifest.signature"] == "failed"
    (d / "release-manifest.json.sig").unlink()
    assert status(rac.check(tag, d, rel)) == {"manifest.present": "failed"}


def test_the_acceptance_workflow_runs_the_check_and_cannot_skip_it():
    wf = (ROOT / ".github/workflows/published-artifacts-acceptance.yml").read_text()
    assert "packaging/acceptance/release_assets_check.py --tag" in wf
    job = wf.split("  supply-chain:")[1].split("\n  native:")[0]
    assert "continue-on-error" not in job and "|| true" not in job

"""deploy/compat/releases.json states which desktop version, release tag, kernel, bundle and UI belong together."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("compat_check", ROOT / "packaging/acceptance/compat_check.py")
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)
DOC = json.loads((ROOT / "deploy/compat/releases.json").read_text())


def test_the_file_is_consistent_and_names_the_bundle_this_checkout_installs():
    assert cc.offline(DOC) == []


def test_the_desktop_version_and_its_release_tag_are_stated_separately_and_never_derived():
    cur = next(r for r in DOC["desktop"] if r["status"] == "current")
    assert cur["app_version"] not in cur["release_tag"], "if they ever coincide, drop this test; today they differ and tools must read both"
    assert cur["release_url"].endswith(cur["release_tag"]) and cur["release_tag"] in cur["manifest_url"]


def test_a_stale_bundle_pin_two_current_entries_and_a_tag_under_the_wrong_url_are_refused(tmp_path):
    d = copy.deepcopy(DOC)
    d["bundle"][0]["redevops_qa"] = "0.0.1"
    assert any("QA_IMAGE_TAG" in b for b in cc.offline(d))
    d = copy.deepcopy(DOC)
    d["desktop"][1]["status"] = "current"
    assert any("exactly one entry must be current" in b for b in cc.offline(d))
    d = copy.deepcopy(DOC)
    d["desktop"][0]["release_url"] = d["desktop"][0]["release_url"].rsplit("/", 1)[0] + "/r0prime-v0.0.0"      # another tag's page
    assert any("release_url" in b for b in cc.offline(d))


def test_the_published_release_must_match_its_entry(tmp_path):
    cur = next(r for r in DOC["desktop"] if r["status"] == "current")
    man = {"release_commit": cur["build_commit"], "platforms": {"macos": [{"name": f"App_{cur['app_version']}_aarch64.dmg"}]}}
    (tmp_path / "release-manifest.json").write_text(json.dumps(man))
    d = copy.deepcopy(DOC)
    d["desktop"][0]["manifest_sha256"] = hashlib.sha256((tmp_path / "release-manifest.json").read_bytes()).hexdigest()
    assert cc.published(d, cur["release_tag"], tmp_path) == []
    assert any("published manifest is" in b for b in cc.published(DOC, cur["release_tag"], tmp_path))           # another manifest
    man["platforms"]["macos"][0]["name"] = "App_9.9.9_aarch64.dmg"
    (tmp_path / "release-manifest.json").write_text(json.dumps(man))
    assert any("do not carry app version" in b for b in cc.published(d, cur["release_tag"], tmp_path))
    assert cc.published(DOC, "r0prime-v9.9.9", tmp_path) == ["no desktop entry for the release tag r0prime-v9.9.9: add it to deploy/compat/releases.json"]


def test_the_published_release_check_runs_the_compatibility_check():
    wf = (ROOT / ".github/workflows/published-artifacts-acceptance.yml").read_text()
    assert 'packaging/acceptance/compat_check.py --tag "$TAG" --release-dir rel' in wf

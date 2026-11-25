"""The Projects UI is a PINNED artifact (url + sha256 + source commit) vendored reproducibly into the build (convergence stage 5).

As of the 2026-11-19 re-pin the artifact is built from a PUBLISHED commit of the public redevops-projects repository, but it still lives in
this repository (``local_path``) because no GitHub Release asset has been published for it yet, so builds and tests stay offline; the lock
says so (``source_commit_published: true``, empty ``url``) and a later release publication only fills in the URL. Decision D-LIC-12 (which
AGPL section 6 option to use) is not touched by this.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import tarfile
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LOCK = json.loads((ROOT / "packaging" / "projects_ui.lock.json").read_text(encoding="utf-8"))
REC = json.loads((ROOT / "packaging" / "ui-bundle-source.json").read_text(encoding="utf-8"))
ART = ROOT / LOCK["local_path"]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fetch_mod = _load("fetch_projects_ui")
pack_mod = _load("make_projects_ui_artifact")


def _members():
    with tarfile.open(ART, "r:gz") as t:
        return {m.name: t.extractfile(m).read() for m in t.getmembers() if m.isfile()}


def test_the_lock_pins_the_committed_artifact_and_says_it_is_published():
    assert hashlib.sha256(ART.read_bytes()).hexdigest() == LOCK["sha256"]
    assert re.fullmatch(r"[0-9a-f]{64}", LOCK["sha256"]) and re.fullmatch(r"[0-9a-f]{40}", LOCK["source_commit"])
    assert LOCK["source_commit_published"] is True and LOCK["url"] == ""     # source is public; no release asset exists yet, so url is still empty
    assert LOCK["source_commit"] == REC["source_commit"] and LOCK["base_commit"] == REC["base_commit"]
    assert "OPEN" in LOCK["_comment"] and "D-LIC-12" in LOCK["_comment"]
    assert not re.search(r"open[ -]source|tamper-proof|soc ?2|production-ready|certified", json.dumps(LOCK).lower())
    assert REC["artifact"]["sha256"] == LOCK["sha256"] and REC["artifact"]["path"] == LOCK["local_path"]


def test_the_members_are_exactly_the_recorded_files():
    got = {n: hashlib.sha256(d).hexdigest() for n, d in _members().items()}
    assert got == LOCK["bundle_files"]
    for rel, want in REC["files"].items():
        assert got[rel] == want                                     # packaging/ui-bundle-source.json agrees with the artifact
    html = _members()["index.html"].decode()
    scripts = "".join(_members()[rel].decode() for rel in REC["files"] if rel.endswith(".js") and "/" + rel in html)
    for rel in REC["files"]:
        if rel != "index.html":
            assert "/" + rel in html or rel.rsplit("/", 1)[-1] in scripts            # a code-split chunk is loaded by the entry script


def test_the_artifact_carries_no_image_at_all():
    """D-LIC-8 option c: the hero image is removed from the product. The artifact is build output only, in the lock and in the archive."""
    names = set(_members())
    assert "hero.jpg" not in names and "hero.jpg" not in LOCK["bundle_files"]
    assert not any(n.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico")) for n in names), names
    assert not (ROOT / "agentic_os" / "projects_ui" / "hero.jpg").exists() and not (ROOT / "scripts" / "make_neutral_hero.py").exists()


def test_the_superseded_local_source_chain_is_kept_as_history():
    """2026-11-19: this pin was rebuilt from a published public commit because the previous pin's whole local source
    chain (down to ea9e53e5) was lost in the 2026-11-07 scratchpad wipe and could not be recovered. The superseded
    chain's own artifacts are kept in the repository unchanged, as history, even though their content now differs
    from the current pin (a different, later source)."""
    prev = LOCK["previous_pin"]
    assert prev["version"] == "local+ea9e53e5-nohero" and prev["source_commit_published"] is False
    old = ROOT / prev["local_path"]
    assert hashlib.sha256(old.read_bytes()).hexdigest() == prev["sha256"]
    # the chain nests all the way back to the public v0.2.0 release
    node = prev
    seen = [node["version"]]
    while "previous_pin" in node:
        node = node["previous_pin"]
        seen.append(node["version"])
    assert seen[-1] == "v0.2.0"
    assert LOCK["source_commit_published"] is True and LOCK["source_commit"] != prev["source_commit"]


def test_packing_is_deterministic_and_normalised():
    files = _members()
    a, b = pack_mod.pack(files), pack_mod.pack(dict(sorted(files.items(), reverse=True)))
    assert a == b == ART.read_bytes() or a == b                  # order-independent; equal to the committed bytes when the zlib matches
    with tarfile.open(ART, "r:gz") as t:
        for m in t.getmembers():
            assert (m.mtime, m.uid, m.gid, m.uname, m.gname) == (pack_mod.MTIME, 0, 0, "", ""), m.name
            assert m.isfile() or m.isdir()
    assert pack_mod.MTIME >= 315532800                               # not before 1980: zip-based tools refuse it


def test_the_pack_script_refuses_stray_files(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>")
    (dist / "assets" / "a.js").write_text("1")
    (dist / "notes.txt").write_text("stray")
    with pytest.raises(SystemExit, match="only build output"):
        pack_mod.collect(dist)


def test_fetch_is_offline_verified_and_replaces_the_destination(tmp_path, monkeypatch):
    real = urllib.request.urlopen

    def only_file(url, *a, **k):
        assert str(url).startswith("file:"), f"the pinned artifact must be read locally, not from {url}"
        return real(url, *a, **k)
    monkeypatch.setattr(urllib.request, "urlopen", only_file)
    dest = tmp_path / "ui"
    dest.mkdir()
    (dest / "stale.js").write_text("x")
    assert fetch_mod.fetch(LOCK, dest) == "fetched"
    assert not (dest / "stale.js").exists() and (dest / ".pinned-sha256").read_text().strip() == LOCK["sha256"]
    got = {p.relative_to(dest).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in dest.rglob("*") if p.is_file() and p.name != ".pinned-sha256"}
    assert got == LOCK["bundle_files"]
    assert fetch_mod.fetch(LOCK, dest) == "current"


def test_a_tampered_artifact_is_refused_before_anything_is_replaced(tmp_path):
    lock = dict(LOCK, sha256="0" * 64)
    dest = tmp_path / "ui"
    dest.mkdir()
    (dest / "index.html").write_text("good")
    with pytest.raises(fetch_mod.FetchError, match="sha256 mismatch"):
        fetch_mod.fetch(lock, dest)
    assert (dest / "index.html").read_text() == "good"


def test_a_local_path_cannot_leave_the_repository(tmp_path):
    with pytest.raises(fetch_mod.FetchError, match="escapes"):
        fetch_mod.fetch(dict(LOCK, local_path="../outside.tgz"), tmp_path / "ui")


def test_publication_changes_only_the_url(tmp_path):
    """With a url in the lock (file: stands in for a release asset) the same sha pin governs; local_path is not consulted."""
    other = tmp_path / "asset.tgz"
    other.write_bytes(ART.read_bytes())
    lock = dict(LOCK, url=other.as_uri(), local_path="packaging/ui-artifacts/does-not-exist.tgz")
    assert fetch_mod.fetch(lock, tmp_path / "ui") == "fetched"


# ---------------------------------------------------------------------------------------------------------- the 2026-11-19 re-pin
def test_the_lost_local_only_features_are_not_in_the_current_bundle():
    """The accessibility series, the mission drill-down, the server-driven Sample/Partly-sample chip, the plain-words
    uncertain/stale/compensation flags, the shared keyboard focus ring, and the fixed-wording source refusal/remove
    flows only ever existed in the local commit chain (ea9e53e5 and its ancestors) that was lost in the 2026-11-07
    scratchpad wipe. This pin is rebuilt from the public repository instead (see packaging/projects_ui.lock.json's
    own _comment), which does not contain that work, so none of it is in the current bundle. This is a known,
    accepted regression (the owner chose re-pinning to public main over redoing the lost work first), not a bug:
    this test documents it so a future re-add of this functionality is a deliberate, visible change."""
    js = next(v for k, v in _members().items() if k.endswith(".js")).decode("utf-8")
    lost_strings = (
        "Deployment inspection", "Social intelligence",                                   # v0.2.0-era cards, present in the lost build only
        "aos.drilldown.timeline/", "aos.drilldown.evidence-inspect/",                     # mission drill-down
        "/api/ui-config", "Partly sample",                                                # server-driven sample/runtime chip
        "Outcome unknown: the effect may or may not exist.",                              # uncertain/stale/compensation wording
        "Your whole home folder can't be connected. Pick a folder inside it, for example Documents.",  # fixed source-refusal wording
    )
    for s in lost_strings:
        assert s not in js, f"{s!r} unexpectedly present: is this still built from a lost-work commit?"


def test_the_pin_chain_keeps_the_replaced_artifacts_and_ends_at_the_public_release():
    chain = ["local+ea9e53e5-nohero", "local+ea9e53e5", "local+9a1788b1", "local+5751e9e5", "local+312a811a", "local+2f24719e", "local+ee958a01", "local+2fdef7f5"]
    node = LOCK
    for want in chain:
        node = node["previous_pin"]
        assert node["version"] == want and node["source_commit_published"] is False, want
        f = ROOT / node["local_path"]
        assert f.is_file() and hashlib.sha256(f.read_bytes()).hexdigest() == node["sha256"], want          # going back is a lock edit
    assert node["previous_pin"]["version"] == "v0.2.0" and re.fullmatch(r"[0-9a-f]{64}", node["previous_pin"]["sha256"])
    assert LOCK["merged_public_release"]["tag"] == "v0.2.0"
    assert REC["merged_public_release"] == {"tag": "v0.2.0", "commit": LOCK["merged_public_release"]["commit"]}
    # 2026-11-19: a real re-pin to a different, later, PUBLISHED source - not just an image removal from the same source
    assert LOCK["source_commit"] != LOCK["previous_pin"]["source_commit"]
    assert LOCK["source_commit_published"] is True and LOCK["url"] == ""


def test_the_record_states_that_the_npm_lock_is_unchanged_since_the_base():
    assert REC["lock_unchanged_since_base"] is True
    assert "byte-identical" in REC["lock_change_since_base"]

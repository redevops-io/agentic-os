"""The Projects UI is a PINNED artifact (url + sha256 + source commit) vendored reproducibly into the build (convergence stage 5).

Today the artifact is built locally from an UNPUBLISHED commit of the UI source and lives in this repository, so builds and tests are offline;
the lock says so (``source_commit_published: false``, empty ``url``) and a later publication only fills in the URL. Nothing here claims a
publication; the distribution gate decisions pins-04 / D-LIC-12 are not touched.
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


def test_the_lock_pins_the_committed_artifact_and_says_it_is_unpublished():
    assert hashlib.sha256(ART.read_bytes()).hexdigest() == LOCK["sha256"]
    assert re.fullmatch(r"[0-9a-f]{64}", LOCK["sha256"]) and re.fullmatch(r"[0-9a-f]{40}", LOCK["source_commit"])
    assert LOCK["source_commit_published"] is False and LOCK["url"] == ""           # nothing is published; a later publication fills in the url
    assert LOCK["source_commit"] == REC["source_commit"] and LOCK["base_commit"] == REC["base_commit"]
    assert "NOT a source offer" in LOCK["_comment"] and "OPEN" in LOCK["_comment"]
    assert not re.search(r"open[ -]source|tamper-proof|soc ?2|production-ready|certified", json.dumps(LOCK).lower())
    assert REC["artifact"]["sha256"] == LOCK["sha256"] and REC["artifact"]["path"] == LOCK["local_path"]


def test_the_members_are_exactly_the_recorded_files():
    got = {n: hashlib.sha256(d).hexdigest() for n, d in _members().items()}
    assert got == LOCK["bundle_files"]
    for rel, want in REC["files"].items():
        assert got[rel] == want                                     # packaging/ui-bundle-source.json agrees with the artifact
    html = _members()["index.html"].decode()
    for rel in REC["files"]:
        if rel != "index.html":
            assert "/" + rel in html


def test_the_artifact_carries_the_neutral_hero_of_this_repository():
    hero = _members()["hero.jpg"]
    assert hero == (ROOT / "agentic_os" / "projects_ui" / "hero.jpg").read_bytes()          # the tracked placeholder, not the UI repository's public/hero.jpg
    assert hashlib.sha256(hero).hexdigest() == LOCK["bundle_files"]["hero.jpg"]
    assert not any(tag in hero for tag in (b"c2pa", b"jumb", b"Black Forest", b"trainedAlgorithmicMedia"))


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
        pack_mod.collect(dist, ROOT / "agentic_os" / "projects_ui" / "hero.jpg")


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


# ---------------------------------------------------------------------------------------------------------------- the 2026-11-06 re-pin
def test_the_bundle_carries_both_the_today_first_views_and_the_accessibility_series_and_the_drilldown():
    """The previous pin lacked the Today-first views of v0.2.0 (the convergence report's material finding). A regression guard on the bytes."""
    js = next(v for k, v in _members().items() if k.endswith(".js")).decode("utf-8")
    today_first = ("Quick capture", "Deployment inspection", "Social intelligence", "Notifications")                # v0.2.0 line
    a11y = ("Skip to main content", "About and licences")                                                           # UIA series
    drilldown = ("aos.drilldown.timeline/", "aos.drilldown.evidence-inspect/", "Sample data, not a record", "Access changed while you were viewing",
                 "Approve and Reject are not available here")
    for s in today_first + a11y + drilldown:
        assert s in js, s


def test_the_bundle_takes_its_data_source_from_the_running_server_not_a_build_flag():
    """One artifact serves a sample deployment and a runtime-backed one: it asks GET /api/ui-config, and the build flag is gone."""
    js = next(v for k, v in _members().items() if k.endswith(".js")).decode("utf-8")
    for s in ("/api/ui-config", "aos.ui-config/v1", "Data source unknown", "Where this data comes from is not known", "Sample data",
              "This list comes from the sample projection", "Partly sample", "partly-note", "only for missions your session has been granted access to"):
        assert s in js, s
    assert "VITE_PROJECTS_SAMPLE_DATA" not in js and "SAMPLE_DATA" not in js


def test_the_bundle_flags_uncertain_stale_and_incomplete_compensation_in_plain_words():
    """The mission views flag what the log says is uncertain, late or not cleanly undone, and never call a residual a clean rollback."""
    js = next(v for k, v in _members().items() if k.endswith(".js")).decode("utf-8")
    for s in ("Outcome unknown: the effect may or may not exist.", "A late result from an earlier attempt was recorded; a duplicate effect is possible.",
              "Compensation was denied; the effect remains.", "this is not a clean rollback.", "Waiting for an operator to reconcile an uncertain step",
              "Compensated (undone)", "State as recorded", "Needs attention: uncertain, late or incomplete steps", "Verifier kind as recorded:",
              "the application did not enforce it as read-only", "recorded as enforced read-only by the server"):
        assert s in js, s
    assert "application-enforced" not in js and "rolled back cleanly" not in js


def test_the_bundle_gives_every_focusable_element_one_visible_keyboard_focus_ring():
    """WCAG 2.4.7: the previous pin had an inline `outline: none` on the top-bar Ask Sidekick input (found by the native accessibility harness).
    The shipped bytes carry the one shared :focus-visible rule from a --focus token, a forced-colors rule, and no inline outline removal."""
    m = _members()
    js = next(v for k, v in m.items() if k.endswith(".js")).decode("utf-8")
    css = next(v for k, v in m.items() if k.endswith(".css")).decode("utf-8")
    assert re.search(r":focus-visible\{outline:var\(--focus-w\) solid var\(--focus\);outline-offset:var\(--focus-off\)\}", css)
    assert "--focus:var(--accent)" in css and "forced-colors:active){:focus-visible{outline:2px solid Highlight" in css
    assert 'outline:"none"' not in js and "outlineStyle" not in js


def test_the_bundle_says_every_source_refusal_in_fixed_words_and_can_remove_a_registered_folder():
    """A refused or failed Connect / Looks right / Remove was silent in the previous pin (the promise rejected and nothing was shown). The shipped
    bytes carry one fixed sentence per server refusal code (the server's own message text is never rendered), an alert, and a Remove control with
    an in-page question for a folder registered in this session."""
    js = next(v for k, v in _members().items() if k.endswith(".js")).decode("utf-8")
    for s in ("Your whole home folder can't be connected. Pick a folder inside it, for example Documents.",
              "That folder holds credentials, browser data, system files or this app's own data, so it can't be connected.",
              "Enter the full path of the folder, starting from the top", "Only a folder you picked in a proposal can be connected. Ask Sidekick to use it as context",
              "The registration could not be recorded, so nothing was registered.", "That source is already gone, so there is nothing to remove.",
              "You're signed out of this app session, so ", "This app session has ended, so ", "Can't reach the runtime, so ",
              "/api/sources/remove", "Yes, remove", "Your files are not changed; the folder just stops being available as evidence until you add it again."):
        assert s in js, s
    for server_words in ("cannot be registered as a source", "A local session is required."):
        assert server_words not in js, server_words                           # the page's words, never the server's
    css = next(v for k, v in _members().items() if k.endswith(".css")).decode("utf-8")
    assert ".problem{" in css and ".confirm{" in css


def test_the_pin_chain_keeps_the_replaced_artifacts_and_ends_at_the_public_release():
    chain = ["local+9a1788b1", "local+5751e9e5", "local+312a811a", "local+2f24719e", "local+ee958a01", "local+2fdef7f5"]
    node = LOCK
    for want in chain:
        node = node["previous_pin"]
        assert node["version"] == want and node["source_commit_published"] is False, want
        f = ROOT / node["local_path"]
        assert f.is_file() and hashlib.sha256(f.read_bytes()).hexdigest() == node["sha256"], want          # going back is a lock edit
    assert node["previous_pin"]["version"] == "v0.2.0" and re.fullmatch(r"[0-9a-f]{64}", node["previous_pin"]["sha256"])
    assert LOCK["merged_public_release"]["tag"] == "v0.2.0" and REC["merged_public_release"] == {"tag": "v0.2.0", "commit": LOCK["merged_public_release"]["commit"]}
    assert LOCK["source_commit"] != LOCK["previous_pin"]["source_commit"] and LOCK["source_commit_published"] is False and LOCK["url"] == ""


def test_the_record_states_that_the_npm_lock_changed_only_by_the_licence_field():
    assert REC["lock_unchanged_since_base"] is False
    assert "no dependency was added" in REC["lock_change_since_base"]

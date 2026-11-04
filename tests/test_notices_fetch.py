"""generate_notices.py: key lookup, first-party items, dist-info of first-party wheels; fetch_crate_sources.py:
checksum verification before extraction (offline: the network function is replaced)."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
N = ROOT / "packaging" / "notices"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, N / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


gen = _load("generate_notices")
fetch = _load("fetch_crate_sources")


def write_sbom(path, comps):
    path.write_text(json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.5", "components": comps}))
    return path


def test_entries_are_found_by_the_sbom_generators_key_and_first_party_items_are_listed_not_missing(tmp_path):
    sb = write_sbom(tmp_path / "s.json", [
        {"type": "library", "name": "react", "version": "18.3.1", "purl": "pkg:npm/react@18.3.1"},
        {"type": "library", "name": "Mozilla CA certificate bundle (certifi/cacert.pem)", "version": "2026.7.22",
         "purl": "pkg:pypi/certifi@2026.7.22", "properties": [{"name": "redevops:key", "value": "certifi-bundle"}]},
        {"type": "data", "name": "Projects UI bundle", "version": "0.2.4",
         "properties": [{"name": "redevops:key", "value": "ui-bundle"}]},
        {"type": "library", "name": "unknown-thing", "version": "1", "purl": "pkg:generic/unknown-thing@1"},
    ])
    recs, missing = gen.run(sb, [], [], gen.HERE / "texts", gen.HERE / "components.json", tmp_path / "out", "t", [])
    by = {r["name"]: r for r in recs}
    assert by["react"]["status"] == "text-found"
    assert by["Mozilla CA certificate bundle (certifi/cacert.pem)"]["status"] == "text-found"
    assert by["Projects UI bundle"]["status"] == "not-third-party"
    assert [m["name"] for m in missing] == ["unknown-thing"], "an unknown component is still a missing item"
    txt = (tmp_path / "out" / "THIRD_PARTY_NOTICES.txt").read_text()
    assert "Not a third-party notice: first-party" in txt and "TEXT NOT FOUND" in txt
    rep = json.loads((tmp_path / "out" / "notices-report.json").read_text())
    assert (rep["text_found"], rep["not_third_party"], rep["text_not_found"]) == (2, 1, 1)


def test_a_first_party_wheel_with_a_github_purl_reads_its_dist_info(tmp_path):
    site = tmp_path / "site"
    di = site / "runtime_contracts-0.3.4.dist-info" / "licenses"
    di.mkdir(parents=True)
    (di / "LICENSE").write_text("family licence text\n")
    sb = write_sbom(tmp_path / "s.json", [{
        "type": "library", "name": "runtime-contracts", "version": "0.3.4",
        "purl": "pkg:github/redevops-io/runtime-contracts@abc",
        "properties": [{"name": "redevops:key", "value": "pypi:runtime-contracts"}]}])
    recs, missing = gen.run(sb, [site], [], gen.HERE / "texts", gen.HERE / "components.json", tmp_path / "out", "t", [])
    assert recs[0]["status"] == "text-found" and not missing


def make_crate(files: dict[str, bytes], top="demo-1.0.0") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            ti = tarfile.TarInfo(f"{top}/{name}")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        evil = tarfile.TarInfo("../escape.txt")
        evil.size = 1
        tf.addfile(evil, io.BytesIO(b"x"))
    return buf.getvalue()


def test_a_crate_is_extracted_only_after_its_checksum_verifies(tmp_path, monkeypatch):
    crate = make_crate({"Cargo.toml": b"[package]\nname='demo'\n", "LICENSE-MIT": b"MIT text " * 40, "src/lib.rs": b"code"})
    good = hashlib.sha256(crate).hexdigest()
    monkeypatch.setattr(fetch, "get", lambda url, timeout=60: crate)
    ok = fetch.fetch_one("demo", "1.0.0", good, tmp_path / "ok", offline=False)
    assert ok["status"] == "verified"
    d = tmp_path / "ok" / "demo-1.0.0"
    assert (d / "LICENSE-MIT").is_file() and (d / "Cargo.toml").is_file()
    assert not (d / "src").exists(), "no code is extracted"
    assert not (tmp_path / "ok" / "escape.txt").exists() and not (tmp_path / "escape.txt").exists()
    bad = fetch.fetch_one("demo", "1.0.0", "0" * 64, tmp_path / "bad", offline=False)
    assert "CHECKSUM MISMATCH" in bad["status"] and not (tmp_path / "bad" / "demo-1.0.0").exists()
    off = fetch.fetch_one("demo", "1.0.0", good, tmp_path / "off", offline=True)
    assert off["status"].startswith("missing")


def test_checksum_falls_back_to_the_sparse_index_and_index_paths(monkeypatch, tmp_path):
    crate = make_crate({"Cargo.toml": b"x", "LICENSE": b"L" * 300})
    cks = hashlib.sha256(crate).hexdigest()

    def fake_get(url, timeout=60):
        if url.startswith("https://index.crates.io/"):
            return (json.dumps({"name": "demo", "vers": "0.9.0", "cksum": "1" * 64}) + "\n" +
                    json.dumps({"name": "demo", "vers": "1.0.0", "cksum": cks}) + "\n").encode()
        return crate
    monkeypatch.setattr(fetch, "get", fake_get)
    assert fetch.fetch_one("demo", "1.0.0", None, tmp_path, False)["verified_against"] == "crates.io index"
    assert [fetch.index_path(n) for n in ("a", "ab", "abc", "abcd", "serde")] == ["1/a", "2/ab", "3/a/abc", "ab/cd/abcd", "se/rd/serde"]

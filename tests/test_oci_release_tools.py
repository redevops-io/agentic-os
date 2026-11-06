"""scripts/release/oci_tools.py: the Dockerfile policy lint and the private-identifier layer scan (the release image content check).

The layer scan is exercised on a synthetic ``docker save`` style archive (no Docker needed): it must report a private address, a home
path, a host alias and an owner-supplied needle in a layer file, a key file name, and must stay quiet on clean content.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("oci_tools", ROOT / "scripts" / "release" / "oci_tools.py")
oci_tools = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oci_tools)
SCANNER = str(ROOT / "scripts" / "check_private_topology.py")


def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _archive(tmp_path, layers: list[dict[str, bytes]]) -> str:
    blobs: dict[str, bytes] = {}

    def put(data: bytes) -> str:
        d = hashlib.sha256(data).hexdigest()
        blobs[d] = data
        return "sha256:" + d

    layer_descs = [{"digest": put(_tar(f)), "mediaType": "application/vnd.oci.image.layer.v1.tar"} for f in layers]
    cfg = put(json.dumps({"config": {"Env": [], "User": "10001:10001"}, "history": []}).encode())
    man = put(json.dumps({"schemaVersion": 2, "config": {"digest": cfg}, "layers": layer_descs}).encode())
    out = tmp_path / "img.tar"
    with tarfile.open(out, "w") as t:
        def add(name, data):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
        add("index.json", json.dumps({"manifests": [{"digest": man}]}).encode())
        for d, data in blobs.items():
            add("blobs/sha256/" + d, data)
    return str(out)


def test_both_release_dockerfiles_pass_the_lint():
    for name in ("Dockerfile", "Dockerfile.projects"):
        r = oci_tools.cmd_lint(str(ROOT / name))
        assert r["all_bases_digest_pinned"] and r["has_user"] and r["has_healthcheck"], r
        assert not r["curl_pipe_or_latest_download"] and not r["copy_dot"], r
        assert r["oci_labels_set"] and r["forbidden_labels_present"] == [] and r["pip_removed"], r


def test_the_lint_flags_a_dockerfile_with_a_forbidden_label_and_a_floating_base(tmp_path):
    bad = tmp_path / "Dockerfile"
    bad.write_text('FROM python:3.12-slim\nCOPY . /app\nLABEL org.opencontainers.image.source="x"\n')
    r = oci_tools.cmd_lint(str(bad))
    assert not r["all_bases_digest_pinned"] and r["copy_dot"] and r["forbidden_labels_present"] == ["source"]
    assert not r["has_user"] and not r["has_healthcheck"] and not r["oci_labels_set"] and not r["pip_removed"]


def test_layer_scan_reports_private_identifiers_names_and_needles(tmp_path, monkeypatch):
    needles = tmp_path / "needles.txt"
    needles.write_text("ownerlogin\n")
    monkeypatch.setenv("OCI_EXTRA_NEEDLES", str(needles))
    # the offending literals are assembled at run time so this file itself stays clean for the repository's topology guard
    dirty = {
        "app/a.py": ("HOST = '10.77." + "3.9'\n").encode(),
        "app/b.md": ("see /Users/" + "someone/work for ownerlogin's notes\n").encode(),
        "app/c.sh": ("ssh prox" + "mox true\n").encode(),
        "app/.env": b"X=1\n",
        "usr/lib/id_rsa": b"not really a key\n",
    }
    r = oci_tools.cmd_layer_scan(_archive(tmp_path, [{"app/ok.txt": b"hello\n"}, dirty]), SCANNER)
    cats = {h["category"] for h in r["own_code_hits"]}
    assert {"ip-private", "home-path", "needle"} <= cats, r["own_code_hits"]
    assert any(h["path"] == "app/c.sh" for h in r["own_code_hits"])
    names = {n["path"] for n in r["secret_like_file_names"]}
    assert {"app/.env", "usr/lib/id_rsa"} <= names
    assert all(h["layer"] == 1 for h in r["own_code_hits"])


def test_layer_scan_is_quiet_on_clean_content(tmp_path, monkeypatch):
    monkeypatch.delenv("OCI_EXTRA_NEEDLES", raising=False)
    r = oci_tools.cmd_layer_scan(_archive(tmp_path, [{"app/ok.py": b"print('hello')\nHOST='127.0.0.1'\nNET='198.51.100.0/24'\n"}]), SCANNER)
    assert r["own_code_hits"] == [] and r["other_hit_counts"] == {} and r["secret_like_file_names"] == []


def test_layer_scan_sees_the_final_filesystem_not_deleted_files(tmp_path, monkeypatch):
    monkeypatch.delenv("OCI_EXTRA_NEEDLES", raising=False)
    dirty = ("HOST = '10.77." + "3.9'\n").encode()
    layers = [{"app/old.py": dirty, "usr/lib/id_rsa": b"x\n", "app/keep.py": b"print(1)\n"}, {"app/.wh.old.py": b"", "usr/lib/.wh.id_rsa": b""}]
    r = oci_tools.cmd_layer_scan(_archive(tmp_path, layers), SCANNER)
    assert r["own_code_hits"] == [] and r["secret_like_file_names"] == [] and r["files_in_final_fs"] == 1
    r2 = oci_tools.cmd_layer_scan(_archive(tmp_path, layers[:1]), SCANNER)          # without the deleting layer the same content is reported
    assert r2["own_code_hits"] and r2["secret_like_file_names"]


def test_public_ca_bundle_files_are_not_key_material(tmp_path, monkeypatch):
    monkeypatch.delenv("OCI_EXTRA_NEEDLES", raising=False)
    r = oci_tools.cmd_layer_scan(_archive(tmp_path, [{"etc/ssl/certs/ISRG_Root_X1.pem": b"-----BEGIN CERTIFICATE-----\n"}]), SCANNER)
    assert r["secret_like_file_names"] == []


def test_the_prepare_script_pins_the_release_identity_rules():
    s = (ROOT / "scripts" / "release" / "oci_prepare.sh").read_text()
    for needle in ("OCI_TAG commit part", "working tree is not clean", "--build-arg OCI_VERSION=", "OCI_REVISION=", "NAME_PREFIX"):
        assert needle in s, needle
    code = "\n".join(l for l in s.splitlines() if not l.lstrip().startswith("#"))
    assert "docker push" not in code and "ghcr.io/" not in code and "imagetools create" not in code and "--push" not in code

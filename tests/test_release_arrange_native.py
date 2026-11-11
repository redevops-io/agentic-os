"""Arranging native pack output for the release stages: layout, hashes, and the refusals that keep a tampered or foreign pack out."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from .about_fixture import ROOT

sys.path.insert(0, str(ROOT / "scripts" / "release"))
import arrange_native as an  # noqa: E402
import signing_package as sp  # noqa: E402

COMMIT = "c" * 40


def h(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def make_pack(tmp_path, platform="linux", *, same=True, name="pack", commit=COMMIT):
    pack = tmp_path / name
    pack.mkdir()
    files = {}
    about = {"build": {"mode": "distribution", "source": {"commit": commit, "dirty_files": 0}}, "legal": {"slots": {"a": {"state": "approved-verified"}}}}
    deliver = {"linux": {"app_0.6.0_arm64.deb": b"deb", "app_0.6.0_aarch64.AppImage": b"img"}, "windows": {"App_0.6.0_x64-setup.exe": b"setup", "App_0.6.0_x64_en-US.msi": b"msi"}}[platform]
    aname = an.ABOUT[platform]
    for i, b in enumerate(({"linux": "b1", "windows": "f1"}[platform], {"linux": "b2", "windows": "f2"}[platform])):
        d = pack / b
        d.mkdir()
        for n, data in deliver.items():
            data = data if (same or i == 0) else data + b"-2"
            (d / n).write_bytes(data)
            files[f"{b}/{n}"] = data
        (d / aname).write_text(json.dumps(about))
        files[f"{b}/{aname}"] = (d / aname).read_bytes()
        (d / "build.log").write_text("log " + b)
        files[f"{b}/build.log"] = (d / "build.log").read_bytes()
    (pack / "probes").mkdir()
    (pack / "probes" / "p").write_text("probe")
    (pack / "SHA256SUMS").write_text("".join(f"{h(v)}  ./{k}\n" for k, v in sorted(files.items())))
    return pack


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_arranged_layout_has_deliverables_about_record_and_a_verifiable_sha256sums(tmp_path, platform):
    pack = make_pack(tmp_path, platform)
    out = tmp_path / "out"
    cmp = an.arrange(platform, pack, out)
    assert cmp["all_identical"] is True and len(cmp["builds"]) == 2
    top = sorted(p.name for p in out.iterdir() if p.is_file())
    assert "about.json" in top and "SHA256SUMS" in top and "native-compare.json" in top
    assert any(n.endswith((".deb", ".msi")) for n in top)
    # the arranged folder is exactly what the release stages read
    sums = sp.parse_sums(out)
    assert all(sp.regular_inside(out, n) and sp.sha_f(out / n) == hh for n, hh in sums.items())
    wrap = tmp_path / "wrap"
    wrap.mkdir()
    os.symlink(out, wrap / platform)
    ev = json.loads(sp.evidence_manifest(wrap, COMMIT, "t" * 40, "a" * 64))
    assert [f["name"] for f in ev["platforms"][platform]] and all("/" not in f["name"] for f in ev["platforms"][platform])
    assert not any(s == "FAIL" for s, t in sp.about_findings(out, COMMIT))


def test_differences_between_the_two_builds_are_recorded_and_can_be_refused(tmp_path):
    pack = make_pack(tmp_path, same=False)
    cmp = an.arrange("linux", pack, tmp_path / "out")
    assert cmp["all_identical"] is False
    assert json.loads((tmp_path / "out/native-compare.json").read_text())["all_identical"] is False
    with pytest.raises(sp.Refused, match="builds differ"):
        an.arrange("linux", pack, tmp_path / "out2", require_identical=True)


def test_a_tampered_file_is_refused(tmp_path):
    pack = make_pack(tmp_path)
    (pack / "b1/app_0.6.0_arm64.deb").write_bytes(b"swapped")
    with pytest.raises(sp.Refused, match="does not match"):
        an.arrange("linux", pack, tmp_path / "out")
    assert not (tmp_path / "out").exists() and not (tmp_path / "out.building").exists()


def test_an_unlisted_file_in_a_build_folder_is_refused(tmp_path):
    pack = make_pack(tmp_path)
    (pack / "b1/extra.deb").write_bytes(b"x")
    with pytest.raises(sp.Refused, match="does not list"):
        an.arrange("linux", pack, tmp_path / "out")


def test_path_traversal_and_symlinks_in_the_pack_are_refused(tmp_path):
    pack = make_pack(tmp_path)
    s = pack / "SHA256SUMS"
    s.write_text(s.read_text() + f"{h(b'x')}  ./../escape\n")
    with pytest.raises(sp.Refused, match="refused"):
        an.arrange("linux", pack, tmp_path / "out")
    pack2 = make_pack(tmp_path, name="pack2")
    victim = pack2 / "b1/app_0.6.0_arm64.deb"
    victim.unlink()
    outside = tmp_path / "outside.deb"
    outside.write_bytes(b"deb")
    os.symlink(outside, victim)
    with pytest.raises(sp.Refused, match="symlink"):
        an.arrange("linux", pack2, tmp_path / "out2")


def test_a_pack_without_sums_or_about_or_deliverables_is_refused(tmp_path):
    pack = make_pack(tmp_path)
    (pack / "SHA256SUMS").unlink()
    with pytest.raises(sp.Refused, match="missing"):
        an.arrange("linux", pack, tmp_path / "o1")
    pack2 = make_pack(tmp_path, name="p2")
    (pack2 / "b1/about.json").unlink()
    s = pack2 / "SHA256SUMS"
    s.write_text("".join(ln for ln in s.read_text().splitlines(True) if "b1/about.json" not in ln))
    with pytest.raises(sp.Refused, match="no About record"):
        an.arrange("linux", pack2, tmp_path / "o2")
    pack3 = make_pack(tmp_path, name="p3")
    s = pack3 / "SHA256SUMS"
    s.write_text("".join(ln for ln in s.read_text().splitlines(True) if "b1/" not in ln or "about" in ln or "build.log" in ln))
    for f in (pack3 / "b1").glob("*.deb"):
        f.unlink()
    for f in (pack3 / "b1").glob("*.AppImage"):
        f.unlink()
    with pytest.raises(sp.Refused, match="no deliverable"):
        an.arrange("linux", pack3, tmp_path / "o3")


def test_builds_with_different_deliverable_names_are_refused(tmp_path):
    pack = make_pack(tmp_path)
    (pack / "b2/app_0.6.0_arm64.deb").rename(pack / "b2/app_9.9.9_arm64.deb")
    s = pack / "SHA256SUMS"
    s.write_text(s.read_text().replace("b2/app_0.6.0_arm64.deb", "b2/app_9.9.9_arm64.deb"))
    with pytest.raises(sp.Refused, match="other deliverable names"):
        an.arrange("linux", pack, tmp_path / "out")


def test_an_existing_output_or_a_leftover_is_never_overwritten(tmp_path):
    pack = make_pack(tmp_path)
    (tmp_path / "out").mkdir()
    with pytest.raises(sp.Refused, match="exists"):
        an.arrange("linux", pack, tmp_path / "out")
    (tmp_path / "out2.building").mkdir()
    with pytest.raises(sp.Refused, match="interrupted"):
        an.arrange("linux", pack, tmp_path / "out2")

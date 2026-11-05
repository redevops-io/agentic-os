"""Contract between the launcher About / Legal view and the build record `redevops.about/1` (RC3).

The record is produced by packaging/notices/generate_about.py (agent rc3about; design:
`ABOUT_LEGAL_UI_DESIGN.md` section 3.2). Until that generator is merged, the launcher is developed against the
fixture copy `tests/fixtures/launcher_about/Licenses/` (same shape, FIXTURE TEXT instead of licence texts). This
file pins what the launcher reads, so a generator change that breaks the view fails here:

  * `about.json`: schema, product.*, build.*, licence_files[] {file, label, bytes, sha256}, notices {file, bytes,
    sha256, components[] {name, version, declared, status, offset, length}}, legal.slots{id: {state, decisions}},
    source_access {state};
  * the six fixed file names `about_text` can open (Rust `TEXT_FILES`) are the only files the record lists;
  * sha256 and bytes of every listed file match the files; component offsets point at `[NNN] name version` headings;
  * ASCII only, <= 1 MiB, no absolute path, no logo/icon reference, no wording that implies legal clearance.

When a generated record exists (`packaging/notices/out/about.json`, written by the real pipeline) the same checks
run on it too, with that folder as the Licenses folder.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "launcher_about" / "Licenses"
ABOUT_RS = ROOT / "deploy" / "launcher" / "src-tauri" / "src" / "about.rs"
UI = ROOT / "deploy" / "launcher" / "ui"
FORBIDDEN = ("cleared", "compliant", "approved by counsel", "certified", "tamper-proof", "open source", "open-source")
SLOT_ID = re.compile(r"^[A-Z0-9_.]{1,64}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def rust_text_files() -> list[str]:
    src = ABOUT_RS.read_text(encoding="utf-8")
    body = re.search(r"pub const TEXT_FILES: \[&str; \d+\] = \[(.*?)\];", src, re.S).group(1)
    return re.findall(r'"([^"]+)"', body)


def check_record(rec: dict, licenses: Path) -> None:
    assert rec["schema"] == "redevops.about/1"
    p = rec["product"]
    for k in ("name", "version", "release_line", "bundle_identifier"):
        assert isinstance(p[k], str) and p[k], k
    assert isinstance(p["launcher"]["version"], str)
    assert rec["build"]["mode"] in ("private-candidate", "distribution")
    assert isinstance(rec["build"]["platform"], str)
    assert re.fullmatch(r"[0-9a-f]{40}", rec["build"]["source"]["commit"])
    allowed = rust_text_files()
    listed = []
    for f in rec["licence_files"]:
        assert f["file"] in allowed, f"{f['file']} is not one of the files the launcher can open"
        assert isinstance(f["label"], str) and isinstance(f["bytes"], int) and HEX64.match(f["sha256"])
        listed.append(f["file"])
    n = rec["notices"]
    assert n["file"] in allowed and HEX64.match(n["sha256"]) and isinstance(n["bytes"], int)
    listed.append(n["file"])
    assert len(listed) == len(set(listed))
    for name in listed:
        entry = next((f for f in rec["licence_files"] if f["file"] == name), n)
        data = (licenses / name).read_bytes()
        assert len(data) == entry["bytes"], name
        assert hashlib.sha256(data).hexdigest() == entry["sha256"], name
        data.decode("utf-8")
    notices = (licenses / n["file"]).read_bytes()
    for c in n["components"]:
        assert isinstance(c["name"], str) and isinstance(c["version"], str) and isinstance(c["declared"], str)
        assert c["status"] in ("text-found", "text-not-found", "not-third-party")
        if c["status"] == "text-found":
            assert isinstance(c["offset"], int) and isinstance(c["length"], int) and c["length"] > 0
            assert c["offset"] + c["length"] <= len(notices)
            section = notices[c["offset"] : c["offset"] + c["length"]].decode("utf-8")
            heading = re.match(r"={72}\n\[\d{3}\] (.+)\n", section)
            assert heading and heading.group(1) == f"{c['name']} {c['version']}", (c, section[:120])
    slots = rec["legal"]["slots"]
    assert slots, "legal.slots"
    for sid, s in slots.items():
        assert SLOT_ID.match(sid), sid
        assert s["state"] in ("placeholder", "filled")
        assert all(isinstance(d, str) for d in s.get("decisions", []))
        for opt in ("placeholder", "text"):
            assert isinstance(s.get(opt, ""), str)
    assert rec["source_access"]["state"] in ("not-published", "published")
    raw = json.dumps(rec)
    raw.encode("ascii")
    assert len(raw) <= 1024 * 1024
    for bad in ("/Users/", "/private/", "/home/", "C:\\\\", "wt-"):
        assert bad not in raw, bad
    low = raw.lower()
    for word in FORBIDDEN:
        assert word not in low, word
    assert not re.search(r"\.(png|ico|icns|svg)\b", low), "no logo or icon reference"


def test_fixture_follows_the_contract():
    rec = json.loads((FIXTURE / "about.json").read_text(encoding="utf-8"))
    check_record(rec, FIXTURE)


def test_fixture_legal_slots_are_all_placeholders_and_say_what_they_wait_for():
    rec = json.loads((FIXTURE / "about.json").read_text(encoding="utf-8"))
    for sid, s in rec["legal"]["slots"].items():
        assert s["state"] == "placeholder" and s["decisions"], sid
    assert rec["build"]["mode"] == "private-candidate" and rec["source_access"]["state"] == "not-published"


def test_the_six_names_are_the_shipped_licence_files():
    # LICENSING.md section 2 / the Licenses folder: these are the text files the launcher may show
    assert rust_text_files() == ["LICENSE", "LICENSE-COMMONS-CLAUSE", "LICENSE.md", "LICENSING.md", "THIRD_PARTY_NOTICES.txt", "NOTICES_MISSING.md"]
    assert sorted(p.name for p in FIXTURE.iterdir()) == sorted(rust_text_files() + ["about.json"])


def test_the_page_never_composes_a_path_or_url_and_never_claims_clearance():
    js = (UI / "app.js").read_text(encoding="utf-8")
    html = (UI / "index.html").read_text(encoding="utf-8")
    # the only argument the page passes to the file command is a name taken from the record
    assert re.findall(r'invoke\("about_text",\s*\{([^}]*)\}', js) == [" slug: file ", " slug: file "]
    assert "../" not in js and "/Licenses" not in js
    low = (js + html).lower()
    for word in FORBIDDEN:
        assert word not in low, word
    assert "private candidate" in low and "placeholder" in low


@pytest.mark.skipif(not (ROOT / "packaging" / "notices" / "out" / "about.json").is_file(), reason="no generated about.json in this tree")
def test_a_generated_record_follows_the_contract():
    out = ROOT / "packaging" / "notices" / "out"
    check_record(json.loads((out / "about.json").read_text(encoding="utf-8")), out)

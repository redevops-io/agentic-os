"""Distribution inventory: name, version, DECLARED licence metadata, top-level modules, wheel hash.

Sources (merged, later wins):
  from_wheels(dir)      the wheelhouse: exactly the wheels that were installed into the freeze venv, with
                        sha256 (the strongest evidence for the shipped Python components)
  from_environment(py)  importlib.metadata of a venv (reaches PyInstaller, setuptools, packaging, ...:
                        components that enter the archive without being in the wheelhouse)
  from_locks(files)     name==version only (no licence data): version oracle for listing-only checks

Run `python -I inventory.py --dump` inside a venv to print the environment inventory as JSON (stdlib only).
"""
from __future__ import annotations

import email.parser
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _licence_from_metadata(msg) -> dict:
    return {
        "expression": (msg.get("License-Expression") or "").strip() or None,
        "license": (msg.get("License") or "").strip() or None,
        "classifiers": [c for c in msg.get_all("Classifier") or [] if c.startswith("License ::")],
        "license_files": list(msg.get_all("License-File") or []),
    }


def _top_level_from_record(paths: list[str]) -> list[str]:
    tops: set[str] = set()
    for p in paths:
        p = p.replace("\\", "/")
        if ".dist-info/" in p or p.startswith(("..", "__pycache__")) or "/__pycache__/" in p:
            continue
        head = p.split("/", 1)[0]
        if "/" not in p:  # single-file module / extension
            head = re.sub(r"(\.cpython-\d+[a-z]*(-[a-z0-9_]+)?|\.cp\d+-\w+)?\.(py|so|pyd|dylib)$", "", head)
            head = re.sub(r"\.abi3$", "", head)
        if head and head not in ("bin", "scripts", "data"):
            tops.add(head)
    return sorted(tops)


def _entry(name: str, version: str, msg, record: list[str], top_level_txt: str | None, source: str, **extra) -> dict:
    top = [t.strip() for t in (top_level_txt or "").splitlines() if t.strip()] or _top_level_from_record(record)
    e = {
        "name": name, "version": version, "norm": norm(name), "source": source,
        "licence": _licence_from_metadata(msg),
        "top_level": top, "requires": list(msg.get_all("Requires-Dist") or []),
        "summary": msg.get("Summary"), "home_page": msg.get("Home-page"),
        "project_urls": list(msg.get_all("Project-URL") or []),
        "embedded_sboms": sorted(p for p in record if "/sboms/" in p.replace("\\", "/")),
        "license_files_in_dist": sorted(p for p in record if "/licenses/" in p.replace("\\", "/") or re.search(r"\.dist-info/(LICENSE|COPYING|NOTICE)", p.replace("\\", "/"))),
    }
    e.update(extra)
    return e


def from_wheels(wheel_dir: str | Path) -> dict[str, dict]:
    inv: dict[str, dict] = {}
    for w in sorted(Path(wheel_dir).glob("*.whl")):
        with zipfile.ZipFile(w) as z:
            names = z.namelist()
            meta = next(n for n in names if n.endswith(".dist-info/METADATA"))
            msg = email.parser.Parser().parsestr(z.read(meta).decode("utf-8", "replace"))
            rec_name = meta.replace("METADATA", "RECORD")
            record = [l.split(",")[0] for l in z.read(rec_name).decode().splitlines() if l] if rec_name in names else names
            tl = next((n for n in names if n.endswith(".dist-info/top_level.txt")), None)
            e = _entry(msg["Name"], msg["Version"], msg, record, z.read(tl).decode() if tl else None, "wheel",
                       wheel_file=w.name, wheel_sha256=hashlib.sha256(w.read_bytes()).hexdigest())
            e["metadata_sha256"] = hashlib.sha256(z.read(meta)).hexdigest()
        inv[e["norm"]] = e
    return inv


def dump_environment() -> dict[str, dict]:
    """Inventory of the CURRENT interpreter's site-packages (run inside the venv)."""
    from importlib import metadata

    inv: dict[str, dict] = {}
    for d in metadata.distributions():
        msg = d.metadata
        name = msg["Name"]
        if not name:
            continue
        record = [str(f) for f in (d.files or [])]
        e = _entry(name, msg["Version"], msg, record, d.read_text("top_level.txt"), "environment")
        du = d.read_text("direct_url.json")
        if du:
            e["direct_url"] = json.loads(du)
        inv[e["norm"]] = e
    return inv


def from_environment(python: str) -> dict[str, dict]:
    import subprocess

    out = subprocess.run([python, "-I", str(Path(__file__).resolve()), "--dump"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout
    return json.loads(out)


def parse_lock(path: str | Path) -> dict[str, dict]:
    inv: dict[str, dict] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s\;]+)", line.strip())
        if m:
            inv[norm(m.group(1))] = {"name": m.group(1), "version": m.group(2), "norm": norm(m.group(1)), "source": "lock",
                                     "licence": {}, "top_level": [], "requires": []}
    return inv


def merge(*invs: dict[str, dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for inv in invs:
        for k, v in inv.items():
            if k in out and out[k]["version"] == v["version"]:
                # keep richer data: fill what the earlier entry lacks
                for f, val in v.items():
                    if val and not out[k].get(f):
                        out[k][f] = val
                    elif f == "top_level" and val:
                        out[k][f] = sorted(set(out[k][f]) | set(val))
            else:
                out[k] = dict(v)
    return out


def module_owner_map(inv: dict[str, dict]) -> dict[str, str]:
    """top-level module name -> normalized dist name"""
    m: dict[str, str] = {}
    for k, v in inv.items():
        for t in v.get("top_level", []):
            m.setdefault(t, k)
    return m


if __name__ == "__main__":
    if "--dump" in sys.argv:
        print(json.dumps(dump_environment()))
    else:
        print(__doc__)

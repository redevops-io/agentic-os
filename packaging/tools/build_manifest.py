#!/usr/bin/env python3
"""Write / extend the build manifest JSON: what produced an artifact, from which inputs.

The manifest records facts about the BUILD, never secrets: tool versions, hashes of every pin / lock /
script that steers the build, the git commit and tree of the source, the CI runner identity (the
runner image VERSION floats even when the image NAME is pinned, so it is recorded here), the
wheelhouse contents with hashes, and the artifacts produced. Stages call it repeatedly; each call
merges into the same file.

  build_manifest.py --root REPO --out packaging/dist/build-manifest.json \
      [--wheelhouse packaging/wheelhouse] [--artifact sidecar=packaging/dist/redevops-sidecar] \
      [--cmd rustc="rustc -Vv"] [--stage sidecar]

Run it with the build venv's python so the Python build tools are read from that environment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path

SCHEMA = "redevops.build-manifest/1"

INPUT_GLOBS = [
    "pyproject.toml", "packaging/toolchain.env", "packaging/git-pins.json", "packaging/requirements.in",
    "packaging/build-tools.in", "packaging/build-backends.in", "packaging/pip.in", "packaging/locks/*",
    "packaging/redevops-sidecar.spec", "packaging/sidecar_main.py", "packaging/build-wheelhouse.sh",
    "packaging/build-sidecar.sh", "packaging/build-launcher.sh", "packaging/build-windows.ps1",
    "packaging/stage-sidecar.sh", "packaging/tools/*.py", "packaging/cargo-targets/*",
    "deploy/launcher/src-tauri/Cargo.toml", "deploy/launcher/src-tauri/Cargo.lock",
    "deploy/launcher/src-tauri/tauri.conf.json", "deploy/launcher/src-tauri/build.rs", "deploy/launcher/src-tauri/Info.plist",
    "packaging/identity.json",
    "deploy/launcher/src-tauri/src/*.rs", "deploy/launcher/rust-toolchain.toml",
    ".github/workflows/native-packaging.yml",
]
PY_DISTS = ["pip", "setuptools", "wheel", "hatchling", "pyinstaller", "pyinstaller-hooks-contrib",
            "altgraph", "macholib", "pefile", "pywin32-ctypes", "packaging"]
RUNNER_ENV = ["ImageOS", "ImageVersion", "RUNNER_OS", "RUNNER_ARCH", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
              "GITHUB_SHA", "GITHUB_REF", "GITHUB_REPOSITORY", "GITHUB_WORKFLOW", "GITHUB_ACTIONS"]


_MSYS_DRIVE = re.compile(r"^/([A-Za-z])(/.*)?$")


def native_path(path: str, windows: bool | None = None) -> str:
    """git-bash passes `name=/c/a/b` (an argument that does not start with `/`) to a native Windows Python UNCONVERTED;
    open('/c/a/b') then names `<current drive>:\\c\\a\\b`. Convert a leading `/<drive>/...` to `<drive>:/...` on Windows only
    (a path that exists as given is left alone)."""
    if windows is None:
        windows = os.name == "nt"
    m = _MSYS_DRIVE.match(path)
    if windows and m and not os.path.exists(path):
        return f"{m.group(1).upper()}:{m.group(2) or '/'}"
    return path


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def run(cmd: str, cwd: Path | None = None) -> str | None:
    try:
        r = subprocess.run(shlex.split(cmd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (r.stdout or r.stderr).strip()
    return out if r.returncode == 0 and out else None


def git_info(root: Path) -> dict:
    g = lambda *a: run("git " + " ".join(a), root)  # noqa: E731
    status = g("status", "--porcelain")
    return {
        "commit": g("rev-parse", "HEAD"), "tree": g("rev-parse", "HEAD^{tree}"),
        "describe": g("describe", "--tags", "--always", "--dirty"),
        "commit_time_epoch": g("show", "-s", "--format=%ct", "HEAD"),
        "dirty_files": len(status.splitlines()) if status else 0,
        "remote_origin": g("config", "--get", "remote.origin.url"),
    }


def release_identity(root: Path) -> tuple[str | None, dict | None]:
    """The release line and the identity of the artifact this build produces (packaging/identity.json), so a manifest says
    whether it describes the R0-prime line or not. None/None when the file is absent (an older tree)."""
    f = root / "packaging" / "identity.json"
    if not f.is_file():
        return None, None
    d = json.loads(f.read_text(encoding="utf-8"))
    return d.get("release_line"), {"schema": d.get("schema"), **d.get("r0prime", {})}


def input_hashes(root: Path) -> dict:
    out: dict[str, str | None] = {}
    for pat in INPUT_GLOBS:
        hits = sorted(root.glob(pat))
        if not hits and "*" not in pat:
            out[pat] = None
        for p in hits:
            if p.is_file():
                out[p.relative_to(root).as_posix()] = sha256_file(p)
    return out


def python_identity() -> dict:
    import ssl
    exe = Path(os.path.realpath(sys.executable))
    d = {
        "version": platform.python_version(), "implementation": platform.python_implementation(),
        "build": " ".join(platform.python_build()), "compiler": platform.python_compiler(),
        "executable": str(exe), "executable_sha256": sha256_file(exe) if exe.is_file() else None,
        "base_prefix": sys.base_prefix, "openssl": ssl.OPENSSL_VERSION,
        "pbs_marker": "python-build-standalone" if Path(sys.base_prefix, "lib", "libpython3.12.dylib").exists()
        and "uv" in sys.base_prefix else None,
        "source": ("uv python-build-standalone" if "/uv/python/" in sys.base_prefix.replace("\\", "/")
                   else "runner/system interpreter (python.org framework or hostedtoolcache)"),
    }
    d["dists"] = {}
    for n in PY_DISTS:
        try:
            d["dists"][n] = metadata.version(n)
        except metadata.PackageNotFoundError:
            pass
    return d


def wheelhouse_info(wh: Path) -> list[dict]:
    rows = []
    for w in sorted(wh.glob("*.whl")):
        parts = w.name[:-4].split("-")
        rows.append({"file": w.name, "name": parts[0], "version": parts[1], "sha256": sha256_file(w), "size": w.stat().st_size})
    return rows


def projects_ui_info(root: Path, wh: Path) -> dict | None:
    """The pinned Projects UI as it is PACKED: the bundle files of the agentic_os wheel in `wh`, compared with
    packaging/projects_ui.lock.json (the same verification generate_about.py applies to the About record). None when nothing is pinned
    or the wheelhouse holds no agentic_os wheel; ValueError when the bytes differ from the pin."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_about_gen", Path(__file__).resolve().parent.parent / "notices" / "generate_about.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    lock = gen.load_ui_lock(root)
    wheels = sorted(wh.glob("agentic_os-*.whl"))
    if lock is None or not wheels:
        return None
    if len(wheels) > 1:
        raise ValueError("more than one agentic_os wheel in " + str(wh))
    files, marker = gen.ui_files_from_wheel(wheels[0])
    problems = gen.verify_ui_pin(lock, files, marker, root)
    if problems:
        raise ValueError("Projects UI bundle in " + wheels[0].name + " does not match packaging/projects_ui.lock.json: " + "; ".join(problems[:6]))
    return {**gen.ui_pin_record(lock, "wheel"), "wheel": wheels[0].name,
            "lock_sha256": sha256_file(root / "packaging" / "projects_ui.lock.json"),
            "bundle_files": {p: files[p][1] for p in sorted(files)}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--stage", help="stage name recorded with a timestamp, e.g. wheelhouse|sidecar|launcher")
    ap.add_argument("--record-python", action="store_true", help="record THIS interpreter (the build venv) as the build python")
    ap.add_argument("--wheelhouse")
    ap.add_argument("--git-wheels", help="JSON written by build_git_wheels.py")
    ap.add_argument("--artifact", action="append", default=[], help="name=path")
    ap.add_argument("--cmd", action="append", default=[], help='label="command args" (tool version probes)')
    ap.add_argument("--duration", action="append", default=[], help="stage=seconds")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    out = Path(a.out)
    m = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    m["schema"] = SCHEMA
    m["generated_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    m["source"] = git_info(root)
    m["release_line"], m["identity"] = release_identity(root)
    m["inputs_sha256"] = input_hashes(root)
    tc = m.setdefault("toolchain", {})
    if a.record_python:
        tc["python_" + (a.stage or "build")] = python_identity()
    for spec in a.cmd:
        label, _, cmd = spec.partition("=")
        tc[label] = run(cmd.strip('"'))
    m["runner"] = {"platform": platform.platform(), "machine": platform.machine(), "system": platform.system(),
                   "env": {k: os.environ[k] for k in RUNNER_ENV if k in os.environ},
                   "note": "The image NAME is pinned in the workflow; the image VERSION (ImageVersion) is not controllable and is recorded here."}
    if a.wheelhouse:
        m["wheelhouse"] = wheelhouse_info(Path(a.wheelhouse))
        try:
            ui = projects_ui_info(root, Path(a.wheelhouse))
        except (OSError, ValueError, KeyError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        if ui is not None:
            m["projects_ui"] = ui
    if a.git_wheels and Path(a.git_wheels).exists():
        m["first_party_git_wheels"] = json.loads(Path(a.git_wheels).read_text(encoding="utf-8"))["git_wheels"]
    arts = m.setdefault("artifacts", {})
    missing = []
    for spec in a.artifact:
        name, _, path = spec.partition("=")
        p = Path(native_path(path))
        if p.is_file():
            arts[name] = {"path": str(p), "sha256": sha256_file(p), "size": p.stat().st_size}
        elif p.is_dir():
            files = {f.relative_to(p).as_posix(): sha256_file(f) for f in sorted(p.rglob("*")) if f.is_file()}
            arts[name] = {"path": str(p), "files": len(files), "tree_sha256": hashlib.sha256(
                "\n".join(f"{k} {v}" for k, v in files.items()).encode()).hexdigest(),
                "sha256_by_file": files}   # the installers (.msi / .exe / .dmg / .deb) are individually recorded, not only as a tree
        else:
            arts[name] = {"path": str(p), "missing": True}
            missing.append(f"{name}={path}")
    if a.stage:
        m.setdefault("stages", {})[a.stage] = {"completed_utc": m["generated_at_utc"]}
    for d in a.duration:
        k, _, v = d.partition("=")
        m.setdefault("durations_seconds", {})[k] = float(v)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(m, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if missing:
        # An artifact that was named but is not there must stop the build: a manifest without the hash of the sidecar or the
        # launcher is not evidence (and on Windows it is the symptom of an unconverted git-bash path).
        print("ERROR: artifact path(s) not found: " + ", ".join(missing), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

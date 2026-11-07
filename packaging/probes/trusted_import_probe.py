"""Exercise agentic_os.trusted_import against planted packages, unfrozen or inside a PyInstaller build (D-NEW-3).

    trusted_import_probe.py [WORKDIR]       prints one JSON object on stdout; exit 0 only if every guarantee held

trusted_import decides whether a top-level package (context_runtime, context_runtime_enterprise, agent_harness) is provided by an
INSTALLED distribution before anything imports it. This probe plants, in a directory that is first on sys.path AND the working
directory, a package that writes a marker file when its __init__ runs, with a forged RECORD-form dist-info next to it and a second
package with a forged editable-form dist-info. Guarantees checked (the same as tests/test_bb01_forged_dist_info.py and
tests/test_dr02_enterprise_import_trust.py check unfrozen):

  * package_status of every planted package is "untrusted" (never "trusted"),
  * import_trusted of it returns None,
  * no planted __init__ ever ran (the marker files do not exist).

packaging/probes/build-trusted-import-probe.sh freezes this file with the SAME spec as the sidecar (same excludes, same hidden
imports), so what it prints is what trusted_import does inside the shipped binary. tests/test_frozen_trusted_import.py runs it.
Standard library plus agentic_os.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

PLANTED = ("context_runtime_enterprise", "context_runtime", "agent_harness")


def _plant(attack: Path, marker_dir: Path) -> None:
    for i, name in enumerate(PLANTED):
        pkg = attack / name
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(f"open({str(marker_dir / ('MARKER-' + name))!r}, 'w').write('executed')\n")
        (pkg / "apps.py").write_text(f"open({str(marker_dir / ('MARKER-' + name + '-apps'))!r}, 'w').write('executed')\n")
        info = attack / f"{name}-1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
        (info / "top_level.txt").write_text(name + "\n")
        (info / "RECORD").write_text(f"{name}/__init__.py,,\n")
        if i == 2:        # the editable form: direct_url.json naming the planted directory as the project
            (info / "direct_url.json").write_text(json.dumps({"url": "file://" + str(attack), "dir_info": {"editable": True}}))


def _site_origin():
    m = sys.modules.get("site")
    if m is None:
        return None
    spec = getattr(m, "__spec__", None)
    origin = getattr(spec, "origin", None)
    if origin == "frozen":
        return "frozen"
    loader = type(getattr(m, "__loader__", None)).__name__
    return "archive" if "Pyi" in loader else "file"


def run(workdir: Path) -> dict:
    from agentic_os import trusted_import as ti

    attack = workdir / "attack"
    markers = workdir / "markers"
    markers.mkdir(parents=True)
    _plant(attack, markers)
    sys.path.insert(0, str(attack))
    sys.path.insert(0, "")
    old = os.getcwd()
    os.chdir(attack)
    try:
        import importlib
        importlib.invalidate_caches()
        status = {n: ti.package_status(n) for n in PLANTED}
        imported = {n + ".apps": (ti.import_trusted(n + ".apps") is None) for n in PLANTED}
        ran = sorted(p.name for p in markers.iterdir())
        dirs = [str(d) for d in ti._trusted_dirs()]
    finally:
        os.chdir(old)
    out = {
        "frozen": bool(getattr(sys, "frozen", False)),
        "site_loaded": "site" in sys.modules and sys.modules["site"] is not None,
        # where `site` came from: "frozen" = CPython's own built-in frozen module (3.11+), "file" = a site.py on disk, "archive" = a
        # module inside the PyInstaller archive (what the sidecar spec excludes), None = not loaded
        "site_origin": _site_origin(),
        "trusted_dirs_exist": all(Path(d).is_dir() for d in dirs),
        "trusted_dirs_count": len(dirs),
        "planted_status": status,
        "planted_import_refused": imported,
        "planted_code_ran": ran,
    }
    out["ok"] = (all(s in ("untrusted", "absent") for s in status.values()) and all(imported.values()) and not ran
                 and out["trusted_dirs_exist"])
    return out


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        res = run(Path(argv[0]))
    else:
        with tempfile.TemporaryDirectory() as d:
            res = run(Path(d))
    sys.stdout.write(json.dumps(res, sort_keys=True) + "\n")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

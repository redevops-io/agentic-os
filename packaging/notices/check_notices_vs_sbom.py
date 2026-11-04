#!/usr/bin/env python3
"""Check that the staged Licenses folder describes exactly the components of the built artifact's SBOM.

  check_notices_vs_sbom.py --sbom packaging/dist/sbom/sbom.cdx.json \
        --notices-report packaging/notices/out/notices-report.json \
        --licenses-dir deploy/launcher/src-tauri/licenses [--exclude NAME]...

build-all.sh runs this after build-sbom.sh (the SBOM of the FINAL artifacts: sidecar archive + launcher
binary + crate graph). The notices are generated earlier, from the SBOM of the pass-1 sidecar archive (they
must exist before the launcher is built), so this is the closing check that nothing was added or dropped in
between. It fails (exit 1) when
  * a component of the final SBOM has no record in the notices report (unlisted: no text, no missing-item),
  * the notices report has a record that is not in the final SBOM (phantom),
  * a version differs between the two,
  * the staged THIRD_PARTY_NOTICES.txt / NOTICES_MISSING.* do not match LICENSES-MANIFEST.json or the report
    (the folder was edited after it was generated, or it is stale).
It does not judge licences. Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _gen():
    spec = importlib.util.spec_from_file_location("generate_notices", HERE / "generate_notices.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(sbom: Path, report: Path, licenses_dir: Path | None, exclude: list[str]) -> list[str]:
    gen = _gen()
    errors: list[str] = []
    excl = {gen.norm(e) for e in exclude}
    sbom_c = {(c["type"], gen.norm(c["name"])): c for c in gen.load_components(sbom) if gen.norm(c["name"]) not in excl}
    rep = json.loads(report.read_text(encoding="utf-8"))
    rep_c = {(r["type"], gen.norm(r["name"])): r for r in rep["records"]}
    for key, c in sorted(sbom_c.items()):
        r = rep_c.get(key)
        if r is None:
            errors.append(f"in the final SBOM but not in the notices: {key[0]}:{c['name']} {c['version']}")
        elif r["version"] != c["version"]:
            errors.append(f"version differs for {key[0]}:{c['name']}: SBOM {c['version']!r}, notices {r['version']!r}")
    for key, r in sorted(rep_c.items()):
        if key not in sbom_c:
            errors.append(f"in the notices but not in the final SBOM: {key[0]}:{r['name']} {r['version']}")
    if licenses_dir is not None:
        man_path = licenses_dir / "LICENSES-MANIFEST.json"
        if not man_path.is_file():
            errors.append(f"{man_path} missing")
        else:
            files = json.loads(man_path.read_text(encoding="utf-8"))["files"]
            for name, meta in files.items():
                p = licenses_dir / name
                if not p.is_file():
                    errors.append(f"{name} listed in LICENSES-MANIFEST.json but absent")
                elif hashlib.sha256(p.read_bytes()).hexdigest() != meta["sha256"]:
                    errors.append(f"{name} differs from LICENSES-MANIFEST.json (edited after staging?)")
            notices = licenses_dir / "THIRD_PARTY_NOTICES.txt"
            if notices.is_file() and f"sha256 {rep['sbom_sha256']}" not in notices.read_text(encoding="utf-8"):
                errors.append("THIRD_PARTY_NOTICES.txt was not generated from the SBOM named in notices-report.json")
            mj = licenses_dir / "NOTICES_MISSING.json"
            if mj.is_file():
                staged_missing = sorted((m["type"], gen.norm(m["name"]), m["version"]) for m in json.loads(mj.read_text(encoding="utf-8")))
                rep_missing = sorted((r["type"], gen.norm(r["name"]), r["version"]) for r in rep["records"] if r["status"] != "text-found")
                if staged_missing != rep_missing:
                    errors.append("staged NOTICES_MISSING.json does not match the notices report")
    return errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sbom", required=True, type=Path)
    ap.add_argument("--notices-report", required=True, type=Path)
    ap.add_argument("--licenses-dir", type=Path)
    ap.add_argument("--exclude", action="append", default=[])
    a = ap.parse_args(argv)
    errs = check(a.sbom, a.notices_report, a.licenses_dir, a.exclude)
    for e in errs:
        print("FAIL:", e, file=sys.stderr)
    print("notices match the SBOM" if not errs else f"{len(errs)} problem(s)")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())

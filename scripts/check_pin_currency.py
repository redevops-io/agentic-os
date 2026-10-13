#!/usr/bin/env python3
"""Cross-repo pin-currency check (stdlib only; shells out to the `gh` CLI).

For each repo, fetch its dependency pins on the DEFAULT branch via `gh api`, compare every
`redevops-io/<repo>@<ref>` pin to that repo's latest release tag, print the matrix, and exit
non-zero when a security-relevant pin (default: agentic-os, runtime-contracts, mission-sdk)
is behind by more than --max-behind releases (default 0).

"Behind" = number of release tags (vX.Y.Z, pre-releases excluded) newer than the pinned tag.
Pins by SHA/branch cannot be ordered; they are shown as `ref` and only fail with --strict-refs.

Usage:
  python scripts/check_pin_currency.py                       # default repo list
  python scripts/check_pin_currency.py redevops-io/learnerbot accounting-firm
  python scripts/check_pin_currency.py --max-behind 1 --json out.json --markdown out.md

Read-only: only GET requests through `gh api`; needs GH_TOKEN/gh auth with read access
(private repos are reported as `unreadable`, not as failures).
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys

ORG = "redevops-io"
DEFAULT_REPOS = [
    "wealth-manager", "accounting-firm", "learnerbot", "mission-sdk", "RAAAL",
    "decision-intelligence", "intelligence-gateway", "redevops-integrations",
    "edge-diagnosis", "redevops-conformance", "rag-saas-platform", "context-runtime",
    "agentic-os-stack",
]
# Repos whose pins are tracked (columns of the matrix) and the subset that gates the exit code.
TRACKED = ["agentic-os", "runtime-contracts", "mission-sdk", "redevops-rag",
           "knowledge-frontier", "redevops-connectors", "context-runtime"]
SECURITY = ["agentic-os", "runtime-contracts", "mission-sdk"]
# Files that carry pins.
PIN_FILE = re.compile(
    r"(^|/)(pyproject\.toml|requirements[^/]*\.txt|Dockerfile[^/]*|setup\.cfg|package\.json|"
    r"[^/]*\.ya?ml|[^/]*\.toml)$")
SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def gh(path: str):
    p = subprocess.run(["gh", "api", "--paginate", path], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip().splitlines()[-1] if p.stderr.strip() else "gh failed")
    out = p.stdout.strip()
    # --paginate concatenates JSON arrays as `][` on some gh versions
    return json.loads(out.replace("][", ","))


def semver(tag: str):
    m = SEMVER.match(tag)
    return tuple(int(x) for x in m.groups()) if m else None


def release_tags(repo: str) -> list[str]:
    tags = [t["name"] for t in gh(f"repos/{ORG}/{repo}/tags?per_page=100")]
    return sorted((t for t in tags if semver(t)), key=semver)


def pins_for(repo: str):
    """Return ({tracked_repo: {ref,...}}, default_branch)."""
    full = repo if "/" in repo else f"{ORG}/{repo}"
    branch = gh(f"repos/{full}")["default_branch"]
    tree = gh(f"repos/{full}/git/trees/{branch}?recursive=1")["tree"]
    found: dict[str, set[str]] = {}
    pat = re.compile(rf"{ORG}/({'|'.join(map(re.escape, TRACKED))})(?:\.git)?(?:/archive/refs/tags/)?[@/]"
                     r"(?:refs/tags/)?(v?\d+\.\d+\.\d+(?:[-.]\w+)*|[0-9a-f]{7,40}|[A-Za-z][\w./-]*)")
    for node in tree:
        if node["type"] != "blob" or not PIN_FILE.search(node["path"]) or node.get("size", 0) > 500_000:
            continue
        if "node_modules/" in node["path"]:
            continue
        blob = gh(f"repos/{full}/git/blobs/{node['sha']}")
        text = base64.b64decode(blob["content"]).decode("utf-8", "replace")
        for line in text.splitlines():
            code = line.split(" #")[0] if not line.lstrip().startswith("#") else ""
            for m in pat.finditer(code):
                name, ref = m.group(1), m.group(2).removesuffix(".tar.gz").removesuffix(".zip")
                if full.endswith("/" + name):
                    continue
                if ref in ("main", "master") and "/blob/" in line:
                    continue
                found.setdefault(name, set()).add(ref)
    return found, branch


def classify(name: str, ref: str, latest: dict[str, list[str]]):
    """-> (cell, behind|None)."""
    tags = latest.get(name, [])
    if semver(ref):
        behind = sum(1 for t in tags if semver(t) > semver(ref))
        return (f"{ref}" if not behind else f"{ref} (-{behind})"), behind
    return f"{ref[:9]}*", None  # SHA or branch: unorderable


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repos", nargs="*", help="repos (name or owner/name); default: built-in list")
    ap.add_argument("--max-behind", type=int, default=0,
                    help="allowed releases behind for security repos (default 0)")
    ap.add_argument("--security", default=",".join(SECURITY), help="comma list of gating pin targets")
    ap.add_argument("--strict-refs", action="store_true", help="also fail on SHA/branch pins of security repos")
    ap.add_argument("--json", help="write the matrix as JSON")
    ap.add_argument("--markdown", help="write the matrix as Markdown")
    a = ap.parse_args()
    repos = a.repos or DEFAULT_REPOS
    security = set(a.security.split(","))

    latest = {n: release_tags(n) for n in TRACKED}
    rows, failures = [], []
    for repo in repos:
        short = repo.split("/")[-1]
        try:
            pins, branch = pins_for(repo)
        except Exception as e:  # private / missing / rate-limited: report, do not gate
            rows.append({"repo": short, "branch": "?", "error": str(e), "cells": {}})
            continue
        cells = {}
        for name, refs in sorted(pins.items()):
            parts = []
            for ref in sorted(refs):
                cell, behind = classify(name, ref, latest)
                parts.append(cell)
                if name in security and ((behind is not None and behind > a.max_behind)
                                         or (behind is None and a.strict_refs)):
                    failures.append(f"{short}: {name} pinned {ref}, latest {latest[name][-1] if latest[name] else '?'}"
                                    + (f" ({behind} behind > {a.max_behind})" if behind is not None else " (unorderable ref)"))
            cells[name] = " / ".join(parts)
        rows.append({"repo": short, "branch": branch, "cells": cells})

    cols = [n for n in TRACKED if any(n in r["cells"] for r in rows)]
    head = ["repo", "branch"] + cols
    table = [head, ["---"] * len(head),
             ["latest", ""] + [(latest[c][-1] if latest[c] else "-") for c in cols]]
    for r in rows:
        table.append([r["repo"], r["branch"]] + [r["cells"].get(c, "-") if not r.get("error") else "unreadable"
                                                   for c in cols])
    widths = [max(len(row[i]) for row in table) for i in range(len(head))]
    for row in table:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip())
    print("\n(-N) = N releases behind latest; * = SHA/branch pin (unorderable); - = no pin")
    for r in rows:
        if r.get("error"):
            print(f"note: {r['repo']}: {r['error']}", file=sys.stderr)

    if a.markdown:
        with open(a.markdown, "w") as f:
            f.write("\n".join("| " + " | ".join(row) + " |" for row in table) + "\n")
            f.write("\n`(-N)` = N releases behind latest; `*` = SHA/branch pin; `-` = no pin\n")
            if failures:
                f.write("\n**Out of date (security-relevant):**\n" + "\n".join(f"- {x}" for x in failures) + "\n")
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"latest": {k: (v[-1] if v else None) for k, v in latest.items()},
                       "rows": rows, "failures": failures, "max_behind": a.max_behind}, f, indent=2)
    if failures:
        print("\nSTALE security pins:", file=sys.stderr)
        for x in failures:
            print("  " + x, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

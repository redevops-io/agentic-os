#!/usr/bin/env python3
"""The ONE source of truth for which Python distributions are first-party: packaging/git-pins.json (+ agentic-os itself).

Why (D-NEW-1): three build scripts, four Python tools and a test each kept their own hand-written list of the first-party
wheels. git-pins.json grew a fifth (discovery-runtime) and the lists did not, so the pristine 0.6.0-rc1 tree failed
`pip check` in build-wheelhouse.sh step 4. Everything now derives the set from here, and a mismatch between the pins and
the wheelhouse stops the build with a message naming the package.

  first_party.py names
  first_party.py wheels  --wheelhouse DIR --lock requirements-<os>.lock     # one wheel FILE NAME per line (first-party only)
  first_party.py summary --wheelhouse DIR --lock requirements-<os>.lock     # "26 wheels = 21 third-party + 5 first-party"

Exit 1, one message on stderr and nothing on stdout when: a pin has no wheel, a pin has more than one wheel, a wheel in the
wheelhouse is neither a first-party pin nor a locked third-party pin ("no pin"), or a locked third-party wheel is absent.
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PINS = HERE.parent / "git-pins.json"
# The checkout itself is built into a wheel by build_project_wheel.py; it is not a git pin.
THIS_CHECKOUT = "agentic-os"


class Inconsistent(Exception):
    """git-pins.json, the lock and the wheelhouse disagree."""


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def first_party_names(pins_path: Path | str | None = None) -> frozenset[str]:
    data = json.loads(Path(pins_path or PINS).read_text(encoding="utf-8"))
    return frozenset({THIS_CHECKOUT} | {norm(d["name"]) for d in data["dependencies"]})


def lock_names(lock_path: Path | str) -> frozenset[str]:
    """Normalised distribution names pinned (name==version) in a hash lock."""
    out = set()
    for line in Path(lock_path).read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)==\S+", line)
        if m:
            out.add(norm(m.group(1)))
    return frozenset(out)


def dist_of(wheel_filename: str) -> str:
    """Distribution name of a wheel file name (the first '-'-separated field, normalised)."""
    return norm(Path(wheel_filename).name.split("-")[0])


def select_first_party(wheel_names, first_party, third_party) -> list[str]:
    """The first-party wheel file names, or Inconsistent naming every disagreement."""
    by_dist: dict[str, list[str]] = {}
    for w in wheel_names:
        by_dist.setdefault(dist_of(w), []).append(Path(w).name)
    problems = []
    for n in sorted(first_party):
        if n not in by_dist:
            problems.append(f"first-party pin '{n}' has no wheel in the wheelhouse")
        elif len(by_dist[n]) > 1:
            problems.append(f"first-party pin '{n}' has {len(by_dist[n])} wheels: {', '.join(sorted(by_dist[n]))}")
    for n in sorted(third_party):
        if n not in by_dist:
            problems.append(f"locked third-party wheel '{n}' is missing from the wheelhouse")
    for n in sorted(by_dist):
        if n not in first_party and n not in third_party:
            problems.append(f"distribution '{n}' (wheel {sorted(by_dist[n])[0]}) has no pin (not in packaging/git-pins.json, not in the lock)")
    if problems:
        raise Inconsistent("; ".join(problems) + " (first-party set: packaging/git-pins.json plus agentic-os)")
    return sorted(w for n in first_party for w in by_dist[n])


def _inputs(ns):
    wh = Path(ns.wheelhouse)
    if not wh.is_dir():
        raise Inconsistent(f"wheelhouse {wh} does not exist")
    lock = Path(ns.lock)
    if not lock.is_file():
        raise Inconsistent(f"lock {lock} does not exist")
    return sorted(p.name for p in wh.glob("*.whl")), first_party_names(ns.pins), lock_names(lock)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["names", "wheels", "summary"])
    ap.add_argument("--wheelhouse")
    ap.add_argument("--lock")
    ap.add_argument("--pins", default=None)
    ns = ap.parse_args(argv)
    try:
        if ns.cmd == "names":
            print("\n".join(sorted(first_party_names(ns.pins))))
            return 0
        if not ns.wheelhouse or not ns.lock:
            raise Inconsistent(f"'{ns.cmd}' needs --wheelhouse and --lock")
        wheels, first, third = _inputs(ns)
        chosen = select_first_party(wheels, first, third)
        if ns.cmd == "wheels":
            print("\n".join(chosen))
        else:
            print(f"{len(wheels)} wheels = {len(wheels) - len(chosen)} third-party + {len(chosen)} first-party")
        return 0
    except (Inconsistent, OSError, ValueError, KeyError) as e:
        print(f"first-party wheel check FAILED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

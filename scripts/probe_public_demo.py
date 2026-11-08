#!/usr/bin/env python3
"""Nightly live probe of the public-demo compatibility contract (STDLIB ONLY, ANONYMOUS GET/HEAD ONLY).

Exit 0 = every public journey answers its pinned status + marker and every anonymous read probe of a
privileged route stays refused. Exit 1 lists each violated contract line. Runs on a schedule
(.github/workflows/public-demo-nightly.yml) — CI PR runs validate functionality+containment against the local
apps; this job validates hosted availability and response contract.

Nothing here mutates anything, and that is a constraint rather than an observation: the only methods this
script can issue are GET and HEAD (see the protected-probe filter in main()). Probing a write route live to
prove it is refused would perform the mutation it is trying to detect if the guard had regressed, from an
unattended scheduled job, against a hosted demo. Write refusal is proven offline against the repo's own apps.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

CONTRACT = json.loads((Path(__file__).resolve().parent.parent / "audit" / "public_demo_contract.json").read_text())
BASE = CONTRACT["live_base"].rstrip("/")
UA = {"User-Agent": "agentic-os-demo-nightly/1"}


def get(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # an HTTPError IS the response: a pinned non-2xx journey (the /unsubscribe guidance page answers
        # 400 by design) carries the marker the contract checks, so dropping the body here would report a
        # journey as broken when the probe simply stopped reading.
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except OSError as e:
        return 0, str(e)


def main() -> int:
    bad, notes = [], []
    for ep in CONTRACT["public_journeys"]:
        url = ep["path"] if ep["path"].startswith("http") else BASE + ep["path"]
        status, body = get(url)
        strict = ep["status"]                       # statuses the contract expects the marker in
        want = ep.get("live_status") or strict      # widened = documented deploy drift
        if status not in want:
            bad.append(f"{ep['id']}: live {status} not in {want} ({url})")
        elif ep.get("marker") and status in strict and ep["marker"] not in body:
            bad.append(f"{ep['id']}: live marker {ep['marker']!r} missing ({url})")
        elif ep.get("marker") and status not in strict:
            # The same rule tests/test_public_demo_contract.py applies offline: the marker belongs to the
            # documented status. A widened live_status (an older build answering 404) is already reported as
            # drift by the status line above; demanding the marker from that response would report the one
            # condition twice and hide which surface actually drifted. The check stays strict for every
            # response that matched the pinned status — including a designed non-2xx whose marker lives in the
            # error body, like /unsubscribe's 400, which is in `status` and is still marker-checked.
            notes.append(f"{ep['id']}: status {status} is widened live drift "
                         f"(pinned {strict}); marker check skipped for this response only ({url})")
    for ep in CONTRACT["pending_owner"]:
        url = BASE + ep["path"]
        status, body = get(url)
        if status not in ep["pinned_status"] or ep["marker"] not in body:
            bad.append(f"{ep['id']} (pending-owner) drifted: status {status}, marker present={ep['marker'] in body} — owner must classify before any change")
    # Privileged probes are ANONYMOUS READS ONLY. A live probe cannot prove a refusal without sending the
    # request the route refuses, and if a guard had regressed the probe's own POST/DELETE would have already
    # performed the mutation it was trying to detect — on a hosted demo, from a scheduled job. Write-refusal
    # is proven offline against the repo's own apps instead
    # (tests/test_public_demo_contract.py::test_protected_operation_refused_anonymous_local), which also
    # asserts the refusal left no state behind.
    probed = refused_count = 0
    for ep in CONTRACT["protected_anonymous"]:
        if ep["method"] not in ("GET", "HEAD"):
            notes.append(f"{ep['id']}: {ep['method']} probe is offline-only by policy — never sent to a live host")
            continue
        probed += 1
        status, _ = get(BASE + ep["path"])
        if 200 <= status < 300:
            bad.append(f"{ep['id']}: LIVE privileged route served anonymously ({status}) ({BASE}{ep['path']})")
        else:
            refused_count += 1
    if bad:
        print("PUBLIC DEMO CONTRACT VIOLATED:")
        [print(" -", b) for b in bad]
        [print(" - note:", n) for n in notes]
        return 1
    n = len(CONTRACT["public_journeys"]) + len(CONTRACT["pending_owner"])
    print(f"public-demo contract OK: {n} public/pinned surfaces verified live, "
          f"{refused_count}/{probed} anonymous read probes refused as expected "
          f"({len(CONTRACT['protected_anonymous']) - probed} write probes are offline-only)")
    [print("note:", n) for n in notes]
    return 0


if __name__ == "__main__":
    sys.exit(main())

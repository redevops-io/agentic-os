#!/usr/bin/env python3
"""Nightly live probe of the public-demo compatibility contract (read-only GETs, stdlib only).

Exit 0 = every public journey answers its pinned status + marker and every privileged probe is
refused. Exit 1 lists each violated contract line. Runs on a schedule (.github/workflows/
public-demo-nightly.yml) — CI PR runs validate functionality+containment against the local apps;
this job validates hosted availability and response contract. Nothing here mutates anything.
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
    bad = []
    for ep in CONTRACT["public_journeys"]:
        url = ep["path"] if ep["path"].startswith("http") else BASE + ep["path"]
        status, body = get(url)
        want = ep.get("live_status") or ep["status"]
        if status not in want:
            bad.append(f"{ep['id']}: live {status} not in {want} ({url})")
        elif ep.get("marker") and ep["marker"] not in body:
            bad.append(f"{ep['id']}: live marker {ep['marker']!r} missing ({url})")
    for ep in CONTRACT["pending_owner"]:
        url = BASE + ep["path"]
        status, body = get(url)
        if status not in ep["pinned_status"] or ep["marker"] not in body:
            bad.append(f"{ep['id']} (pending-owner) drifted: status {status}, marker present={ep['marker'] in body} — owner must classify before any change")
    for ep in CONTRACT["protected_anonymous"]:
        url = BASE + ep["path"]
        req = urllib.request.Request(url, method=ep["method"], headers=UA,
                                     data=b"{}" if ep["method"] == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                status = r.status
        except urllib.error.HTTPError as e:
            status = e.code
        except OSError as e:
            status = -1
        if 200 <= status < 300:
            bad.append(f"{ep['id']}: LIVE privileged route served anonymously ({status}) ({url})")
    if bad:
        print("PUBLIC DEMO CONTRACT VIOLATED:")
        [print(" -", b) for b in bad]
        return 1
    n = len(CONTRACT["public_journeys"]) + len(CONTRACT["pending_owner"])
    print(f"public-demo contract OK: {n} public/pinned surfaces verified live, "
          f"{len(CONTRACT['protected_anonymous'])} privileged probes refused")
    return 0


if __name__ == "__main__":
    sys.exit(main())

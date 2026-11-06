"""Scan a (minified) JavaScript bundle for third-party package banners and versions.

Only what the bytes show is reported: `@license <Name>` banners (React-style), `/*! name vX.Y.Z | MIT */`
banners, and a few exact version assignments. A library with a banner but no version string is reported
with version None (never guessed). Everything else in the bundle is treated as first-party/unattributed
application code and listed as a limitation.
"""
from __future__ import annotations

import re

BANNER = re.compile(r"/\*[*!]?(?:(?!\*/).)*?\*/", re.S)
LICENSE_BANNER = re.compile(r"@license\s+(?P<who>[^\n*]*)\s*\n\s*\*\s*(?P<file>[A-Za-z0-9_.\-]+?)(?:\.production\.min\.js|\.development\.js|\.js)?\s*\n", re.S)
BANG_BANNER = re.compile(r"/\*!\s*(?P<name>[A-Za-z0-9_.@/\-]+)\s+v?(?P<ver>\d+\.\d+\.\d+[\w.\-+]*)")
MIT_TEXT = re.compile(r"licensed under the (?P<lic>[A-Za-z0-9.\- ]+?) license", re.I)
PACKAGE_OF = {  # banner file stem -> npm package it belongs to
    "react.production.min": "react", "react-jsx-runtime.production.min": "react", "react-dom.production.min": "react-dom",
    "scheduler.production.min": "scheduler", "react-dom-server.browser.production.min": "react-dom",
}


def scan(js: str) -> list[dict]:
    found: dict[str, dict] = {}
    for m in BANNER.finditer(js):
        text = m.group(0)
        lb = LICENSE_BANNER.search(text)
        if lb:
            stem = lb.group("file")
            pkg = PACKAGE_OF.get(stem) or PACKAGE_OF.get(stem + ".production.min") or stem.split(".")[0]
            lic = MIT_TEXT.search(text)
            e = found.setdefault(pkg, {"name": pkg, "version": None, "licence_text": None, "evidence": []})
            e["evidence"].append(f"licence banner of {stem} at offset {m.start()}")
            if lic:
                e["licence_text"] = lic.group("lic").strip()
            continue
        bb = BANG_BANNER.match(text)
        if bb:
            e = found.setdefault(bb.group("name"), {"name": bb.group("name"), "version": None, "licence_text": None, "evidence": []})
            e["version"] = bb.group("ver")
            e["evidence"].append(f"banner at offset {m.start()}: {text[:80]!r}")
    # exact version assignments (React 18: `X.version="18.3.1";` right before the react banner, and the
    # react-dom devtools hook `version:"18.3.1",rendererPackageName:"react-dom"`)
    if "react" in found:
        m = re.search(r'\.version="(\d+\.\d+\.\d+)";[\w.=]*;?var \w+=\w+\.exports;/\*\*\s*\n\s*\* @license React', js)
        m = m or re.search(r'\.useTransition=function\(\)\{[^}]*\}\};\w+\.version="(\d+\.\d+\.\d+)"', js)
        if m:
            found["react"]["version"] = m.group(1)
            found["react"]["evidence"].append(f'exact string .version="{m.group(1)}" at offset {m.start(1)}')
    if "react-dom" in found:
        m = re.search(r'version:"(\d+\.\d+\.\d+)",rendererPackageName:"react-dom"', js)
        if m:
            found["react-dom"]["version"] = m.group(1)
            found["react-dom"]["evidence"].append(f'rendererPackageName "react-dom" with version "{m.group(1)}" at offset {m.start(1)}')
    return sorted(found.values(), key=lambda e: e["name"])

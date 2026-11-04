#!/usr/bin/env python3
"""Identity of the R0-prime line (and of the frozen v0.3.2 line it must stay distinct from).

The data live in `packaging/identity.json`. This module only reads it and DERIVES the artifact file names the way
Tauri 2 (tauri-bundler 2.9.x) names them, so no script has to repeat a literal:

    python3 -I packaging/tools/identity.py get product_name
    python3 -I packaging/tools/identity.py get app_bundle            # "<product>.app"
    python3 -I packaging/tools/identity.py get dmg_macos_arm64       # "<product>_<version>_aarch64.dmg"
    python3 -I packaging/tools/identity.py get msi_x64               # "<product>_<version>_x64_en-US.msi"
    python3 -I packaging/tools/identity.py get nsis_x64              # "<product>_<version>_x64-setup.exe"
    python3 -I packaging/tools/identity.py get --frozen dmg_macos_arm64
    python3 -I packaging/tools/identity.py all [--frozen]            # JSON

`get` prints one value and nothing else (no newline-sensitive data: values contain no newline).
Standard library only; no network; no side effect.
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path

IDENTITY = Path(__file__).resolve().parent.parent / "identity.json"

# Apple: CFBundleShortVersionString / CFBundleVersion are one to three period-separated integers.
APPLE_VERSION = re.compile(r"^(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*)){0,2}$")


def load(path: Path = IDENTITY) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def wix_default_upgrade_code(product_name: str) -> str:
    """What tauri-bundler derives when `bundle.windows.wix.upgradeCode` is unset (tauri-bundler 2.9.x, msi/mod.rs)."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{product_name}.exe.app.x64"))


def derived(line: dict) -> dict:
    p, v = line["product_name"], line["version"]
    return {
        "app_bundle": f"{p}.app",
        "dmg_macos_arm64": f"{p}_{v}_aarch64.dmg",
        "msi_x64": f"{p}_{v}_x64_en-US.msi",
        "nsis_x64": f"{p}_{v}_x64-setup.exe",
        "launcher_exe_windows": f"{line['main_binary_name']}.exe",
        "sidecar_exe_windows": f"{line['sidecar_binary_name']}.exe",
        "dmg_volume_name": p,
        "nsis_uninstall_key": rf"HKCU or HKLM\Software\Microsoft\Windows\CurrentVersion\Uninstall\{p}",
        "install_dir_registry_key": rf"HKCU\Software\{line['bundle_identifier'].split('.')[1]}\{p}",
        "msi_install_dir_name": p,
        "webview_and_cache_dir_name": line["bundle_identifier"],
    }


def resolve(frozen: bool = False, path: Path = IDENTITY) -> dict:
    d = load(path)
    line = d["frozen" if frozen else "r0prime"]
    out = {"release_line": "frozen-v0.3.2" if frozen else d["release_line"], **line, **derived(line)}
    return out


def main(argv: list[str]) -> int:
    args = [a for a in argv if a != "--frozen"]
    frozen = "--frozen" in argv
    if not args:
        print(__doc__)
        return 2
    cmd, rest = args[0], args[1:]
    data = resolve(frozen)
    if cmd == "all":
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    if cmd == "get" and len(rest) == 1 and rest[0] in data:
        print(data[rest[0]])
        return 0
    print(f"usage: identity.py [--frozen] (all | get <{'|'.join(sorted(data))}>)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

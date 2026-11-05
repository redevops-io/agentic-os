# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — freeze the Projects control plane + bootstrap brain into ONE binary.
# Built by packaging/build-sidecar.sh against the offline wheelhouse (Task 1). Target: CPython 3.12.
#
# The tricky bits for a FastAPI/uvicorn app that loads plugins dynamically:
#   • collect_submodules('agentic_os') — mission/connector modules are imported by name at runtime
#   • collect_submodules('uvicorn')    — uvicorn picks its event-loop/protocol impls dynamically
#   • collect_data_files('agentic_os') — ships the vendored Projects UI bundle (agentic_os/projects_ui)
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hidden = collect_submodules("agentic_os") + collect_submodules("uvicorn") + [
    "anyio._backends._asyncio",
    "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
]
# Shipped-but-unexercised: nothing in the serve/bootstrap import closure imports `knowledge-frontier` (AST scan,
# blocked-import run and frozen run: tests/test_sidecar_excludes.py), and its licence is an inference
# ([OWNER DECISION D-LIC-2]), so the frozen sidecar leaves it out. The two shim modules go with it: they cannot
# import without it.
UNEXERCISED = ("knowledge_frontier", "agentic_os.knowledge_frontier", "agentic_os.knowledge_frontier_backtest")
hidden = [h for h in hidden if h not in UNEXERCISED]
# Build-tool leftovers ([OWNER DECISION D-LIC-17]): PyInstaller's own `pyi_rth_setuptools` runtime hook pulls setuptools
# (130 modules, with its vendored jaraco.* / backports.* / wheel), `_distutils_hack` and `packaging` into the archive
# because setuptools sits in the freeze venv as a PyInstaller dependency. No first-party or shipped third-party module
# imports them (AST scan + frozen run: tests/test_sidecar_build_only_excludes.py), none of the three is in the shipped
# wheel lock, and none has a licence text in the notices. Leaving them out changes no behaviour.
BUILD_ONLY = ("setuptools", "pkg_resources", "_distutils_hack", "packaging")
for opt in ("redevops_connectors", "duckdb"):
    try:
        hidden += collect_submodules(opt)
    except Exception:
        pass  # optional; absent in a minimal build

datas = collect_data_files("agentic_os")  # includes agentic_os/projects_ui/** (the served UI)
# Licence and notice files travel inside the sidecar too (extracted with it at run time): the output of
# packaging/notices/stage_licenses.py. Fail the build if a directory is named but empty of the required files.
import os
_LIC = os.environ.get("REDEVOPS_LICENSES_DIR")
if _LIC:
    for _f in ("LICENSE", "LICENSE-COMMONS-CLAUSE", "LICENSE.md", "LICENSING.md", "THIRD_PARTY_NOTICES.txt", "about.json"):
        assert os.path.isfile(os.path.join(_LIC, _f)), f"REDEVOPS_LICENSES_DIR lacks {_f}"
    datas += [(_LIC, "Licenses")]

a = Analysis(
    ["sidecar_main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "redevops_rag", "sentence_transformers", "torch", *UNEXERCISED, *BUILD_ONLY],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="redevops-sidecar",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True,            # a background service; the launcher owns the window/tray
    disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)

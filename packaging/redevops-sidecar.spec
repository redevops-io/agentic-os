# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — freeze the Projects control plane + bootstrap brain into ONE binary.
# Built by packaging/build-sidecar.sh against the offline wheelhouse (Task 1). Target: CPython 3.12.
#
# The tricky bits for a FastAPI/uvicorn app that loads plugins dynamically:
#   • collect_submodules('agentic_os') — mission/connector modules are imported by name at runtime
#   • collect_submodules('uvicorn')    — uvicorn picks its event-loop/protocol impls dynamically
#   • collect_data_files('agentic_os') — ships the Projects UI bundle fetched at wheel build (agentic_os/projects_ui)
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
# `site` ([D-NEW-3]): agentic_os/trusted_import.py imports it inside try/except ImportError to add site.getsitepackages() and the user site to
# the interpreter's trusted package directories. The import pulled `site`, `_sitebuiltins`, `pydoc`, `pydoc_data` and `rlcompleter` into
# the archive (1008 -> 1424 names). CPython 3.11+ carries `site` as a built-in FROZEN module, so excluding it from the archive changes
# nothing at run time (the frozen probe shows `site` loaded from the interpreter, origin "frozen", and the same verdicts with and without
# the exclusion); on an interpreter without it, `import site` raises ImportError and trusted_import falls back to sysconfig alone.
# (packaging/probes/trusted_import_probe.py, tests/test_frozen_trusted_import.py)
SITE_NOT_FROZEN = ("site",)
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
    excludes=["tkinter", "matplotlib", "redevops_rag", "sentence_transformers", "torch", *UNEXERCISED, *BUILD_ONLY, *SITE_NOT_FROZEN],
    noarchive=False,
)
# [D-NEW-4] The interpreter's `_sysconfigdata__<platform>` holds the absolute path it is installed at; without this two builds with the
# interpreter at different paths differ in that one module (and so in the pass-1 SBOM digest and the Licenses files). The prefix becomes a
# fixed token in the module's source before the archive is written. See packaging/tools/normalise_sysconfigdata.py.
import sys
_here = globals().get("SPECPATH") or os.getcwd()
_pk = next(d for d in (_here, os.path.dirname(_here), os.path.join(_here, "packaging"))
           if os.path.isfile(os.path.join(d, "tools", "normalise_sysconfigdata.py")))
sys.path.insert(0, os.path.join(_pk, "tools"))
from normalise_sysconfigdata import normalise_pure
try:
    from PyInstaller.config import CONF as _CONF          # the module graph's code cache: PYZ uses it in preference to the source path
    _code_cache = _CONF["code_cache"].get(id(a.pure))
    assert _code_cache is not None, "PyInstaller keeps no code cache for Analysis.pure: update packaging/tools/normalise_sysconfigdata.py"
except ImportError:                                       # the spec tests run it with PyInstaller stubbed
    _code_cache = None
a.pure[:] = normalise_pure(a.pure, os.path.join(_pk, "build", "normalised"),
                           [sys.base_prefix, sys.prefix, sys.exec_prefix, sys.base_exec_prefix], code_cache=_code_cache)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="redevops-sidecar",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True,            # a background service; the launcher owns the window/tray
    disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)

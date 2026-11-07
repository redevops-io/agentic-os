#!/usr/bin/env python3
"""Make the frozen sidecar independent of where the build interpreter is installed (D-NEW-4).

The interpreter's `_sysconfigdata__<platform>` module (uv rewrites the install prefix into it) holds ~25 strings with the absolute path
of the interpreter. PyInstaller compiles it into the archive, so two builds with the interpreter at different paths (a user build and a
root build, two hosts) produced different sidecar bytes, a different pass-1 SBOM digest and different Licenses files, from identical
sources. This replaces that prefix by a fixed token in the one module's source, before the archive is written. The values are
build-host paths a frozen app never uses (sysconfig derives the real prefix from sys.prefix at run time); every other constant of the
module, including the CPython build's own temporary directories, stays as it is.

Used from packaging/redevops-sidecar.spec; standard library only.
"""
from __future__ import annotations

import re
from pathlib import Path

TOKEN = "/__python_prefix__"
PREFIX_OF = "_sysconfigdata"


def normalise(source: str, prefixes) -> str:
    """`source` with every occurrence of one of `prefixes` (a path, longest first, not followed by a name character) replaced by TOKEN."""
    out = source
    for p in sorted({str(x).rstrip("/\\") for x in prefixes if x and str(x).rstrip("/\\")}, key=len, reverse=True):
        out = re.sub(re.escape(p) + r"(?![A-Za-z0-9_.\-])", lambda _m: TOKEN, out)
    return out


def normalise_pure(pure, workdir, prefixes, code_cache=None):
    """A copy of PyInstaller's `Analysis.pure` list ((name, path, typecode) entries) in which each `_sysconfigdata*` source that mentions
    one of `prefixes` is replaced by a normalised copy written under `workdir`. Entries are otherwise returned untouched.

    PyInstaller 6 compiles the modules the module graph already read from its CODE CACHE (name -> code object, keyed by the identity of
    `Analysis.pure`) and only compiles from the entry's path when the cache has no entry. So the changed source is used only if the
    cached code object of that module is dropped: pass that dict as `code_cache` (the spec does); the entry is removed here."""
    out = []
    for entry in pure:
        name, src, typ = entry[0], entry[1], entry[2]
        if typ == "PYMODULE" and str(name).startswith(PREFIX_OF) and src and str(src).endswith(".py") and Path(src).is_file():
            text = Path(src).read_text(encoding="utf-8")
            new = normalise(text, prefixes)
            if new != text:
                dest = Path(workdir) / (str(name) + ".py")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(new, encoding="utf-8")
                entry = (name, str(dest), typ, *entry[3:])
                if code_cache is not None:
                    code_cache.pop(name, None)
        out.append(entry)
    return out

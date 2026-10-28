"""Import the enterprise package and the engine only from an INSTALLED distribution (DR-02).

A directory named ``context_runtime_enterprise`` (or ``context_runtime``, ``agent_harness``) on ``sys.path`` ahead of the real one executes when it is imported, e.g. the working directory of
``python -m uvicorn``. ``importlib.util.find_spec`` of a top-level name locates the package WITHOUT running it; this module then requires the located ``__init__`` to be a file an installed
distribution's metadata lists (its RECORD), or, for an editable install, to lie under the project directory the distribution's ``direct_url.json`` names. **The distribution itself is looked up
ONLY in the interpreter's own package directories** (BB-01): ``importlib.metadata`` reads dist-info from every ``sys.path`` entry, the working directory included, so a forged ``*.dist-info`` placed next to a
planted package would otherwise vouch for it. Anything else is ``untrusted`` and is never imported by the kernel. ``package_status`` never executes the package.
"""
from __future__ import annotations

import importlib
import importlib.metadata as md
import importlib.util as iu
import site
import sys
import sysconfig
from pathlib import Path


def _resolved(path) -> "Path | None":
    try:
        return Path(path).resolve()
    except Exception:  # noqa: BLE001
        return None


def _editable_root(dist) -> "Path | None":
    import json
    try:
        raw = dist.read_text("direct_url.json")
        if not raw:
            return None
        info = json.loads(raw)
        if not (info.get("dir_info") or {}).get("editable"):
            return None
        url = str(info.get("url", ""))
        if not url.startswith("file://"):
            return None
        from urllib.parse import unquote, urlparse
        return _resolved(unquote(urlparse(url).path))
    except Exception:  # noqa: BLE001
        return None


def _trusted_dirs() -> "list[Path]":
    """The interpreter's own package directories: ``sysconfig`` purelib / platlib (the venv's site-packages inside a venv), ``site.getsitepackages()`` and the user site. Never the working directory and
    never an arbitrary ``sys.path`` entry."""
    out: "list[Path]" = []
    cands = [sysconfig.get_paths().get("purelib"), sysconfig.get_paths().get("platlib")]
    try:
        cands += list(site.getsitepackages())
    except Exception:  # noqa: BLE001
        pass
    try:
        if site.ENABLE_USER_SITE:
            cands.append(site.getusersitepackages())
    except Exception:  # noqa: BLE001
        pass
    for c in cands:
        p = _resolved(c) if c else None
        if p is not None and p not in out and p.is_dir():
            out.append(p)
    return out


def _under(path: Path, roots) -> bool:
    return any(path == r or r in path.parents for r in roots)


def package_status(top: str) -> str:
    """``absent`` (not found, or blocked with a ``None`` entry in ``sys.modules``), ``trusted`` (provided by a distribution installed in the interpreter's package directories), or ``untrusted`` (present,
    but something else provides it: a plain directory or file on ``sys.path``, a module injected into ``sys.modules``, a forged dist-info elsewhere, a file the distribution does not list). Never imports the package."""
    if top in sys.modules and sys.modules[top] is None:
        return "absent"
    try:
        spec = iu.find_spec(top)
    except ValueError:                       # in sys.modules with no __spec__: something put it there, and no distribution provides that
        return "untrusted"
    except Exception:  # noqa: BLE001 — a finder that fails is not a trusted source
        return "untrusted"
    if spec is None:
        return "absent"
    origin = _resolved(spec.origin) if spec.origin and spec.has_location else None
    if origin is None or origin.name != "__init__.py":
        return "untrusted"                   # a namespace package, a built-in, a single-file module: not what a distribution installs
    roots = _trusted_dirs()
    if not roots:
        return "untrusted"
    try:
        dists = list(md.distributions(path=[str(r) for r in roots]))      # ONLY the interpreter's package directories, not every sys.path entry
    except Exception:  # noqa: BLE001
        return "untrusted"
    for dist in dists:
        try:
            info_dir = _resolved(dist._path) if getattr(dist, "_path", None) is not None else None     # where this dist-info really is
        except Exception:  # noqa: BLE001
            info_dir = None
        if info_dir is None or not _under(info_dir, roots):
            continue
        for f in dist.files or ():
            if f.parts and f.parts[0] == top and _resolved(dist.locate_file(f)) == origin and _under(origin, roots):       # the listed file lies in the package directories too
                return "trusted"
        root = _editable_root(dist)
        if root is not None and (origin == root or root in origin.parents):
            return "trusted"
    return "untrusted"


def import_trusted(module: str):
    """Import ``module`` only if its top-level package is provided by an installed distribution; None for absent or untrusted (nothing is executed in those cases)."""
    if package_status(module.split(".")[0]) != "trusted":
        return None
    try:
        return importlib.import_module(module)
    except Exception:  # noqa: BLE001
        return None

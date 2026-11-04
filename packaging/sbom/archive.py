"""A read-only view of a PyInstaller (CArchive) executable: PKG entries + PYZ modules.

Two sources:
  * ExecutableArchive(path)   reads the real executable with PyInstaller.archive.readers (full fidelity:
                              bytes are available, so native library versions and the UI bundle can be probed)
  * ListingArchive(...)       parses a text listing (`pyi-archive_viewer -l` style, or the plain PKG / PYZ
                              name files the audit saved). No bytes: versions must come from a facts file.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def norm_path(name: str) -> str:
    return name.replace("\\", "/")


class Archive:
    pkg: list[str]
    pyz: list[str]
    source: str
    source_sha256: str | None = None

    def read(self, name: str) -> bytes | None:  # PKG member bytes (uncompressed) or None
        return None

    def pyz_read(self, mod: str) -> bytes | None:  # raw marshalled bytecode of a PYZ module or None
        return None

    @property
    def has_bytes(self) -> bool:
        return False

    @property
    def is_windows(self) -> bool:
        return any(n.lower().endswith((".pyd", ".dll")) for n in self.pkg)


class ExecutableArchive(Archive):
    def __init__(self, path: str | Path):
        from PyInstaller.archive.readers import CArchiveReader  # needs PyInstaller (build venv)

        self.path = Path(path)
        self._r = CArchiveReader(str(self.path))
        self.source = f"executable:{self.path.name}"
        self.source_sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()
        names = list(self._r.toc)
        self.pyz = []
        self.pkg = []
        self._pyz_reader = {}
        for n, ent in self._r.toc.items():
            if ent[4] == "z":  # an embedded PYZ archive
                z = self._r.open_embedded_archive(n)
                self._pyz_reader[n] = z
                self.pyz += list(z.toc)
            self.pkg.append(n)
        self.pkg_all = names

    def read(self, name: str) -> bytes | None:
        try:
            return self._r.extract(name)
        except Exception:
            return None

    def pyz_read(self, mod: str) -> bytes | None:
        for z in self._pyz_reader.values():
            if mod in z.toc:
                try:
                    return z.extract(mod, raw=True)
                except Exception:
                    return None
        return None

    @property
    def has_bytes(self) -> bool:
        return True


class ListingArchive(Archive):
    """PKG listing (+ optional PYZ listing). Accepts the combined `pyi-archive_viewer -l` text (two
    sections: "Contents of '...' (PKG/CArchive):" and "Contents of 'PYZ.pyz' (PYZ):") or two plain name files."""

    def __init__(self, pkg_listing: str | Path, pyz_listing: str | Path | None = None):
        text = Path(pkg_listing).read_text(encoding="utf-8", errors="replace")
        self.source = f"listing:{Path(pkg_listing).name}"
        self.source_sha256 = hashlib.sha256(text.encode()).hexdigest()
        pkg: list[str] = []
        pyz: list[str] = []
        section = "pkg"
        for raw in text.splitlines():
            line = raw.rstrip("\r")
            s = line.strip()
            if not s:
                continue
            if s.startswith("Options in"):
                section = "skip"
                continue
            if s.startswith("Contents of") and "(PKG/CArchive)" in s:
                section = "pkg"
                continue
            if s.startswith("Contents of") and "(PYZ)" in s:
                section = "pyz"
                continue
            if section == "skip" and not s.startswith("Contents"):
                continue
            (pyz if section == "pyz" else pkg).append(s)
        if pyz_listing:
            pyz = [l.strip() for l in Path(pyz_listing).read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
        self.pkg = [norm_path(p) for p in pkg if p != "PYZ.pyz"]
        self.pyz = pyz

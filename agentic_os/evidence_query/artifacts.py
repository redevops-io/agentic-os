"""Confined, read-only access to runtime artifact files.

The loader opens every component with O_NOFOLLOW relative to a directory file descriptor, so a symlink anywhere in
the path (inside or outside the root), a ``..`` component, an absolute path or a NUL byte is refused, and a path
swapped after registration cannot redirect the read. Files are opened O_RDONLY only. The bytes that are hashed are
the bytes that are parsed (no re-read between check and use). The query worker never receives a path at all."""
from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from typing import Callable, List

from .errors import ArtifactError, IntegrityError

_FLAGS_RO = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


class ArtifactRoot:
    def __init__(self, path: str, *, max_bytes: int = 256 * 1024 * 1024):
        real = os.path.realpath(str(path))
        if not os.path.isdir(real):
            raise ArtifactError("artifact root is not a directory")
        self.path = real
        self.max_bytes = max_bytes

    @staticmethod
    def _components(rel: str) -> List[str]:
        if not isinstance(rel, str) or not rel or "\x00" in rel or rel.startswith("/") or "\\" in rel:
            raise ArtifactError("invalid artifact path")
        parts = rel.split("/")
        if any(p in ("", ".", "..") for p in parts):
            raise ArtifactError("invalid artifact path")
        return parts

    def read_bytes(self, rel: str) -> bytes:
        parts = self._components(rel)
        fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            for name in parts[:-1]:
                try:
                    nfd = os.open(name, _FLAGS_RO | getattr(os, "O_DIRECTORY", 0), dir_fd=fd)
                except OSError:
                    raise ArtifactError("artifact path not readable") from None
                os.close(fd)
                fd = nfd
            try:
                ffd = os.open(parts[-1], _FLAGS_RO, dir_fd=fd)
            except OSError:
                raise ArtifactError("artifact path not readable") from None
        finally:
            os.close(fd)
        try:
            st = os.fstat(ffd)
            if not stat.S_ISREG(st.st_mode):
                raise ArtifactError("artifact is not a regular file")
            if st.st_size > self.max_bytes:
                raise ArtifactError("artifact exceeds the size limit")
            with os.fdopen(ffd, "rb", closefd=False) as fh:
                data = fh.read(self.max_bytes + 1)
        finally:
            os.close(ffd)
        if len(data) > self.max_bytes:
            raise ArtifactError("artifact exceeds the size limit")
        return data


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class ArtifactFile:
    """A file inside a root with the sha256 recorded when it was registered (the manifest entry)."""
    root: ArtifactRoot
    rel: str
    sha256: str
    fmt: str = "jsonl"                      # jsonl | json (array of objects) | custom (see ``parse``)
    parse: Callable[[bytes], list] | None = None

    def load(self) -> tuple[list, str]:
        data = self.root.read_bytes(self.rel)
        digest = sha256_hex(data)
        if digest != self.sha256:
            raise IntegrityError("artifact hash mismatch")
        return self._rows(data), digest

    def verify(self) -> bool:
        try:
            return sha256_hex(self.root.read_bytes(self.rel)) == self.sha256
        except ArtifactError:
            return False

    def _rows(self, data: bytes) -> list:
        import json
        if self.parse is not None:
            return list(self.parse(data))
        text = data.decode("utf-8")
        if self.fmt == "jsonl":
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        if self.fmt == "json":
            rows = json.loads(text)
            if not isinstance(rows, list):
                raise ArtifactError("json artifact must be an array of objects")
            return rows
        raise ArtifactError("unsupported artifact format")

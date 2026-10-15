"""``DocumentStore``: where source documents live. Today a LOCAL directory; S3 is a future implementation.

LocalFS layout (all under the configured document root)::

    <root>/<tenant>/<doc path>                  the customer's own file (never modified or deleted by the connector)
    <root>/<tenant>/.docedit/blobs/<sha256>     content-addressed version bytes (write-once)
    <root>/<tenant>/.docedit/manifests/<key>.json   sidecar manifest: monotonically increasing version counter,
                                                version -> sha256, classification label, lineage
    <root>/<tenant>/.docedit/receipts.jsonl     immutable hash-chained receipts (see ledger.py)

A document id is a LOGICAL posix-style relative path (``contracts/nda.docx``) -- never an OS path. ``safe_rel_path``
rejects traversal, absolute / drive / UNC paths, backslashes, NTFS alternate data streams, reserved Windows device
names, trailing dots/spaces, 8.3 aliases, hidden/sidecar segments and control or bidi characters, on EVERY OS (so a
document id means the same thing on Windows, macOS and Linux). Symlinks / junctions that escape the root are refused.
Versions are never overwritten in place: every edit is a new blob + a new counter value.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .errors import Conflict, InvalidPath, NotFound, StaleVersion, TenantMismatch
from .ledger import Ledger
from .locking import FileLock, atomic_write_bytes

SIDECAR = ".docedit"
_TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_WIN_RESERVED = re.compile(r"^(con|prn|aux|nul|conin\$|conout\$|clock\$|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?$",
                           re.I)
_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f\x7f\\]')
_TILDE_ALIAS = re.compile(r"~[0-9]")
MAX_SEGMENT, MAX_PATH = 255, 1024
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
MAX_FILE_BYTES = 64 * 1024 * 1024


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_tenant(tenant: str) -> str:
    if not isinstance(tenant, str) or not _TENANT_RE.match(tenant):
        raise TenantMismatch(f"invalid tenant id {tenant!r}")
    return tenant


def safe_rel_path(doc_id: str) -> str:
    """Validate a logical document id and return it (NFC-normalised). Raises ``InvalidPath``."""
    if not isinstance(doc_id, str) or not doc_id.strip():
        raise InvalidPath("empty document id")
    if len(doc_id) > MAX_PATH:
        raise InvalidPath("document id too long")
    doc_id = unicodedata.normalize("NFC", doc_id)
    if doc_id.startswith(("/", "\\")) or doc_id.startswith("//"):
        raise InvalidPath("absolute or UNC paths are not allowed")
    if re.match(r"^[A-Za-z]:", doc_id):
        raise InvalidPath("drive-qualified paths are not allowed")
    if "\\" in doc_id:
        raise InvalidPath("backslashes are not allowed (document ids use '/')")
    if ":" in doc_id:
        raise InvalidPath("':' is not allowed (alternate data streams / drive letters)")
    for ch in doc_id:
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
            raise InvalidPath("control, format (bidi) or unassigned characters are not allowed")
    segments = doc_id.split("/")
    for seg in segments:
        if seg == "" or seg in (".", ".."):
            raise InvalidPath("empty, '.' or '..' path segments are not allowed")
        if len(seg) > MAX_SEGMENT:
            raise InvalidPath("path segment too long")
        if seg.startswith("."):
            raise InvalidPath("hidden / sidecar path segments are not allowed")
        if seg != seg.rstrip(" .") or seg != seg.lstrip(" "):
            raise InvalidPath("segments may not start with a space or end with a dot or space")
        if _BAD_CHARS.search(seg):
            raise InvalidPath("reserved characters in path segment")
        if _WIN_RESERVED.match(seg.split(".", 1)[0].rstrip(" ")) or _WIN_RESERVED.match(seg):
            raise InvalidPath(f"reserved Windows device name {seg!r}")
        if _TILDE_ALIAS.search(seg):
            raise InvalidPath("8.3 short-name aliases are not allowed")
    return "/".join(segments)


def canonical_id(doc_id: str) -> str:
    """The ONE identity of a document: validated, NFC, case-folded. Windows/macOS file systems are case-insensitive,
    so ``Secret.docx`` and ``secret.docx`` are the same document and must share label, versions and lock."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", safe_rel_path(doc_id)).casefold())


def canonical_id_segment(name: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", name).casefold())


def _is_link_or_reparse(p: Path) -> bool:
    try:
        st = os.lstat(p)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)       # Windows junctions / reparse points
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)


@dataclass(frozen=True)
class VersionInfo:
    doc_id: str
    tenant: str
    version: int
    sha256: str
    size: int
    created_at: float = 0.0
    actor: str = ""
    kind: str = ""                  # import | external_edit | edit | convert
    parent_version: int = 0
    receipt_id: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class DocumentStore(ABC):
    """Storage interface. ``LocalFSStore`` implements it today; an S3 backend is a future implementation."""

    @abstractmethod
    def head(self, tenant: str, doc_id: str) -> VersionInfo: ...

    @abstractmethod
    def read(self, tenant: str, doc_id: str, version: Optional[int] = None) -> "tuple[bytes, VersionInfo]": ...

    @abstractmethod
    def put_version(self, tenant: str, doc_id: str, data: bytes, *, expected_version: int, actor: str,
                    kind: str, receipt_id: str = "") -> VersionInfo: ...

    @abstractmethod
    def get_classification(self, tenant: str, doc_id: str) -> Optional[str]: ...

    @abstractmethod
    def set_classification(self, tenant: str, doc_id: str, label: str) -> None: ...

    @abstractmethod
    def versions(self, tenant: str, doc_id: str) -> "list[VersionInfo]": ...

    @abstractmethod
    def receipt_log(self, tenant: str) -> "Ledger": ...

    @abstractmethod
    def create_derived(self, tenant: str, doc_id: str, data: bytes, *, actor: str, kind: str,
                       classification: Optional[str] = None) -> VersionInfo: ...


class LocalFSStore(DocumentStore):
    def __init__(self, root: "str | os.PathLike", max_bytes: int = MAX_FILE_BYTES):
        self.max_bytes = max_bytes
        self.root = Path(root)
        if not self.root.is_dir():
            raise NotFound(f"document root {self.root} is not a directory")
        self._root_real = Path(os.path.realpath(self.root))

    # -- path resolution ---------------------------------------------------------------------------------------
    def _tenant_dir(self, tenant: str) -> Path:
        validate_tenant(tenant)
        d = self.root / tenant
        if _is_link_or_reparse(d):
            raise InvalidPath("tenant directory is a link")
        return d

    def _contained(self, p: Path) -> Path:
        real = Path(os.path.realpath(p))
        try:
            real.relative_to(self._root_real)
        except ValueError:
            raise InvalidPath("path escapes the document root") from None
        return real

    def _resolve(self, tenant: str, doc_id: str) -> "tuple[Path, str]":
        """Path of the real on-disk file plus its real-case relative id. Each segment is matched exactly, else
        case-insensitively; two entries that differ only by case (or Unicode form) make the id AMBIGUOUS and are
        refused (they would otherwise share one identity)."""
        rel = safe_rel_path(doc_id)
        cur = self._tenant_dir(tenant)
        real: list = []
        for seg in rel.split("/"):
            want = canonical_id_segment(seg)
            try:
                names = os.listdir(cur)
            except OSError:
                names = []
            hits = [n for n in names if canonical_id_segment(n) == want]
            if len(hits) > 1:
                raise InvalidPath("ambiguous document id: several entries differ only by case")
            name = hits[0] if hits else seg
            cur = cur / name
            real.append(name)
            if _is_link_or_reparse(cur):
                raise InvalidPath("symlinks / junctions inside the document tree are refused")
        self._contained(cur)
        return cur, "/".join(real)

    def _source_path(self, tenant: str, doc_id: str) -> Path:
        return self._resolve(tenant, doc_id)[0]

    def _side(self, tenant: str) -> Path:
        d = self._tenant_dir(tenant) / SIDECAR
        if _is_link_or_reparse(d):
            raise InvalidPath("sidecar directory is a link")
        return d

    @staticmethod
    def _key(doc_id: str) -> str:
        return sha256_hex(canonical_id(doc_id).encode("utf-8"))

    def _manifest_path(self, tenant: str, doc_id: str) -> Path:
        return self._side(tenant) / "manifests" / (self._key(doc_id) + ".json")

    def _blob_path(self, tenant: str, sha: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise InvalidPath("bad blob hash")
        return self._side(tenant) / "blobs" / sha

    # -- manifest ----------------------------------------------------------------------------------------------
    def _load(self, tenant: str, doc_id: str) -> Optional[dict]:
        p = self._manifest_path(tenant, doc_id)
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            raise Conflict(f"manifest for {doc_id!r} is unreadable: {e}") from None

    def _save(self, tenant: str, doc_id: str, m: dict) -> None:
        atomic_write_bytes(self._manifest_path(tenant, doc_id),
                           json.dumps(m, sort_keys=True, indent=1).encode("utf-8"))

    def _lock(self, tenant: str, doc_id: str) -> FileLock:
        return FileLock(self._side(tenant) / "manifests" / (self._key(doc_id) + ".lock"))

    def _write_blob(self, tenant: str, data: bytes) -> str:
        sha = sha256_hex(data)
        p = self._blob_path(tenant, sha)
        if not p.exists():
            atomic_write_bytes(p, data)
        return sha

    def _read_source_bytes(self, tenant: str, doc_id: str) -> bytes:
        p = self._source_path(tenant, doc_id)
        if not p.is_file():
            raise NotFound(f"document {doc_id!r} not found")
        if p.stat().st_size > self.max_bytes:
            raise InvalidPath(f"document exceeds the {self.max_bytes} byte store limit")
        with open(p, "rb") as f:
            data = f.read(self.max_bytes + 1)
        if len(data) > self.max_bytes:
            raise InvalidPath(f"document exceeds the {self.max_bytes} byte store limit")
        return data

    def _vi(self, tenant: str, doc_id: str, v: dict) -> VersionInfo:
        return VersionInfo(doc_id=doc_id, tenant=tenant, version=v["version"], sha256=v["sha256"], size=v["size"],
                           created_at=v.get("created_at", 0.0), actor=v.get("actor", ""), kind=v.get("kind", ""),
                           parent_version=v.get("parent_version", 0), receipt_id=v.get("receipt_id", ""))

    def _sync(self, tenant: str, doc_id: str) -> dict:
        """Load the manifest, registering the customer's file as a version when it is new or was changed outside
        the connector (counter only ever increases). Caller holds the lock for writes."""
        m = self._load(tenant, doc_id)
        data = self._read_source_bytes(tenant, doc_id)
        sha = sha256_hex(data)
        if m is None:
            m = {"doc_id": self._resolve(tenant, doc_id)[1], "counter": 0, "origin_sha256": "", "versions": [],
                 "classification": None}
        if m["origin_sha256"] != sha:
            self._write_blob(tenant, data)
            m["counter"] += 1
            m["versions"].append({"version": m["counter"], "sha256": sha, "size": len(data),
                                  "created_at": time.time(), "actor": "owner",
                                  "kind": "import" if not m["versions"] else "external_edit",
                                  "parent_version": m["versions"][-1]["version"] if m["versions"] else 0})
            m["origin_sha256"] = sha
            self._save(tenant, doc_id, m)
        return m

    # -- DocumentStore ------------------------------------------------------------------------------------------
    def head(self, tenant: str, doc_id: str) -> VersionInfo:
        m = self._load_or_derived(tenant, doc_id)
        return self._vi(tenant, doc_id, m["versions"][-1])

    def _load_or_derived(self, tenant: str, doc_id: str) -> dict:
        m = self._load(tenant, doc_id)
        if m is not None and m.get("derived"):
            return m
        with self._lock(tenant, doc_id):
            return self._sync(tenant, doc_id)

    def versions(self, tenant: str, doc_id: str) -> "list[VersionInfo]":
        m = self._load_or_derived(tenant, doc_id)
        return [self._vi(tenant, doc_id, v) for v in m["versions"]]

    def read(self, tenant: str, doc_id: str, version: Optional[int] = None) -> "tuple[bytes, VersionInfo]":
        m = self._load_or_derived(tenant, doc_id)
        chosen = m["versions"][-1] if version is None else next(
            (v for v in m["versions"] if v["version"] == version), None)
        if chosen is None:
            raise NotFound(f"version {version} of {doc_id!r} not found")
        bp = self._blob_path(tenant, chosen["sha256"])
        if bp.stat().st_size > self.max_bytes:
            raise InvalidPath("stored version exceeds the store size limit")
        data = bp.read_bytes()
        if sha256_hex(data) != chosen["sha256"]:
            raise Conflict(f"blob for {doc_id!r} v{chosen['version']} failed its hash check (corrupt store)")
        return data, self._vi(tenant, doc_id, chosen)

    def put_version(self, tenant: str, doc_id: str, data: bytes, *, expected_version: int, actor: str,
                    kind: str, receipt_id: str = "") -> VersionInfo:
        """Append a NEW version. Fails closed (``StaleVersion``) unless ``expected_version`` is still the head."""
        with self._lock(tenant, doc_id):
            m = self._load(tenant, doc_id)
            if m is None or not m.get("derived"):
                m = self._sync(tenant, doc_id)
            head = m["versions"][-1]
            if head["version"] != expected_version:
                raise StaleVersion(f"expected version {expected_version} but head is {head['version']}",
                                   expected=expected_version, current=head["version"])
            sha = self._write_blob(tenant, data)
            m["counter"] += 1
            entry = {"version": m["counter"], "sha256": sha, "size": len(data), "created_at": time.time(),
                     "actor": actor, "kind": kind, "parent_version": head["version"], "receipt_id": receipt_id}
            m["versions"].append(entry)
            self._save(tenant, doc_id, m)
            return self._vi(tenant, doc_id, entry)

    def create_derived(self, tenant: str, doc_id: str, data: bytes, *, actor: str, kind: str,
                       classification: Optional[str] = None) -> VersionInfo:
        """Store a connector-produced artifact (converted copy, export) as its OWN versioned document that lives
        only in the sidecar (no file is written into the customer's tree)."""
        with self._lock(tenant, doc_id):
            m = self._load(tenant, doc_id)
            if m is None:
                m = {"doc_id": safe_rel_path(doc_id), "counter": 0, "origin_sha256": "", "versions": [],
                     "classification": classification, "derived": True}
            sha = self._write_blob(tenant, data)
            m["counter"] += 1
            entry = {"version": m["counter"], "sha256": sha, "size": len(data), "created_at": time.time(),
                     "actor": actor, "kind": kind,
                     "parent_version": m["versions"][-1]["version"] if m["versions"] else 0}
            m["versions"].append(entry)
            self._save(tenant, doc_id, m)
            return self._vi(tenant, doc_id, entry)

    def receipt_log(self, tenant: str) -> Ledger:
        return Ledger(self._side(tenant) / "receipts.jsonl")

    def get_classification(self, tenant: str, doc_id: str) -> Optional[str]:
        m = self._load(tenant, doc_id)
        return (m or {}).get("classification")

    def set_classification(self, tenant: str, doc_id: str, label: str) -> None:
        with self._lock(tenant, doc_id):
            m = self._load(tenant, doc_id)
            if m is None:
                m = self._sync(tenant, doc_id)
            m["classification"] = label
            self._save(tenant, doc_id, m)

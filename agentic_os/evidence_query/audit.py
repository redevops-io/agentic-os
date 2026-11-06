"""Audit receipts for every evidence request: who, scope, statement hash, row count, outcome. The statement itself is not
stored (a hash and a length), so an audit log is not a second copy of what was queried; datasets carry the sha256 of the
evidence that answered. File sinks are hash-chained and fsync'd; a sink that cannot write fails the request closed."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from typing import List, Protocol, Tuple


class AuditSink(Protocol):
    def append(self, receipt: dict) -> dict: ...


def statement_sha256(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8", "replace")).hexdigest()


def new_receipt(kind: str, principal: str, scope: str, **fields) -> dict:
    return {"receipt_id": uuid.uuid4().hex, "ts": round(time.time(), 3), "kind": kind, "principal": principal or "",
            "scope": scope, **fields}


class MemoryAuditSink:
    def __init__(self) -> None:
        self.receipts: List[dict] = []

    def append(self, receipt: dict) -> dict:
        self.receipts.append(dict(receipt))
        return receipt


class JsonlAuditSink:
    """Append-only JSONL, each line carries ``prev`` (hash of the previous line) and ``hash`` (of its own body)."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)

    @staticmethod
    def _h(body: dict, prev: str) -> str:
        return hashlib.sha256((prev + json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)).encode()).hexdigest()

    def _last(self) -> str:
        prev = "0" * 64
        if os.path.exists(self.path):
            with open(self.path, "rb") as fh:
                for line in fh:
                    if line.strip():
                        prev = json.loads(line)["hash"]
        return prev

    def append(self, receipt: dict) -> dict:
        with self._lock:
            prev = self._last()
            body = dict(receipt)
            line = dict(body, prev=prev, hash=self._h(body, prev))
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, (json.dumps(line, default=str) + "\n").encode())
                os.fsync(fd)
            finally:
                os.close(fd)
            return receipt

    def verify(self) -> Tuple[bool, int]:
        prev, n = "0" * 64, 0
        if not os.path.exists(self.path):
            return True, 0
        with open(self.path, "rb") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                    h, p = obj.pop("hash"), obj.pop("prev")
                except (ValueError, KeyError):
                    return False, n
                if p != prev or self._h(obj, p) != h:
                    return False, n
                prev, n = h, n + 1
        return True, n

"""Immutable, hash-chained receipt ledger (append-only JSON lines).

Every transmit decision, proposal, write, publish and refusal is receipted. Each record carries ``seq``, ``ts``,
``prev`` (the previous record's hash) and ``hash`` = SHA-256 over the canonical record, so any edit or deletion of
history is detectable with ``verify_chain``. Appends are serialised by the portable lock file (``locking.FileLock``)
and fsynced. The ledger is also the durable idempotency record: ``find(kind=..., idempotency_key=...)``.

The ledger lives next to the documents (``<root>/<tenant>/.docedit/receipts.jsonl``) and is deliberately NOT removed
by uninstall: it is evidence about customer documents.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Iterator, Optional

from .errors import Conflict
from .locking import FileLock

GENESIS = "0" * 64


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def record_hash(rec: dict) -> str:
    return hashlib.sha256(_canon({k: v for k, v in rec.items() if k != "hash"})).hexdigest()


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _lock(self) -> FileLock:
        return FileLock(self.path.with_name(self.path.name + ".lock"))

    def records(self) -> Iterator[dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        yield json.loads(line)
        except FileNotFoundError:
            return

    def _tail(self) -> "tuple[int, str]":
        seq, h = 0, GENESIS
        for r in self.records():
            seq, h = r["seq"], r["hash"]
        return seq, h

    def append(self, rec: dict) -> dict:
        with self._lock():
            seq, prev = self._tail()
            full = {**rec, "receipt_id": rec.get("receipt_id") or uuid.uuid4().hex, "seq": seq + 1,
                    "ts": round(time.time(), 3), "prev": prev}
            full["hash"] = record_hash(full)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "ab") as f:
                f.write(_canon(full) + b"\n")
                f.flush()
                os.fsync(f.fileno())
            return full

    def find(self, **match) -> "list[dict]":
        return [r for r in self.records() if all(r.get(k) == v for k, v in match.items())]

    def verify_chain(self) -> bool:
        prev = GENESIS
        n = 0
        for r in self.records():
            n += 1
            if r.get("seq") != n or r.get("prev") != prev or r.get("hash") != record_hash(r):
                return False
            prev = r["hash"]
        return True

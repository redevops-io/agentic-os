"""Integrity anchors for durable execution and authority records.

A hash chain detects an edited, reordered or mid-chain-deleted record. It cannot detect the END of the chain being cut off: a
shorter chain is still a valid chain. An *anchor* is a second, small durable record that states how far the chain had got
(record count, last sequence number, running head hash). A reader compares the two: a chain shorter than its anchor was truncated,
and a chain whose first ``count`` records do not hash to the anchor's head was rewritten, even if it was re-chained.

What this detects, and what it does not:

* detected: records cut off the end of the log (or the log deleted); a record edited or replaced behind the anchor, including a
  re-chained edit; an anchor that is unreadable, malformed or (with a key configured) fails its MAC;
* not an error: a log AHEAD of its anchor (the process stopped between appending and anchoring): the anchor is refreshed by the
  next append;
* not detected: a log with no anchor yet (an anchor is created by the first append: trust on first use); and a writer who can
  write BOTH the log and its anchor, who can rewrite both consistently. With ``AGENTIC_OS_ANCHOR_KEY`` set the anchor carries an
  HMAC-SHA256, so that writer must also hold the key. The anchor is only as strong as its distance from the log's writer:
  keep it on another file system or host, on append-only or write-once storage, or copy its head into a system the log's writer
  cannot edit. By default it is a file next to the log (``<log>.anchor``), which separates it from the log's bytes, not from its
  writer.

Nothing here requires a secret: without a key the anchor is a plain checkpoint.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass

from .._platform import files as _files

ENV_ANCHOR_KEY = "AGENTIC_OS_ANCHOR_KEY"
ANCHOR_VERSION = 1
GENESIS = "sha256:genesis"


class IntegrityError(RuntimeError):
    """A durable record failed an integrity check. Callers fail closed."""


class LogIntegrityError(IntegrityError):
    """The event log is shorter than, or disagrees with, its anchor, or the anchor itself is invalid."""


@dataclass(frozen=True)
class IntegrityReport:
    ok: bool
    status: str                 # unanchored | ok | lagging | truncated | mismatch | invalid | chain
    reason: str = ""
    count: int = 0              # records in the log
    anchored: int = 0           # records the anchor vouches for (0 when there is no anchor)


def anchor_key_from_env(env=None) -> "bytes | None":
    raw = (os.environ if env is None else env).get(ENV_ANCHOR_KEY, "")
    return raw.encode("utf-8") if raw else None


def chain_step(head: str, entry_hash: str) -> str:
    """The running head over a sequence of entries: sha256(head | entry_hash)."""
    return "sha256:" + hashlib.sha256((head + "|" + entry_hash).encode("ascii", "replace")).hexdigest()


def _canonical(record: dict) -> bytes:
    return json.dumps({k: v for k, v in record.items() if k != "mac"}, sort_keys=True, separators=(",", ":")).encode("utf-8")


def mac_of(key: bytes, record: dict) -> str:
    return hmac.new(key, _canonical(record), hashlib.sha256).hexdigest()


class AnchorFile:
    """One small JSON record, replaced atomically (temp file, optional fsync, rename, optional directory fsync).

    ``read()`` returns None when no anchor exists yet, the record when it is intact, and raises ``IntegrityError`` when the file
    exists but is unreadable, not an anchor, or fails its MAC (a configured key requires a valid MAC)."""

    def __init__(self, path: str, key: "bytes | None" = None) -> None:
        self.path = os.fspath(path)
        self.key = key

    def read(self) -> "dict | None":
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, "rb") as fh:
                rec = json.loads(fh.read().decode("utf-8"))
        except (OSError, ValueError) as e:
            raise IntegrityError(f"anchor {self.path!r} is unreadable: {e}") from None
        if (not isinstance(rec, dict) or rec.get("version") != ANCHOR_VERSION or not isinstance(rec.get("count"), int)
                or isinstance(rec.get("count"), bool) or rec["count"] < 0 or not isinstance(rec.get("head"), str)):
            raise IntegrityError(f"anchor {self.path!r} is not a valid anchor record")
        if self.key is not None:
            mac = rec.get("mac")
            if not isinstance(mac, str) or not hmac.compare_digest(mac, mac_of(self.key, rec)):
                raise IntegrityError(f"anchor {self.path!r} fails its MAC (or was written without the configured key)")
        return rec

    def write(self, record: dict, *, fsync: bool) -> None:
        rec = {"version": ANCHOR_VERSION, **record}
        if self.key is not None:
            rec["mac"] = mac_of(self.key, rec)
        data = (json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        d = os.path.dirname(self.path) or "."
        fd, tmp = _files.create_private_temp(d, os.path.basename(self.path) + ".", ".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                if fsync:
                    os.fsync(f.fileno())
            _files.replace_file(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        if fsync:
            _files.fsync_dir(d)

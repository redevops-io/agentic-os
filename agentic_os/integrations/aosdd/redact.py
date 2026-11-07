"""Credential redaction for the AOS-DD runtime integration.

Every sink that can outlive the process (the hash-chained mission ledger, receipts, exception text, reconstruction
output, logs) is scrubbed by VALUE: a ``Redactor`` knows the exact secret strings of the loaded configuration (and their
URL-quoted / base64 forms, and the ``key:secret`` pair an ERPNext header carries) and replaces any occurrence. Shape
patterns (``Authorization: token a:b``, ``Bearer <jwt>``, ``api_access_token: ...``) are scrubbed too, so a credential
the redactor was never told about still does not survive in an error text. A redactor never raises and never returns
the secret."""
from __future__ import annotations

import base64
import re
import urllib.parse
from typing import Any, Iterable

MARK = "[REDACTED]"
_MIN_LEN = 6      # shorter strings would shred ordinary text; real credentials are far longer

_SHAPES = (
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:token\s+|bearer\s+|basic\s+)?)[^\s,;'\"}]+"),
    re.compile(r"(?i)(api[_-]?access[_-]?token\s*[:=]\s*)[^\s,;'\"}]+"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9_\-\.=]{12,}"),
    re.compile(r"(?i)\b(token\s+)[A-Za-z0-9_\-\.]{6,}:[A-Za-z0-9_\-\.]{6,}"),
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")


def _forms(secret: str) -> set[str]:
    out = {secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)}
    raw = secret.encode()
    out.add(base64.b64encode(raw).decode())
    out.add(base64.urlsafe_b64encode(raw).decode().rstrip("="))
    out.add(raw.hex())
    return {o for o in out if len(o) >= _MIN_LEN}


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()):
        self._forms: list[str] = []
        self.add(*secrets)

    def add(self, *secrets: str) -> None:
        seen = set(self._forms)
        for s in secrets:
            if isinstance(s, str) and len(s) >= _MIN_LEN:
                seen |= _forms(s)
        self._forms = sorted(seen, key=len, reverse=True)       # longest first: a pair before its parts

    def add_pair(self, a: str, b: str) -> None:
        """ERPNext sends ``token <key>:<secret>``; scrub the joined form as well as both halves."""
        if a and b:
            self.add(f"{a}:{b}", a, b)

    def text(self, s: str) -> str:
        if not isinstance(s, str) or not s:
            return s
        for f in self._forms:
            if f in s:
                s = s.replace(f, MARK)
        for rx in _SHAPES:
            s = rx.sub(r"\g<1>" + MARK, s)
        return _JWT.sub(MARK, s)

    def scrub(self, obj: Any) -> Any:
        """Recursively scrub strings in dict keys and values, lists, tuples; other scalars pass through."""
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            return {(self.text(k) if isinstance(k, str) else k): self.scrub(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.scrub(v) for v in obj]
        return obj

    def contains(self, s: str) -> bool:
        return any(f in s for f in self._forms) if isinstance(s, str) else False


NULL = Redactor()

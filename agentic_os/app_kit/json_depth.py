"""Bound the nesting depth of a JSON request body before anything parses it (rc2, G5).

Python's JSON decoder (and the validation layers on top of it) recurse once per nesting level, so a body such as ``[[[[...`` a thousand
levels deep raised ``RecursionError`` inside request parsing and the caller got an HTTP 500. This pure-ASGI middleware counts ``[`` / ``{``
nesting while the body streams in (string contents and escapes are skipped, nothing is buffered or parsed) and answers a controlled
``400 {"detail": "JSON nested too deeply"}`` as soon as the depth passes ``max_depth``; the application is stopped, so it
never sees the body. It applies to POST / PUT / PATCH / DELETE requests whose content type is JSON, absent or text/* (a handler may parse any of them as JSON). Form, multipart and binary bodies are not inspected. It is installed OUTSIDE the
route-authorization layer, so the authorization layer never parses a hostile body either.
"""
from __future__ import annotations

import re

MAX_JSON_DEPTH = 64

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


_OUTSIDE = re.compile(rb'[\[\]{}"]')     # the only bytes that matter outside a string
_INSIDE = re.compile(rb'["\\]')           # the only bytes that matter inside one: the closing quote and a backslash


class JsonDepthScanner:
    """Incremental scanner: feed it chunks, it reports whether the depth limit was passed. It jumps from one significant byte to the next with
    a compiled regular expression, so the cost is proportional to the number of brackets and quotes, not to the size of the strings."""

    __slots__ = ("max_depth", "depth", "in_string", "escaped")

    def __init__(self, max_depth: int = MAX_JSON_DEPTH) -> None:
        self.max_depth, self.depth, self.in_string, self.escaped = max_depth, 0, False, False

    def feed(self, chunk: bytes) -> bool:
        """True when the nesting depth is now greater than ``max_depth``."""
        n = len(chunk)
        pos = 0
        depth, in_string, escaped, limit = self.depth, self.in_string, self.escaped, self.max_depth
        while pos < n:
            if in_string:
                if escaped:                       # the byte after a backslash is part of the string, whatever it is
                    escaped = False
                    pos += 1
                    continue
                m = _INSIDE.search(chunk, pos)
                if m is None:
                    break
                pos = m.end()
                if chunk[m.start()] == 0x22:
                    in_string = False
                else:
                    escaped = True
            else:
                m = _OUTSIDE.search(chunk, pos)
                if m is None:
                    break
                pos = m.end()
                b = chunk[m.start()]
                if b == 0x22:
                    in_string = True
                elif b == 0x5B or b == 0x7B:
                    depth += 1
                    if depth > limit:
                        self.depth, self.in_string, self.escaped = depth, in_string, escaped
                        return True
                elif depth:
                    depth -= 1
        self.depth, self.in_string, self.escaped = depth, in_string, escaped
        return False


class _TooDeep(Exception):
    """Raised into the application's receive() once the 400 is out, so the handler stops instead of parsing an empty body."""


class JsonDepthMiddleware:
    def __init__(self, app, max_depth: int = MAX_JSON_DEPTH) -> None:
        self.app, self.max_depth = app, max_depth

    @staticmethod
    def _applies(scope) -> bool:
        if scope["type"] != "http" or scope.get("method") not in _BODY_METHODS:
            return False
        ctype = b""
        for k, v in scope.get("headers") or []:
            if k == b"content-type":
                ctype = v.lower()
                break
        # JSON, no declared type, or text/*: Starlette's request.json() does not look at the content type, so a handler may parse a text/plain body as JSON
        return not ctype or b"json" in ctype or ctype.startswith(b"text/")

    async def __call__(self, scope, receive, send):
        if not self._applies(scope):
            return await self.app(scope, receive, send)
        scanner = JsonDepthScanner(self.max_depth)
        started = False
        rejected = False

        async def _send(msg):
            nonlocal started
            if rejected:
                return                      # the 400 is already out; drop whatever the app tries next
            started = started or msg["type"] == "http.response.start"
            await send(msg)

        async def _reject():
            nonlocal rejected
            if started or rejected:
                return
            rejected = True
            body = b'{"detail":"JSON nested too deeply"}'
            await send({"type": "http.response.start", "status": 400,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})

        async def _receive():
            msg = await receive()
            if msg["type"] == "http.request" and scanner.feed(msg.get("body", b"")):
                await _reject()             # answer now, then stop the application: it never sees the body
                raise _TooDeep()
            return msg

        try:
            await self.app(scope, _receive, _send)
        except _TooDeep:
            pass

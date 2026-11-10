"""Bound the nesting depth of a JSON request body before anything parses it (rc2, G5).

Python's JSON decoder (and the validation layers on top of it) recurse once per nesting level, so a body such as ``[[[[...`` a thousand
levels deep raised ``RecursionError`` inside request parsing and the caller got an HTTP 500. This pure-ASGI middleware counts ``[`` / ``{``
nesting while the body streams in (string contents and escapes are skipped, nothing is buffered or parsed) and answers a controlled
``400 {"detail": "JSON nested too deeply"}`` as soon as the depth passes ``max_depth``; the application is told the client went away, so it
never sees the body. It applies to POST / PUT / PATCH / DELETE requests whose content type is JSON (or absent). It is installed OUTSIDE the
route-authorization layer, so the authorization layer never parses a hostile body either.
"""
from __future__ import annotations

MAX_JSON_DEPTH = 64

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class JsonDepthScanner:
    """Incremental scanner: feed it chunks, it reports whether the depth limit was passed."""

    __slots__ = ("max_depth", "depth", "in_string", "escaped")

    def __init__(self, max_depth: int = MAX_JSON_DEPTH) -> None:
        self.max_depth, self.depth, self.in_string, self.escaped = max_depth, 0, False, False

    def feed(self, chunk: bytes) -> bool:
        """True when the nesting depth is now greater than ``max_depth``."""
        if not chunk:
            return False
        if not self.in_string and self.depth + chunk.count(b"[") + chunk.count(b"{") <= self.max_depth:
            # cannot exceed the limit even if every opener were nested: only the string state needs tracking, and only if a quote is present
            if b'"' not in chunk:
                self.depth = max(0, self.depth + chunk.count(b"[") + chunk.count(b"{") - chunk.count(b"]") - chunk.count(b"}"))
                return False
        depth, in_string, escaped, limit = self.depth, self.in_string, self.escaped, self.max_depth
        for b in chunk:
            if in_string:
                if escaped:
                    escaped = False
                elif b == 0x5C:        # backslash
                    escaped = True
                elif b == 0x22:        # closing quote
                    in_string = False
            elif b == 0x22:
                in_string = True
            elif b == 0x5B or b == 0x7B:
                depth += 1
                if depth > limit:
                    self.depth, self.in_string, self.escaped = depth, in_string, escaped
                    return True
            elif b == 0x5D or b == 0x7D:
                if depth:
                    depth -= 1
        self.depth, self.in_string, self.escaped = depth, in_string, escaped
        return False


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
        return not ctype or b"json" in ctype

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
                await _reject()             # answer now, then tell the app the client is gone
                return {"type": "http.disconnect"}
            return msg

        await self.app(scope, _receive, _send)

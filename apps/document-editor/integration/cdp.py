"""Tiny stdlib Chrome DevTools Protocol driver (headless Chrome/Chromium; no third-party packages).

Only used by the local integration suite to prove that a REAL editor session opens a document: load a page that
embeds the ONLYOFFICE editor with the connector-signed config and wait for the ``onDocumentReady`` event.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request


def find_chrome() -> "str | None":
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if os.path.exists(p):
            return p
    return None


class _WS:
    def __init__(self, url: str):
        m = re.match(r"ws://([^/:]+):(\d+)(/.*)", url)
        host, port, path = m.group(1), int(m.group(2)), m.group(3)
        self.s = socket.create_connection((host, port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(4096)
        if b" 101 " not in buf.split(b"\r\n")[0]:
            raise RuntimeError("websocket upgrade refused")
        self.rest = buf.split(b"\r\n\r\n", 1)[1]

    def send(self, text: str) -> None:
        data = text.encode()
        n = len(data)
        hdr = bytearray([0x81])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.s.sendall(bytes(hdr) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n: int) -> bytes:
        while len(self.rest) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise RuntimeError("websocket closed")
            self.rest += chunk
        out, self.rest = self.rest[:n], self.rest[n:]
        return out

    def recv(self) -> str:
        msg = b""
        while True:
            b1, b2 = self._read(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            msg += payload
            if b1 & 0x80:
                return msg.decode("utf-8", "replace")


def run_page(url: str, probe_js: str, *, timeout_s: float = 90.0) -> str:
    """Open ``url`` in headless Chrome and return the first non-empty, non-'PENDING' value of ``probe_js``."""
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("no Chrome/Chromium found")
    prof = tempfile.mkdtemp(prefix="docedit-chrome-")
    proc = subprocess.Popen([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--remote-debugging-port=0",
                             f"--user-data-dir={prof}", "--no-first-run", "--disable-extensions", "about:blank"],
                            stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
    try:
        port = None
        t0 = time.time()
        while time.time() - t0 < 30 and port is None:
            line = proc.stderr.readline()
            m = re.search(r"DevTools listening on ws://127\.0\.0\.1:(\d+)/", line or "")
            if m:
                port = int(m.group(1))
        if port is None:
            raise RuntimeError("chrome did not start")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?{url}", method="PUT")
        target = json.loads(urllib.request.urlopen(req, timeout=10).read())
        ws = _WS(target["webSocketDebuggerUrl"])
        n, last = 0, ""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            n += 1
            ws.send(json.dumps({"id": n, "method": "Runtime.evaluate",
                                "params": {"expression": probe_js, "returnByValue": True}}))
            while True:
                r = json.loads(ws.recv())
                if r.get("id") == n:
                    break
            last = str(r.get("result", {}).get("result", {}).get("value", ""))
            if last and last != "PENDING":
                return last
            time.sleep(1.0)
        return last or "TIMEOUT"
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(prof, ignore_errors=True)

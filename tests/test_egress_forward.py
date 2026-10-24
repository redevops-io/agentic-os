"""The inbound forwarder relays one port to one fixed target and nothing else."""
from __future__ import annotations

import asyncio
import importlib.util
import socket
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("egress_forward", ROOT / "deploy" / "egress" / "forward.py")
fw = importlib.util.module_from_spec(spec)
sys.modules["egress_forward"] = fw
spec.loader.exec_module(fw)


def echo_server():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(8)
    seen = []

    def run():
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return
            seen.append(1)

            def one(c=c):
                try:
                    while (d := c.recv(4096)):
                        c.sendall(b"echo:" + d)
                finally:
                    c.close()
            threading.Thread(target=one, daemon=True).start()
    threading.Thread(target=run, daemon=True).start()
    return s, seen


class Fwd:
    def __init__(self, target_port, idle_s=5.0):
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            self.server = self.loop.run_until_complete(fw.serve("127.0.0.1", 0, "127.0.0.1", target_port, idle_s))
            self.port = self.server.sockets[0].getsockname()[1]
            ready.set()
            self.loop.run_forever()
        threading.Thread(target=run, daemon=True).start()
        assert ready.wait(5)

    def stop(self):
        self.loop.call_soon_threadsafe(self.server.close)
        self.loop.call_soon_threadsafe(self.loop.stop)


def test_relays_bytes_both_ways_to_the_fixed_target():
    origin, seen = echo_server()
    f = Fwd(origin.getsockname()[1])
    try:
        c = socket.create_connection(("127.0.0.1", f.port), timeout=5)
        c.sendall(b"hello")
        assert c.recv(100) == b"echo:hello"
        c.sendall(b"again")
        assert c.recv(100) == b"echo:again"
        c.close()
        assert len(seen) == 1
    finally:
        f.stop(); origin.close()


def test_a_dead_target_closes_the_client_without_hanging():
    dead = socket.socket(); dead.bind(("127.0.0.1", 0)); port = dead.getsockname()[1]; dead.close()
    f = Fwd(port)
    try:
        c = socket.create_connection(("127.0.0.1", f.port), timeout=5)
        c.sendall(b"x")
        assert c.recv(10) == b""
        c.close()
    finally:
        f.stop()


def test_there_is_no_way_to_ask_it_for_another_destination():
    """It speaks no protocol: whatever the client sends is relayed to the one target, never interpreted as an address."""
    origin, seen = echo_server()
    other, other_seen = echo_server()
    f = Fwd(origin.getsockname()[1])
    try:
        c = socket.create_connection(("127.0.0.1", f.port), timeout=5)
        c.sendall(f"CONNECT 127.0.0.1:{other.getsockname()[1]} HTTP/1.1\r\n\r\n".encode())
        assert c.recv(200).startswith(b"echo:CONNECT")
        c.close()
        assert other_seen == []
    finally:
        f.stop(); origin.close(); other.close()


def test_cli_rejects_a_malformed_listen_or_target():
    assert fw.main(["--listen", "nope", "--to", "svc:3000"]) == 2
    assert fw.main(["--listen", "0.0.0.0:3000", "--to", "svc"]) == 2

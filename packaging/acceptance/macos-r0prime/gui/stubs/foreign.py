#!/opt/homebrew/bin/python3
"""A foreign process that looks like a sidecar to the launcher: prints a well-formed ready record for a port it
listens on (random secret it does not share). Mode (env RDO_FOREIGN_MODE): 404 | wrongproof | hang."""
import base64, json, os, socket, sys, threading, time
mode = os.environ.get("RDO_FOREIGN_MODE", "404")
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); srv.bind(("127.0.0.1", 0)); srv.listen(64)
port = srv.getsockname()[1]
secret = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
print(json.dumps({"v": 1, "proto": "agentic-os-local-capability/1", "event": "ready", "host": "127.0.0.1", "port": port, "pid": os.getpid(), "base_path": "", "secret": secret}), flush=True)
def serve(c):
    try:
        c.settimeout(5); data = c.recv(65536)
        if mode == "hang": time.sleep(120); return
        if mode == "404":
            body = b'{"detail":"Not Found"}'; c.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
        else:
            nonce = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode()
            body = json.dumps({"v": 1, "proto": "agentic-os-local-capability/1", "proof": base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode(), "server_nonce": nonce, "expires_in": 30}).encode()
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
    except Exception: pass
    finally:
        try: c.close()
        except Exception: pass
while True:
    c, _ = srv.accept(); threading.Thread(target=serve, args=(c,), daemon=True).start()

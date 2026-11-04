#!/usr/bin/env python3
"""Negative security cases against the BUILT sidecar of the candidate, started through the launcher's real
start code (gui-launcher-driver). Labels: OBSERVED-HEADLESS (API) and OBSERVED-BROWSER (controlled
headless Chromium, disposable profile). Usage: python3 sc_negative.py <outdir>"""
import glob, json, os, re, socket, subprocess, sys, time, threading, http.server, socketserver, signal
import guilib as g

out = sys.argv[1]; os.makedirs(out, exist_ok=True)
R = g.Results("negative")
ts = time.strftime("%H%M%S")
base = f"{g.GUI_WORK}/runs/negative-{ts}"; home = base + "/home"; tmp = base + "/tmp"
os.makedirs(home); os.makedirs(tmp)
env = g.rd_env(home=home, tmp=tmp, extra={"RDO_SHIM_LOG": base + "/open-shim.log", "RDO_BROWSER_OUT": base + "/browser-out"})

def signin(d):
    t = d.cmd("start"); assert t.get("ok"), t
    mc = d.cmd("mintcode")
    if "code" not in mc: time.sleep(2); mc = d.cmd("mintcode")
    assert "code" in mc, mc
    s, j, h = g.exchange(t["port"], mc["code"]); assert s == 200, (s, j)
    return t["port"], j["session_token"], j["csrf_token"]

def raw(port, req_bytes, timeout=5):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout); s.sendall(req_bytes)
    buf = b""
    try:
        while True:
            c = s.recv(65536)
            if not c: break
            buf += c
    except Exception: pass
    s.close()
    m = re.match(rb"HTTP/1\.[01] (\d+)", buf)
    return (int(m[1]) if m else -1), buf

dA = g.Driver(env=env); pA, tokA, csrfA = signin(dA)
dB = g.Driver(env=env); pB, tokB, csrfB = signin(dB)
R.add("N-00", "two launcher instances run side by side: two sidecars, two ephemeral ports, each stoppable on its own", pA != pB and len(g.procs()) == 4, "OBSERVED-HEADLESS", f"ports {pA},{pB}")
AH = {"X-Agentic-Session": tokA}

# --- cross-instance replay
s, _, _ = g.http(pB, "GET", "/api/projects", {"X-Agentic-Session": tokA}); R.add("N-01a", "session token of instance A replayed against instance B (other port) -> 401", s == 401, "OBSERVED-HEADLESS", str(s))
mcA = dA.cmd("mintcode")
if "code" not in mcA: print("MINTCODE FAILED:", mcA, flush=True); time.sleep(2); mcA = dA.cmd("mintcode"); print("retry:", {k: v for k, v in mcA.items() if k != "code"}, flush=True)
s, j, _ = g.exchange(pB, mcA["code"]); R.add("N-01b", "launch code minted by A replayed against B -> 401", s == 401, "OBSERVED-HEADLESS", str(s))
for host in (f"localhost:{pA}", f"[::1]:{pA}"):
    s, _, _ = g.http(pA, "GET", "/api/projects", {"X-Agentic-Session": tokA}, host=host)
    R.add("N-01c", f"A's token presented with a different (still allow-listed) Host form {host} -> 401 (session bound to its Host)", s == 401, "OBSERVED-HEADLESS", str(s))
s, _, _ = g.http(pA, "GET", "/api/projects", AH); R.add("N-01d", "control: A's token against A still 200", s == 200, "OBSERVED-HEADLESS", str(s))
sA = g.http(pA, "GET", "/api/local/session", AH); R.add("N-01e", "GET /api/local/session with the header returns CSRF token, no Set-Cookie", sA[0] == 200 and "set-cookie" not in sA[1], "OBSERVED-HEADLESS", str(sA[0]))

# --- Host forms (DNS rebinding and parser tricks), raw sockets
hostforms = ["evil.example:%d" % pA, "127.0.0.1", "127.0.0.1.evil.test:%d" % pA, "localhost.:%d" % pA, "0x7f000001:%d" % pA, "127.1:%d" % pA,
             "2130706433:%d" % pA, "[::ffff:127.0.0.1]:%d" % pA, "127.0.0.1:%d@evil.test" % pA, "evil.test@127.0.0.1:%d" % pA, "127.0.0.1:1", "", "127.0.0.1:%d, evil.test" % pA,
             "127.0.0.1:%d " % pA + "evil", "LOCALHOST.:%d" % pA, "evil.example"]
bad = []
for hf in hostforms:
    st, buf = raw(pA, f"GET / HTTP/1.1\r\nHost: {hf}\r\nConnection: close\r\n\r\n".encode())
    if st == 200: bad.append(hf)
R.add("N-02a", f"{len(hostforms)} rebinding/parser Host forms on GET / : none answered 200", bad == [], "OBSERVED-HEADLESS", f"accepted={bad}")
st, buf = raw(pA, f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{pA}\r\nHost: evil.example\r\nConnection: close\r\n\r\n".encode())
R.add("N-02b", "duplicate Host header -> not 200", st != 200, "OBSERVED-HEADLESS", str(st))
st, buf = raw(pA, f"GET http://evil.example:{pA}/api/projects HTTP/1.1\r\nHost: 127.0.0.1:{pA}\r\nConnection: close\r\n\r\n".encode())
R.add("N-02c", "absolute-URI request target naming another host, with a good Host -> not 200 with data", st != 200 or b"Customer" not in buf, "OBSERVED-HEADLESS", str(st))
st, buf = raw(pA, f"GET /api/projects HTTP/1.0\r\n\r\n".encode()); R.add("N-02d", "HTTP/1.0 without Host -> not 200", st != 200, "OBSERVED-HEADLESS", str(st))
for hf in (f"127.0.0.1:{pA}", f"localhost:{pA}", f"[::1]:{pA}"):
    st, buf = raw(pA, f"GET / HTTP/1.1\r\nHost: {hf}\r\nConnection: close\r\n\r\n".encode())
    R.add("N-02e", f"control: allow-listed Host {hf} on GET /", st == 200 or hf.startswith("[::1]"), "OBSERVED-HEADLESS", str(st) + (" (sidecar is bound to 127.0.0.1 only; the Host form is accepted by the gate)" if st == 200 and hf.startswith("[") else ""))

# --- Origin / Sec-Fetch / CORS / content type
def api(method, path, hdrs=None, body=None, tok=True, csrf=None):
    h = dict(hdrs or {})
    if tok: h["X-Agentic-Session"] = tokA
    if csrf is not None: h["X-Agentic-CSRF"] = csrf
    return g.http(pA, method, path, h, body)
s, h, b = api("GET", "/api/projects", {"Origin": "http://evil.example"}); R.add("N-03a", "valid session + foreign Origin -> 403", s == 403, "OBSERVED-HEADLESS", str(s))
s, h, b = api("GET", "/api/projects", {"Origin": "null"}); R.add("N-03b", "Origin: null -> 403", s == 403, "OBSERVED-HEADLESS", str(s))
s, h, b = api("GET", "/api/projects", {"Sec-Fetch-Site": "cross-site"}); R.add("N-03c", "Sec-Fetch-Site cross-site -> 403", s == 403, "OBSERVED-HEADLESS", str(s))
s, h, b = api("GET", "/api/projects", {"Sec-Fetch-Site": "same-site"}); R.add("N-03d", "Sec-Fetch-Site same-site (another local port) -> 403", s == 403, "OBSERVED-HEADLESS", str(s))
s, h, b = api("OPTIONS", "/api/projects", {"Origin": "http://evil.example", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-agentic-session"}, tok=False)
R.add("N-03e", "CORS preflight -> 403 and no Access-Control-* header", s == 403 and not any(k.startswith("access-control") for k in h), "OBSERVED-HEADLESS", str(s))
s, h, b = g.http(pA, "POST", "/api/local/session", {"Content-Type": "text/plain"}, json.dumps({"code": "x" * 43})); R.add("N-03f", "text/plain POST to the exchange endpoint (cross-site form/no-cors shape) -> 415", s == 415, "OBSERVED-HEADLESS", str(s))
s, h, b = g.http(pA, "POST", "/api/local/session", {"Content-Type": "application/x-www-form-urlencoded"}, "code=" + "x" * 43); R.add("N-03g", "form-encoded POST to the exchange endpoint -> 415", s == 415, "OBSERVED-HEADLESS", str(s))
s, h, b = api("POST", "/api/sources/confirm", {"Content-Type": "text/plain"}, '{"sources":[]}', tok=False); R.add("N-03h", "the original attack shape (anonymous no-cors POST /api/sources/confirm) -> 401/403, never 200", s in (401, 403), "OBSERVED-HEADLESS", str(s))
s, h, b = api("GET", "/api/projects"); R.add("N-03i", "no Access-Control-* header on a normal 200", not any(k.startswith("access-control") for k in h), "OBSERVED-HEADLESS", str(s))
# --- CSRF
body = '{"sources":[]}'; ct = {"Content-Type": "application/json"}
s, _, _ = api("POST", "/api/sources/confirm", ct, body); R.add("N-04a", "POST with session but WITHOUT the CSRF header -> 403 csrf_invalid", s == 403, "OBSERVED-HEADLESS", str(s))
s, _, _ = api("POST", "/api/sources/confirm", ct, body, csrf="wrong"); R.add("N-04b", "POST with a WRONG CSRF header -> 403", s == 403, "OBSERVED-HEADLESS", str(s))
s, _, _ = api("POST", "/api/sources/confirm", ct, body, csrf=csrfA); R.add("N-04c", "control: POST with both headers -> 200", s == 200, "OBSERVED-HEADLESS", str(s))
s, _, _ = api("POST", "/api/sources/confirm", ct, body, csrf=csrfB); R.add("N-04d", "CSRF token of instance B used with A's session -> 403", s == 403, "OBSERVED-HEADLESS", str(s))
# --- no-credential sweep
paths = ["/api/projects", "/api/projects/customer-ops/overview", "/api/sources", "/api/apps", "/api/agent-gateway/x", "/docs", "/redoc", "/openapi.json", "/api/nope", "/.git/config", "/api/../api/projects", "/%2e%2e/api/projects", "/api//projects", "/API/projects", "/api/projects/", "/api/local/session", "/api/local/launch-code"]
codes = {p: g.http(pA, "GET", p)[0] for p in paths}
R.add("N-05", "no-credential GET sweep over %d paths (incl. traversal forms): none 200 except none; all 401/404/405" % len(paths), all(c in (401, 404, 405) for c in codes.values()), "OBSERVED-HEADLESS", json.dumps(codes))
codes2 = {m: g.http(pA, m, "/api/projects")[0] for m in ("POST", "PUT", "DELETE", "PATCH")}
R.add("N-05b", "no-credential POST/PUT/DELETE/PATCH -> 401/403/405", all(c in (401, 403, 405) for c in codes2.values()), "OBSERVED-HEADLESS", json.dumps(codes2))
# --- launch code replay / expiry
mc = dA.cmd("mintcode"); code1 = mc["code"]; t_mint = time.time()
s1, j1, _ = g.exchange(pA, code1); s2, j2, _ = g.exchange(pA, code1)
R.add("N-06a", "launch code replay: first exchange 200, second 401", s1 == 200 and s2 == 401, "OBSERVED-HEADLESS", f"{s1},{s2}")
expiring = dB.cmd("mintcode")["code"]; t_exp = time.time()   # exchanged after 62 s, at the end, on the same instance B
# --- rate limits (SR-04 / bypass)
valid_keep = dA.cmd("mintcode")["code"]
for i in range(30): g.http(pA, "POST", "/api/local/session", {"Content-Type": "application/json", "Origin": "http://evil.example"}, json.dumps({"code": "z" * 43}))
s, _, _ = g.exchange(pA, "z" * 43); R.add("N-07a", "30 CROSS-ORIGIN bad exchanges do not burn the failure budget (next bad same-origin attempt is 401, not 429)", s == 401, "OBSERVED-HEADLESS", str(s))
sts = [g.exchange(pA, "y" * 43)[0] for _ in range(15)]
R.add("N-07b", "same-origin bad codes: rate limited (429) once the budget of 10 failures in 60 s is spent (2 failures were already counted from N-06a/N-07a)", 429 in sts and sts.index(429) <= 9, "OBSERVED-HEADLESS", str(sts))
s, h, _ = g.http(pA, "POST", "/api/local/session", {"Content-Type": "application/json"}, json.dumps({"code": "q" * 43}))
R.add("N-07c", "429 carries Retry-After", s == 429 and "retry-after" in h, "OBSERVED-HEADLESS", f"{s} retry-after={h.get('retry-after')}")
s, j, _ = g.exchange(pA, valid_keep); R.add("N-07d", "LOCKOUT BYPASS CHECK: a correct unexpired code is still served while the budget is spent (SR-04): 200", s == 200, "OBSERVED-HEADLESS", str(s))
sts = [g.http(pA, "POST", "/api/local/launch-code", {"Content-Type": "application/json", "X-Launcher-Proof": "A" * 43}, json.dumps({"server_nonce": "B" * 22}))[0] for _ in range(14)]
R.add("N-07e", "bad launcher proofs on the mint endpoint: 401 then 429", 429 in sts, "OBSERVED-HEADLESS", str(sts))
mc = dA.cmd("mintcode"); R.add("N-07f", "launcher's own Open (handshake + correct-proof mint) still succeeds while the mint budget is spent by a local attacker", mc.get("ok") is True, "OBSERVED-HEADLESS", str(mc.get("ok")))
op = dA.cmd("open"); R.add("N-07g", "launcher Open (full path incl. redirect file) still ok under that spam", op.get("ok") is True, "OBSERVED-HEADLESS", str(op.get("ok")))
# --- flood: Busy vs Untrusted
res = {}
for n in (8, 32):
    dB.cmd(f"flood {n}"); time.sleep(0.5)
    opens = [dB.cmd("open") for _ in range(3)]
    stat = dB.cmd("status"); fs = dB.cmd("floodstop")
    kinds = []
    for o in opens:
        e = o.get("error", "")
        kinds.append("ok" if o.get("ok") else "busy" if "busy" in e else "untrusted" if "prove its identity" in e else "unreachable/other:" + e[:80])
    res[n] = dict(opens=kinds, alive_after=stat.get("alive"), flood=fs)
    res[n]["ports_drained_after_s"] = g.drain_ports()
    if not stat.get("alive"): break
R.add("N-08", "handshake flood against a RUNNING sidecar (8 and 32 threads; 64 is covered by the launcher's own ignored test): Open never reports 'untrusted', the sidecar is never killed, and Open works again after the flood", all("untrusted" not in k for v in res.values() for k in v["opens"]) and all(v["alive_after"] for v in res.values()) and dB.cmd("open").get("ok") is True, "OBSERVED-HEADLESS", json.dumps(res))
# --- two Open clicks
n0 = len(glob.glob(base + "/browser-out/flow-*.json"))
o1 = dA.cmd("open"); o2 = dA.cmd("open")
fl = g.wait_for(lambda: len(glob.glob(base + "/browser-out/flow-*.json")) >= n0 + 2, 30)
last = sorted(glob.glob(base + "/browser-out/flow-*.json"))[-2:]
rr = [json.load(open(x)) for x in last]
R.add("N-09", "two Open clicks in a row: two redirect files, two independent browsers each land signed in (distinct single-use codes)", o1.get("ok") and o2.get("ok") and o1["file"] != o2["file"] and all(x.get("api_status") == 200 for x in rr), "OBSERVED-BROWSER", f"api={[x.get('api_status') for x in rr]}")
# --- expired code (>60 s): minted at t_exp on instance B, which was never restarted
left = 62 - (time.time() - t_exp)
if left > 0: time.sleep(left)
s, j, _ = g.exchange(pB, expiring)
R.add("N-06b", "expired launch code (exchanged 62 s after minting) -> 401", s == 401, "OBSERVED-HEADLESS", f"{s} after {time.time()-t_exp:.0f}s")
dA.cmd("stop"); dB.cmd("stop"); dA.quit(); dB.quit()
time.sleep(0.5)
R.add("N-99", "after all negative tests: 0 sidecar processes, no listener, only _MEI left by none", len(g.procs()) == 0 and g.mei_dirs(tmp) == [], "OBSERVED-HEADLESS", f"procs={len(g.procs())} mei={g.mei_dirs(tmp)}")
R.save(out + "/negative.json")

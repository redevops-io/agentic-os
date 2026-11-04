#!/usr/bin/env python3
"""Lifecycle scenarios through the launcher's real start/open/stop code (gui-launcher-driver) against the
copied candidate .app. Evidence label OBSERVED-HEADLESS (launcher source modules + built sidecar; not the
shipped launcher window). Usage: python3 sc_lifecycle.py <outdir>"""
import glob, json, os, re, stat, subprocess, sys, time, signal
import guilib as g

out = sys.argv[1]; os.makedirs(out, exist_ok=True)
R = g.Results("lifecycle")
ts = time.strftime("%H%M%S")
base = f"{g.GUI_WORK}/runs/lifecycle-{ts}"; home = base + "/home"; tmp = base + "/tmp"
os.makedirs(home); os.makedirs(tmp)
env = g.rd_env(home=home, tmp=tmp, extra={"RDO_SHIM_LOG": base + "/open-shim.log", "RDO_BROWSER_OUT": base + "/browser-out"})

def tree():
    return [p for p in g.procs()]

def clean_state():
    return (len(tree()) == 0, g.mei_dirs(tmp) == [])

# ---------- L-01 start ----------
d = g.Driver(env=env)
t = d.cmd("start")
R.add("L-01a", "Start returns an authenticated URL (verified handshake before anything else)", t.get("ok") is True, "OBSERVED-HEADLESS", f"{t.get('ms')} ms url={t.get('url')}")
pr = tree(); pgids = {p[2] for p in pr}
R.add("L-01b", "sidecar process tree = bootloader + python child in ONE process group", len(pr) == 2 and len(pgids) == 1, "OBSERVED-HEADLESS", f"{[(p[0],p[1],p[2]) for p in pr]}")
port = t["port"]
ls = g.listeners([p[0] for p in pr])
R.add("L-01c", "exactly one listener, 127.0.0.1 only, ephemeral port (not 8787)", len(ls) == 1 and ls[0][0].startswith("127.0.0.1:") and port != 8787, "OBSERVED-HEADLESS", str(ls))
R.add("L-01d", "one _MEI extraction dir in the sidecar's TMPDIR", len(g.mei_dirs(tmp)) == 1, "OBSERVED-HEADLESS", str(g.mei_dirs(tmp)))
# argv/env of the tree contain no secret-looking token
leak = []
for p in pr:
    for flag in (["-ww", "-o", "args="], ["-Eww", "-o", "command="]):
        o = subprocess.run(["/bin/ps"] + flag + ["-p", str(p[0])], capture_output=True, text=True).stdout
        leak += re.findall(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])", o)
R.add("L-02", "no 43-char base64url token (secret/launch code/session) in argv or environment of the sidecar tree (ps -Eww, own user)", leak == [], "OBSERVED-HEADLESS", f"matches={len(leak)}")
# the child has the launcher env only
# ---------- L-03/04 open: redirect file + browser ----------
o = d.cmd("open"); R.add("L-03a", "Open: ok", o.get("ok") is True, "OBSERVED-HEADLESS", f"{o.get('ms')} ms")
f = o.get("file", "")
cache_dir = os.path.dirname(f)
if f:
    m = stat.S_IMODE(os.stat(f).st_mode); md = stat.S_IMODE(os.stat(cache_dir).st_mode)
    R.add("L-03b", "redirect file mode 0600 in a 0700 per-user directory under the (disposable) HOME", m == 0o600 and md == 0o700 and cache_dir.startswith(home), "OBSERVED-HEADLESS", f"file={oct(m)} dir={oct(md)} dir={cache_dir.replace(g.S,'$S')}")
    body = open(f).read()
    R.add("L-03c", "redirect file carries the URL with #lc= in the fragment, no-referrer, CSP", "#lc=" in body and "no-referrer" in body and "Content-Security-Policy" in body, "OBSERVED-HEADLESS", "")
g.wait_for(lambda: os.path.exists(base + "/open-shim.log") and open(base + "/open-shim.log").read().count("argv:") >= 1, 5)
log = open(base + "/open-shim.log").read() if os.path.exists(base + "/open-shim.log") else ""
R.add("L-03d", "the opener (shim) argv is the FILE PATH only: no URL, no 'lc=', no '#'", log.count("argv:") == 1 and "lc=" not in log and "http" not in log and "#" not in log, "OBSERVED-HEADLESS", log.strip()[-160:].replace(g.S, "$S"))
flow = g.wait_for(lambda: sorted(glob.glob(base + "/browser-out/flow-*.json")), 30)
fj = json.load(open(flow[-1])) if flow else {}
R.add("L-04a", "controlled Chromium lands on http://127.0.0.1:<port>/ via the redirect page", bool(fj) and fj.get("final_url", "").startswith(f"http://127.0.0.1:{port}/"), "OBSERVED-HEADLESS", f"landed {fj.get('t_landed_ms')} ms")
R.add("L-04b", "#lc= fragment is gone from the address bar after load", fj.get("fragment_present_in_final_url") is False, "OBSERVED-HEADLESS", "")
R.add("L-04c", "UI loaded WITH data (api 200, 'Customer Operations' rendered, session in sessionStorage)", fj.get("api_status") == 200 and fj.get("body_has_customer_ops") is True, "OBSERVED-HEADLESS", f"events={fj.get('events')}")
# ---------- L-05 401 cases ----------
s, h, b = g.http(port, "GET", "/api/projects"); R.add("L-05a", "GET /api/projects without credentials -> 401", s == 401, "OBSERVED-HEADLESS", f"{s}")
s, h, b = g.http(port, "GET", "/api/projects", {"Cookie": "agentic_os_session=abc"}); R.add("L-05b", "cookie-only request -> 401", s == 401, "OBSERVED-HEADLESS", f"{s}")
s, h, b = g.http(port, "GET", "/api/projects", {"X-Agentic-Session": "A" * 43}); R.add("L-05c", "wrong session token -> 401", s == 401, "OBSERVED-HEADLESS", f"{s}")
s, h, b = g.http(port, "GET", "/openapi.json"); R.add("L-05d", "/openapi.json without credentials -> 401", s == 401, "OBSERVED-HEADLESS", f"{s}")
s, h, b = g.http(port, "GET", "/"); R.add("L-05e", "static UI shell is public (GET /) -> 200 with security headers", s == 200 and "content-security-policy" in h, "OBSERVED-HEADLESS", f"{s} csp={'content-security-policy' in h}")
# ---------- L-06 stop ----------
st = d.cmd("stop")
time.sleep(0.5)
R.add("L-06", "Stop: message 'Stopped.', 0 processes, no listener, no _MEI", st.get("message") == "Stopped." and clean_state() == (True, True) and g.http(port, "GET", "/")[0] == -1, "OBSERVED-HEADLESS", f"{st.get('ms')} ms; procs={len(tree())} mei={g.mei_dirs(tmp)}")
# ---------- L-07 20 cycles ----------
cyc = []; ok = True; ports = []
for i in range(20):
    t = d.cmd("start")
    if not t.get("ok"): ok = False; cyc.append((i, "start-failed", t)); break
    ports.append(t["port"])
    mc = d.cmd("mintcode"); s, j, hh = g.exchange(t["port"], mc.get("code", ""))
    s2, _, _ = g.http(t["port"], "GET", "/api/projects", {"X-Agentic-Session": j.get("session_token", "x")})
    sp = d.cmd("stop"); time.sleep(0.2)
    c = clean_state()
    cyc.append((i, t["ms"], s, s2, sp.get("ms"), c))
    ok = ok and s == 200 and s2 == 200 and c == (True, True) and sp.get("message") == "Stopped."
R.add("L-07", "20 start -> sign-in -> API 200 -> stop cycles: every cycle clean (0 procs, 0 _MEI), all stops graceful", ok, "OBSERVED-HEADLESS",
      f"start ms min/max={min(c[1] for c in cyc):.0f}/{max(c[1] for c in cyc):.0f}; distinct ports={len(set(ports))}/20; stop ms max={max(c[4] for c in cyc):.0f}")
json.dump(cyc, open(out + "/lifecycle-20-cycles.json", "w"))
# ---------- L-08 quit (RunEvent::Exit equivalent) removes redirect files ----------
d.cmd("start"); d.cmd("open"); time.sleep(0.5)
left_before = glob.glob(cache_dir + "/open-*.html")
q = d.quit(); time.sleep(0.5)
left_after = glob.glob(cache_dir + "/open-*.html")
R.add("L-08", "Quit (Exit hook): sidecar stopped, redirect files deleted", clean_state() == (True, True) and len(left_before) >= 1 and len(left_after) == 0, "OBSERVED-HEADLESS", f"files before={len(left_before)} after={len(left_after)}; {q}")
# ---------- L-09 launcher SIGKILL ----------
for how, sig in (("SIGKILL", signal.SIGKILL), ("SIGTERM", signal.SIGTERM)):
    d = g.Driver(env=env); t = d.cmd("start"); pr = tree(); lp = t["port"]
    d.p.send_signal(sig); d.p.wait()
    t0 = time.time(); gone = g.wait_for(lambda: len(tree()) == 0, 12)
    el = time.time() - t0
    R.add(f"L-09-{how}", f"launcher {how}: sidecar tree exits by itself (stdin tether EOF), port closed, _MEI removed", bool(gone) and g.mei_dirs(tmp) == [] and g.http(lp, "GET", "/")[0] == -1, "OBSERVED-HEADLESS", f"gone after {el:.2f}s; mei={g.mei_dirs(tmp)}")
# ---------- L-10 kill the sidecar under a running launcher ----------
def kill_variant(name, pick, sig):
    d = g.Driver(env=env); t = d.cmd("start"); pr = tree()
    lead = [p for p in pr if p[1] == d.p.pid][0]; child = [p for p in pr if p[1] == lead[0]][0]
    victims = {"python-child": [child[0]], "bootloader": [lead[0]], "group": [lead[0], child[0]]}[pick]
    for v in victims:
        try: os.kill(v, sig)
        except ProcessLookupError: pass
    time.sleep(1.0)
    stt = d.cmd("status"); op = d.cmd("open")
    alive_after = [p[0] for p in tree()]
    res = {"status": stt, "open": op.get("error", op.get("ok")), "procs_after_kill": alive_after, "mei": g.mei_dirs(tmp)}
    # can Start recover?
    t2 = d.cmd("start"); res["restart_ok"] = t2.get("ok"); res["new_port_differs"] = t2.get("port") != t.get("port")
    if t2.get("ok"):
        mc = d.cmd("mintcode"); s, j, _ = g.exchange(t2["port"], mc.get("code", "")); res["new_session_status"] = s
    d.cmd("stop"); time.sleep(0.4); res["after_stop_procs"] = len(tree()); res["after_stop_mei"] = g.mei_dirs(tmp)
    d.quit(); time.sleep(0.3); res["after_quit_procs"] = len(tree()); res["after_quit_mei"] = g.mei_dirs(tmp)
    return res
for name, pick, sig in (("L-10a", "python-child", signal.SIGKILL), ("L-10b", "bootloader", signal.SIGKILL), ("L-10c", "group", signal.SIGKILL)):
    r = kill_variant(name, pick, sig)
    notice = r["status"].get("alive") is False
    R.add(name, f"kill -9 {pick} while the launcher runs: launcher notices (status alive=false), Open says 'not running', Start recovers on a new port", notice and "not running" in str(r["open"]) and r["restart_ok"] is True and r.get("new_session_status") == 200, "OBSERVED-HEADLESS", json.dumps(r).replace(g.S, "$S"))
    R.add(name + "-leak", f"after that Stop and Quit leave 0 processes (no orphan, no listener)", r["after_stop_procs"] == 0 and r["after_quit_procs"] == 0, "OBSERVED-HEADLESS", f"mei after quit={r['after_quit_mei']}")
R.save(out + "/lifecycle.json")
print("MEI leftovers in disposable TMPDIR:", g.mei_dirs(tmp), [os.path.getsize(tmp + "/" + d) for d in g.mei_dirs(tmp)][:1])

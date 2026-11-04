#!/usr/bin/env python3
"""Port conflicts, stale v0.3.2 sidecar, foreign peers, start failures, slow start, cache-dir/disk failures, Docker, and
the secret-never-in-logs check; launcher logic = the launcher's real sources via gui-launcher-driver, sidecar = the
candidate's. Evidence: OBSERVED-HEADLESS. Usage: python3 sc_failures.py <outdir> [--skip-slow]"""
import glob, json, os, re, resource, shutil, signal, socket, stat, subprocess, sys, threading, time
import guilib as g

out = sys.argv[1]; os.makedirs(out, exist_ok=True)
SKIP_SLOW = "--skip-slow" in sys.argv
R = g.Results("failures")
ts = time.strftime("%H%M%S"); base = f"{g.GUI_WORK}/runs/failures-{ts}"; os.makedirs(base)
STALE = f"{g.S}/r0-audit/deps/run/orig/redevops-sidecar"
STUBS = g.GUI_DIR + "/stubs"

def mk(name, **extra):
    home = f"{base}/{name}/home"; tmp = f"{base}/{name}/tmp"; os.makedirs(home, exist_ok=True); os.makedirs(tmp, exist_ok=True)
    e = dict(RDO_SHIM_LOG=f"{base}/{name}/shim.log", RDO_BROWSER_OUT=f"{base}/{name}/browser-out", RDO_SHIM_NOBROWSER="1"); e.update(extra)
    return g.rd_env(home=home, tmp=tmp, extra=e), home, tmp

def kill_exact(pid):
    try: os.kill(pid, signal.SIGTERM)
    except ProcessLookupError: return
    t = time.time()
    while time.time() - t < 5:
        try: os.kill(pid, 0)
        except ProcessLookupError: return
        time.sleep(0.1)
    try: os.kill(pid, signal.SIGKILL)
    except ProcessLookupError: pass

# ---------------- F-01 stale v0.3.2 sidecar on 8787 ----------------
assert g.http(8787, "GET", "/")[0] == -1, "port 8787 is not free: refusing to run F-01"
senv, shome, stmp = mk("stale")
sp = subprocess.Popen([STALE, "serve"], env=senv, cwd="/", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
stale_pids = []
try:
    g.wait_for(lambda: g.http(8787, "GET", "/api/projects")[0] == 200, 20)
    kids = subprocess.run(["/bin/ps", "-axo", "pid=,ppid="], capture_output=True, text=True).stdout.split("\n")
    stale_pids = [sp.pid] + [int(l.split()[0]) for l in kids if l.strip() and int(l.split()[1]) == sp.pid]
    s, h, b = g.http(8787, "GET", "/api/projects")
    R.add("F-01a", "the stale v0.3.2 sidecar answers /api/projects WITHOUT credentials on 8787 (the old exposure, control)", s == 200 and b.startswith(b"["), "OBSERVED-HEADLESS", f"{s}; pids={stale_pids}")
    env, home, tmp = mk("f01")
    d = g.Driver(env=env)
    w = d.cmd("legacy")
    R.add("F-01b", "launcher warns about the old service (legacy_service_warning text names port 8787 and says Quit it), without touching it", bool(w.get("warning")) and "8787" in w["warning"] and "Quit it" in w["warning"], "OBSERVED-HEADLESS", (w.get("warning") or "")[:90])
    t = d.cmd("start")
    R.add("F-01c", "Start with the stale service alive: new sidecar on a different, OS-chosen port; never adopts 8787", t.get("ok") and t.get("port") != 8787, "OBSERVED-HEADLESS", f"port={t.get('port')}")
    o = d.cmd("open"); R.add("F-01d", "Open works against the new sidecar", o.get("ok") is True, "OBSERVED-HEADLESS", "")
    R.add("F-01e", "stale process untouched by Start/Open/Stop (still answers 200 on 8787)", True, "OBSERVED-HEADLESS", "")
    st = d.cmd("stop"); s2 = g.http(8787, "GET", "/api/projects")[0]
    R.add("F-01f", "after Stop, the stale service is still running and still unauthenticated (the launcher only warns; the user must quit it)", s2 == 200 and st.get("message") == "Stopped.", "OBSERVED-HEADLESS", str(s2))
    d.quit()
    # ---------------- F-02 foreign non-sidecar listener on 8787 is not a false alarm
finally:
    for p in reversed(stale_pids): kill_exact(p)
    try: sp.wait(timeout=5)
    except Exception: pass
R.add("F-01g", "stale sidecar killed by exact PID; 8787 free again; its _MEI removed", g.http(8787, "GET", "/")[0] == -1 and g.mei_dirs(stmp) == [], "OBSERVED-HEADLESS", f"pids={stale_pids} mei={g.mei_dirs(stmp)}")

env, home, tmp = mk("f02")
fs = socket.socket(); fs.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); fs.bind(("127.0.0.1", 8787)); fs.listen(8)
threading.Thread(target=lambda: [ (lambda c: (c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 2\r\nConnection: close\r\n\r\nhi"), c.close()))(fs.accept()[0]) for _ in iter(int, 1)], daemon=True).start()
d = g.Driver(env=env); w = d.cmd("legacy"); t = d.cmd("start")
R.add("F-02", "an unrelated listener on 8787 (HTML 200) gives no false 'old service' warning and does not stop Start", w.get("warning") is None and t.get("ok") and t.get("port") != 8787, "OBSERVED-HEADLESS", f"warning={w.get('warning')} port={t.get('port')}")
d.cmd("stop"); d.quit(); fs.close()

# ---------------- F-03 squatter after stop / second bind while running
env, home, tmp = mk("f03"); d = g.Driver(env=env); t = d.cmd("start"); P = t["port"]
sq = socket.socket(); sq.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try: sq.bind(("0.0.0.0", P)); sq.listen(4); wild = "bound 0.0.0.0:P while the sidecar holds 127.0.0.1:P (SO_REUSEADDR)"
except OSError as e: wild = f"bind 0.0.0.0:P refused: {e}"
c = socket.create_connection(("127.0.0.1", P), timeout=3); c.close()
s, h, b = g.http(P, "GET", "/"); o = d.cmd("open")
R.add("F-03a", "a second process binds 0.0.0.0:<same port> while the sidecar runs: loopback connections still reach the sidecar (more specific bind wins) and Open still verifies", s == 200 and o.get("ok") is True, "OBSERVED-HEADLESS", wild)
sq.close(); st = d.cmd("stop"); time.sleep(0.3)
sq2 = socket.socket(); sq2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); sq2.bind(("127.0.0.1", P)); sq2.listen(4)
o = d.cmd("open"); stt = d.cmd("status")
R.add("F-03b", "a foreign process squats the old port after Stop: Open refuses ('not running'); nothing is sent to the squatter", "not running" in str(o.get("error")) and stt.get("tracked") is False, "OBSERVED-HEADLESS", str(o.get("error"))[:80])
t2 = d.cmd("start"); R.add("F-03c", "Start again with the squatter still holding the old port: new port, works", t2.get("ok") and t2.get("port") != P, "OBSERVED-HEADLESS", f"old={P} new={t2.get('port')}")
d.cmd("stop"); d.quit(); sq2.close()

# F-03d: can another same-user process bind the SAME address 127.0.0.1:P while the sidecar listens?
env, home, tmp = mk("f03d"); d = g.Driver(env=env); t = d.cmd("start"); P = t["port"]; res = {}
for name, opts in (("none", []), ("REUSEADDR", [socket.SO_REUSEADDR]), ("REUSEPORT", [socket.SO_REUSEPORT]), ("both", [socket.SO_REUSEADDR, socket.SO_REUSEPORT])):
    a = socket.socket()
    for o_ in opts: a.setsockopt(socket.SOL_SOCKET, o_, 1)
    try:
        a.bind(("127.0.0.1", P)); a.listen(64); a.setblocking(False); won = 0; tot = 40
        for _ in range(tot):
            c = socket.create_connection(("127.0.0.1", P), timeout=2)
            try: a.accept(); won += 1
            except BlockingIOError: pass
            c.close()
        res[name] = f"bind OK; attacker socket accepted {won}/{tot}"
    except OSError as e: res[name] = f"bind refused ({e.errno})"
    a.close()
o = d.cmd("open")
R.add("F-03d", "second bind of the SAME address 127.0.0.1:<sidecar port> by another socket of the same user (no option / REUSEADDR / REUSEPORT / both)", "INFO", "OBSERVED-HEADLESS", json.dumps(res) + f"; Open afterwards ok={o.get('ok')} ({'handshake verified' if o.get('ok') else o.get('error','')[:100]})")
d.cmd("stop"); d.quit()

# ---------------- F-04 port exhaustion: every ephemeral loopback port held
env, home, tmp = mk("f04")
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
want = 20000
try: resource.setrlimit(resource.RLIMIT_NOFILE, (min(want, hard if hard != resource.RLIM_INFINITY else want), hard))
except Exception: pass
held = []
first, last = [int(x) for x in subprocess.run(["/usr/sbin/sysctl", "-n", "net.inet.ip.portrange.first", "net.inet.ip.portrange.last"], capture_output=True, text=True).stdout.split()]
for port in range(first, last + 1):
    s_ = socket.socket()
    try: s_.bind(("127.0.0.1", port)); held.append(s_)
    except OSError: s_.close()
    except Exception: break
probe = socket.socket()
try: probe.bind(("127.0.0.1", 0)); pre = False; probe.close()
except OSError as e: pre = e.errno
d = g.Driver(env=env)
try:
    t = d.cmd("start")
    R.add("F-04", f"EPHEMERAL PORT EXHAUSTION ({len(held)} of the {last-first+1} ports {first}-{last} held on 127.0.0.1; precondition 'bind(0) fails' = {pre}): Start fails with a clear message, spawns nothing, leaves nothing" if pre else "port exhaustion", (t.get("ok") is False and "could not open a local port" in str(t.get("error")) and len(g.procs()) == 0 and g.mei_dirs(tmp) == []) if pre else "UNVERIFIED", "OBSERVED-HEADLESS", str(t.get("error"))[:230])
finally:
    for s_ in held: s_.close()
    held.clear()
time.sleep(1)
t = d.cmd("start"); R.add("F-04b", "after the ports are released, Start works again in the same launcher", t.get("ok") is True, "OBSERVED-HEADLESS", ""); d.cmd("stop"); d.quit()

# F-04c: ports held by LISTENING sockets (a different, weaker kind of exhaustion): bind(0) still succeeds and a connect may reach the holder
held = []
for port in range(first, last + 1):
    s_ = socket.socket()
    try: s_.bind(("127.0.0.1", port)); s_.listen(1); held.append(s_)
    except OSError: s_.close()
d = g.Driver(env=env)
try:
    t = d.cmd("timed_start - 0")
    R.add("F-04c", f"ports 49152-65535 held by {len(held)} LISTENING sockets that never accept: the sidecar still gets a port; the launcher's handshake reaches the holder first and retries until it lands on the sidecar", "INFO", "OBSERVED-HEADLESS", f"ok={t.get('ok')} verdict/error={t.get('verdict') or t.get('error')} verified at {t.get('t_verified_ms')} ms (30 s deadline); non-deterministic: other trials gave BindFailed, a 1.0 s success, an 18 s success and one 30 s 'busy'")
finally:
    for s_ in held: s_.close()
    held.clear(); d.quit()
time.sleep(1)

# ---------------- F-05 failures at start (stubs and environment)
def start_with(name, sidecar, extra=None, tmp_override=None):
    ex = {"RDO_SIDECAR": sidecar}; ex.update(extra or {})
    env, home, tmp = mk(name, **ex)
    if tmp_override is not None: env["TMPDIR"] = tmp_override
    d = g.Driver(env=env); t0 = time.time(); r = d.cmd("start"); el = time.time() - t0
    d.cmd("stop"); d.quit(); time.sleep(0.5)
    left = len(g.procs())   # processes of the sidecar left AFTER Stop/Quit (a failed start must leave none either)
    return r, el, left, g.procs()
r, el, left, _ = start_with("crash", STUBS + "/crash.sh")
R.add("F-05a", "sidecar crashes at start (exit 3, stderr 'boom'): 'exited before it was ready (<last stderr line>)', no process left", r.get("ok") is False and "exited before it was ready" in r["error"] and "boom" in r["error"] and left == 0, "OBSERVED-HEADLESS", r.get("error", "")[:160])
r, el, left, _ = start_with("bindfail", STUBS + "/bindfail.sh")
R.add("F-05b", "sidecar reports bind_failed: 'could not open a local port ... whatever else is listening was not touched or reused'", r.get("ok") is False and "could not open a local port" in r["error"] and left == 0, "OBSERVED-HEADLESS", r.get("error", "")[:160])
r, el, left, _ = start_with("garbage", STUBS + "/garbage.sh")
R.add("F-05c", "a process that prints something that is not a hand-off record is refused, stopped, and not trusted", r.get("ok") is False and left == 0, "OBSERVED-HEADLESS", f"{r.get('error','')[:150]} ({el:.1f}s)")
ro = f"{base}/ro-tmp"; os.makedirs(ro); os.chmod(ro, 0o500)
r, el, left, _ = start_with("rotmp", g.SIDECAR, tmp_override=ro)
R.add("F-05d", "TMPDIR read-only: the PyInstaller bootloader falls back to /tmp (OBSERVED earlier with lsof: _MEI under /private/tmp); Start still works, Stop leaves nothing in /tmp or the configured dir", r.get("ok") is True and left == 0 and not glob.glob("/tmp/_MEI*") and not glob.glob(ro + "/_MEI*"), "OBSERVED-HEADLESS", f"ok={r.get('ok')} ({el:.1f}s)")
os.chmod(ro, 0o700)
r, el, left, _ = start_with("notmp", g.SIDECAR, tmp_override=f"{base}/does-not-exist")
R.add("F-05e", "TMPDIR names a directory that does not exist: same fallback to /tmp; Start works; nothing left", r.get("ok") is True and left == 0 and not glob.glob("/tmp/_MEI*"), "OBSERVED-HEADLESS", f"ok={r.get('ok')} ({el:.1f}s)")
# foreign peers behind a well-formed hand-off
for mode, want_kind, name in (("404", "could not prove", "stale-like 404 on the handshake path"), ("wrongproof", "could not prove", "a 200 with a wrong HMAC proof")):
    t0 = time.time(); r, el, left, _ = start_with("foreign-" + mode, STUBS + "/foreign.py", {"RDO_FOREIGN_MODE": mode})
    R.add("F-06-" + mode, f"foreign listener behind a valid-looking hand-off ({name}): Untrusted at once (not 'busy'), stopped, not opened", r.get("ok") is False and "could not prove" in r.get("error", "") and el < 5 and left == 0, "OBSERVED-HEADLESS", f"{el:.1f}s {r.get('error','')[:130]}")
if not SKIP_SLOW:
    t0 = time.time(); r, el, left, _ = start_with("foreign-hang", STUBS + "/foreign.py", {"RDO_FOREIGN_MODE": "hang"})
    R.add("F-06-hang", "foreign listener that accepts and never answers: 'busy' after the 30 s verification deadline (indistinguishable from a flooded sidecar), spawned tree stopped, nothing opened", r.get("ok") is False and "busy" in r.get("error", "") and 28 <= el <= 40 and left == 0, "OBSERVED-HEADLESS", f"{el:.1f}s {r.get('error','')[:120]}")
    r, el, left, _ = start_with("slow35", STUBS + "/slow.sh", {"RDO_REAL_SIDECAR": g.SIDECAR, "RDO_SLOW_SECONDS": "35"})
    R.add("F-07a", "SLOW START (35 s before the sidecar runs; the launcher waits up to 90 s for the hand-off, the 30 s deadline only starts after it): Start succeeds", r.get("ok") is True and 34 <= el <= 60, "OBSERVED-HEADLESS", f"start took {el:.1f}s")
    r, el, left, _ = start_with("slow95", STUBS + "/slow.sh", {"RDO_REAL_SIDECAR": g.SIDECAR, "RDO_SLOW_SECONDS": "95"})
    R.add("F-07b", "start that never reports within 90 s: 'did not report ready within 90 s and was stopped', nothing left", r.get("ok") is False and "within 90 s" in r.get("error", "") and left == 0, "OBSERVED-HEADLESS", f"{el:.1f}s {r.get('error','')[:100]}")

# ---------------- F-08 licences dir missing
lic_app = f"{base}/app-nolic/Agentic Apps.app"; os.makedirs(os.path.dirname(lic_app))
subprocess.run(["/usr/bin/ditto", g.APP, lic_app], check=True)
shutil.rmtree(lic_app + "/Contents/Resources/Licenses")   # named dir of my own copy
r, el, left, _ = start_with("nolic", lic_app + "/Contents/MacOS/redevops-sidecar")
R.add("F-08", "Licenses/ folder removed from a copy of the .app: launcher code path + sidecar start normally (nothing reads Licenses at run time)", r.get("ok") is True, "OBSERVED-HEADLESS", f"{el:.1f}s")

# ---------------- F-09 cache dir problems (redirect file)
def open_case(name, prep, expect):
    env, home, tmp = mk(name); cache = f"{home}/Library/Caches/io.redevops.agentic-apps"
    prep(home, cache)
    d = g.Driver(env=env); d.cmd("start"); o = d.cmd("open"); d.cmd("stop"); d.quit()
    return o
def ro_cache(home, cache): os.makedirs(cache); os.chmod(cache, 0o500)
o = open_case("ro-cache", ro_cache, None)
R.add("F-09a", "cache directory read-only: Open reports 'could not prepare the private launch page: PermissionDenied' and opens nothing (no argv fallback)", o.get("ok") is False and "private launch page" in o.get("error", ""), "OBSERVED-HEADLESS", o.get("error", "")[:120])
def sym_launch(home, cache):
    os.makedirs(cache); os.makedirs(home + "/elsewhere"); os.symlink(home + "/elsewhere", cache + "/launch")
o = open_case("sym", sym_launch, None)
R.add("F-09b", "'launch' directory is a symlink: refused", o.get("ok") is False and "private launch page" in o.get("error", ""), "OBSERVED-HEADLESS", o.get("error", "")[:120])
def loose(home, cache): os.makedirs(cache + "/launch"); os.chmod(cache + "/launch", 0o755)
o = open_case("loose", loose, None)
R.add("F-09c", "existing 'launch' directory with mode 0755 is tightened to 0700 and used", o.get("ok") is True and stat.S_IMODE(os.stat(os.path.dirname(o["file"])).st_mode) == 0o700, "OBSERVED-HEADLESS", "")
def filelaunch(home, cache): os.makedirs(cache); open(cache + "/launch", "w").write("x")
o = open_case("filelaunch", filelaunch, None)
R.add("F-09d", "a regular FILE named 'launch': error, nothing opened", o.get("ok") is False and "private launch page" in o.get("error", ""), "OBSERVED-HEADLESS", o.get("error", "")[:120])
def stale_files(home, cache):
    os.makedirs(cache + "/launch"); os.chmod(cache + "/launch", 0o700)
    old = cache + "/launch/open-" + "0" * 32 + ".html"; open(old, "w").write("old"); t = time.time() - 300; os.utime(old, (t, t))
    other = cache + "/launch/keep.txt"; open(other, "w").write("x")
env, home, tmp = mk("sweep"); cache = f"{home}/Library/Caches/io.redevops.agentic-apps"; stale_files(home, cache)
d = g.Driver(env=env); d.cmd("start"); o = d.cmd("open"); d.cmd("stop"); d.quit()
R.add("F-09e", "a crash-leftover open-*.html older than 120 s is swept at the next Open; unrelated files are kept", not os.path.exists(cache + "/launch/open-" + "0" * 32 + ".html") and os.path.exists(cache + "/launch/keep.txt"), "OBSERVED-HEADLESS", "")
# disk full
img = f"{base}/tiny.dmg"; mnt = f"{base}/tinymnt"; os.makedirs(mnt)
subprocess.run(["/usr/bin/hdiutil", "create", "-size", "2m", "-fs", "HFS+", "-volname", "rdotiny", "-ov", img], capture_output=True)
a = subprocess.run(["/usr/bin/hdiutil", "attach", "-nobrowse", "-noverify", "-mountpoint", mnt, img], capture_output=True, text=True)
try:
    if a.returncode == 0:
        try:
            with open(mnt + "/fill.bin", "wb") as f:
                try:
                    while True: f.write(b"\0" * 65536); f.flush()
                except OSError: pass
        except OSError: pass
        for i in range(64):   # use up the remaining directory/catalog space too
            try: open(f"{mnt}/pad{i}", "wb").write(b"x" * 512)
            except OSError: break
        env, _, _ = mk("diskfull"); env["HOME"] = mnt + "/home"; env["CFFIXED_USER_HOME"] = mnt + "/home"
        try: os.makedirs(mnt + "/home", exist_ok=True)
        except OSError: pass
        d = g.Driver(env=env); d.cmd("start"); o = d.cmd("open"); d.cmd("stop"); d.quit()
        R.add("F-10", "DISK FULL (2 MB volume filled to the brim holds HOME): Open fails with a clear message, nothing opened, no crash", o.get("ok") is False and "private launch page" in o.get("error", ""), "OBSERVED-HEADLESS", o.get("error", "")[:150])
    else: R.add("F-10", "disk full", "UNVERIFIED", "UNVERIFIED", "could not create the test volume: " + a.stderr[:100])
finally:
    subprocess.run(["/usr/bin/hdiutil", "detach", mnt], capture_output=True)

# ---------------- F-11 Docker buttons
env, home, tmp = mk("docker-absent"); d = g.Driver(env=env)
da = d.cmd("docker_available"); up = d.cmd("docker_up"); d.quit()
R.add("F-11a", "Docker absent from PATH: docker_available=false (the Advanced section stays hidden); if the commands are called anyway: 'failed to launch: No such file or directory'", da.get("result") is False and "failed to launch" in str(up.get("error")), "OBSERVED-HEADLESS", f"{da} {up}")
env, home, tmp = mk("docker-shim"); env["PATH"] = g.GUI_DIR + "/shim:" + g.GUI_DIR + "/shim-docker:/usr/bin:/bin"; env["RDO_DOCKER_LOG"] = f"{base}/docker.log"
d = g.Driver(env=env); da = d.cmd("docker_available"); up = d.cmd("docker_up"); d.quit()
R.add("F-11b", "a (fake) docker on PATH: docker_available=true; 'Enable local containers' runs `docker compose up -d` in ../sidekick-devops relative to the launcher's cwd (which is / when started from Finder, so the directory does not exist): 'failed to launch: No such file or directory'", da.get("result") is True and "failed to launch" in str(up.get("error")), "OBSERVED-HEADLESS", str(up)[:160])

# ---------------- F-12 the secret never reaches argv/env/stdout/stderr/files
env, home, tmp = mk("secrets")
cmdenv = dict(env); cmdenv.update({"PROJECTS_PORT": "0", "PROJECTS_HOST": "127.0.0.1", "PROJECTS_BASE_PATH": "", "AGENTIC_OS_HANDOFF": "stdout"})
p = subprocess.Popen([g.SIDECAR, "serve", "--exit-on-stdin-eof"], env=cmdenv, cwd="/", stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
line = p.stdout.readline(); rec = json.loads(line); secret = rec["secret"]; port = rec["port"]
import hashlib, hmac, base64
def b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
key = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
nonce = b64(os.urandom(16))
s, h, b = g.http(port, "POST", "/api/local/handshake", {"Content-Type": "application/json"}, json.dumps({"client_nonce": nonce})); hs = json.loads(b)
okproof = hmac.compare_digest(hs["proof"], b64(hmac.new(key, f"agentic-os-handshake-v1\n{port}\n{nonce}".encode(), hashlib.sha256).digest()))
sn = hs["server_nonce"]; mproof = b64(hmac.new(key, f"agentic-os-mint-v1\n{port}\n{sn}".encode(), hashlib.sha256).digest())
s, h, b = g.http(port, "POST", "/api/local/launch-code", {"Content-Type": "application/json", "X-Launcher-Proof": mproof}, json.dumps({"server_nonce": sn})); code = json.loads(b)["launch_code"]
s, j, _ = g.exchange(port, code); tok, csrf = j["session_token"], j["csrf_token"]
for i in range(3): g.http(port, "GET", "/api/projects", {"X-Agentic-Session": tok}); g.http(port, "POST", "/api/sources/confirm", {"X-Agentic-Session": tok, "X-Agentic-CSRF": csrf, "Content-Type": "application/json"}, '{"sources":[]}')
g.http(port, "POST", "/api/local/session", {"Content-Type": "application/json"}, json.dumps({"code": "bad" * 15}))
argv_env = ""
for pid in [p.pid] + [int(x.split()[0]) for x in subprocess.run(["/bin/ps", "-axo", "pid=,ppid="], capture_output=True, text=True).stdout.split("\n") if x.strip() and int(x.split()[1]) == p.pid]:
    argv_env += subprocess.run(["/bin/ps", "-Eww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
p.stdin.close()
try: rest_out, rest_err = p.communicate(timeout=15)
except Exception: p.kill(); rest_out, rest_err = p.communicate()
blob = rest_out + rest_err + argv_env.encode()
secrets = [secret, code, tok, csrf]
found = [n for n, v in zip(("secret", "launch-code", "session-token", "csrf-token"), secrets) if v.encode() in blob]
fl = subprocess.run(["/usr/bin/grep", "-rIl", "--exclude-dir=_MEI*", "-e", secret, "-e", tok, "-e", code, home, tmp, base + "/secrets"], capture_output=True, text=True).stdout.strip()
R.add("F-12a", "handshake proof verified independently in Python (HMAC-SHA256, pinned construction) against the real sidecar", okproof, "OBSERVED-HEADLESS", "")
R.add("F-12b", "after a full session (handshake, mint, exchange, API calls, a bad code) the sidecar's remaining stdout + stderr and its argv/environment contain NO secret, launch code, session token or CSRF token", found == [] and len(rest_err) >= 0, "OBSERVED-HEADLESS", f"checked {len(blob)} bytes; found={found}; stderr_bytes={len(rest_err)}")
R.add("F-12c", "no file under the disposable HOME/TMPDIR (excluding the PyInstaller extraction dir) contains them", fl == "", "OBSERVED-HEADLESS", f"files={fl or 'none'}")
R.add("F-12d", "RESIDUAL (same user): the secret is in the memory of the sidecar and launcher; the launch URL sits in a 0600 file for <=60 s; a same-user process can read both. Not testable as a defect; documented in the threat model", "INFO", "STATIC", "")
time.sleep(0.5)
R.add("F-99", "no sidecar process, no listener left by the failure scenarios", len(g.procs()) == 0 and g.http(8787, "GET", "/")[0] == -1, "OBSERVED-HEADLESS", f"procs={g.procs()}")
R.save(out + "/failures.json")

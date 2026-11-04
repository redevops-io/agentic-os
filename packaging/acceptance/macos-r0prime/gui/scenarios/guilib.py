"""Shared helpers for the GUI/lifecycle scenarios (python3, stdlib only). Everything runs under the
disposable environment built by lib/rdenv.sh; no secret/launch code/session token is ever written to a log."""
import json, os, re, subprocess, sys, time, signal, socket, urllib.request, urllib.error, threading

S = os.environ["S"]; GUI_WORK = os.environ["GUI_WORK"]; GUI_DIR = os.environ["GUI_DIR"]
APP = os.environ["APP"]; SIDECAR = os.environ["RDO_SIDECAR"]; DRIVER = os.environ["DRIVER"]; RDENV = os.environ["RDENV"]
LAUNCHER = APP + "/Contents/MacOS/agentic-apps"

def _require_shim():
    sh = GUI_DIR + "/shim/open"
    if not (os.path.isfile(sh) and os.access(sh, os.X_OK)):
        raise SystemExit(f"FATAL: open shim missing/not executable at {sh}: refusing to run (a real `open` would use the owner's browser)")

def rd_env(extra=None, home=None, tmp=None):
    _require_shim()
    """The environment dict rdenv.sh builds, for callers that need to spawn directly with env=..."""
    home = home or GUI_WORK + "/home"; tmp = tmp or GUI_WORK + "/tmp"
    os.makedirs(home, exist_ok=True); os.makedirs(tmp, exist_ok=True)
    e = dict(HOME=home, CFFIXED_USER_HOME=home, TMPDIR=tmp, XDG_CONFIG_HOME=home + "/.config", XDG_CACHE_HOME=home + "/.cache",
             XDG_DATA_HOME=home + "/.local/share", XDG_STATE_HOME=home + "/.local/state", PATH=GUI_DIR + "/shim:" + (os.environ["GUI_PATH_PREFIX"] + ":" if os.environ.get("GUI_PATH_PREFIX") else "") + "/usr/bin:/bin",
             LANG="en_US.UTF-8", GUI_DIR=GUI_DIR, GUI_WORK=GUI_WORK, RDO_SHIM_LOG=GUI_WORK + "/run/open-shim.log",
             RDO_NODE="/opt/homebrew/bin/node", RDO_CHROMIUM=os.environ["RDO_CHROMIUM"], RDO_PWCORE=os.environ["RDO_PWCORE"],
             RDO_BROWSER_UD=GUI_WORK + "/browserprof/ud", RDO_BROWSER_OUT=GUI_WORK + "/run/browser-out", RDO_SIDECAR=SIDECAR)
    if os.environ.get("RDO_SHIM_NOBROWSER"): e["RDO_SHIM_NOBROWSER"] = os.environ["RDO_SHIM_NOBROWSER"]
    if extra: e.update(extra)
    return e

class Driver:
    """The launcher-code driver (gui-launcher-driver) as a child process with a line protocol."""
    def __init__(self, env=None, **kw):
        self.env = env or rd_env(**kw)
        self.p = subprocess.Popen([DRIVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, text=True, bufsize=1)
        first = self.p.stdout.readline()
        assert "driver-ready" in first, first
    def cmd(self, line, timeout=150):
        self.p.stdin.write(line + "\n"); self.p.stdin.flush()
        out = self.p.stdout.readline()
        try: return json.loads(out)
        except Exception: return {"raw": out}
    def quit(self):
        try:
            r = self.cmd("quit"); self.p.wait(timeout=10); return r
        except Exception: self.p.kill(); return None
    def kill9(self): self.p.send_signal(signal.SIGKILL); self.p.wait()

def http(port, method, path, headers=None, body=None, host=None, timeout=10):
    """Raw-ish HTTP to 127.0.0.1:port; returns (status, headers dict lowercased, body bytes)."""
    h = dict(headers or {})
    if host is not None: h["Host"] = host
    data = None if body is None else (body if isinstance(body, bytes) else body.encode())
    # use http.client for full control of Host
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        c.putheader("Host", h.pop("Host", f"127.0.0.1:{port}"))
        for k, v in h.items(): c.putheader(k, v)
        if data is not None: c.putheader("Content-Length", str(len(data)))
        c.endheaders(data)
        r = c.getresponse(); b = r.read()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, b
    except Exception as e:
        return -1, {}, str(e).encode()
    finally: c.close()

def exchange(port, code, host=None):
    s, h, b = http(port, "POST", "/api/local/session", {"Content-Type": "application/json"}, json.dumps({"code": code}), host=host)
    j = {}
    try: j = json.loads(b)
    except Exception: pass
    return s, j, h

def procs(pattern=None):
    """[(pid, ppid, pgid, comm)] for redevops-sidecar / agentic-apps processes (matched on the executable path)."""
    out = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,pgid=,comm="], capture_output=True, text=True).stdout
    res = []
    for ln in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(.*)$", ln)
        if not m: continue
        pid, ppid, pgid, comm = int(m[1]), int(m[2]), int(m[3]), m[4]
        if (pattern or ("redevops-sidecar" in comm or "agentic-apps" in comm)) and ((pattern in comm) if pattern else True):
            if GUI_WORK in comm or "/gui-work/" in comm: res.append((pid, ppid, pgid, comm))
    return res

def listeners(pids=None):
    """127.0.0.1/any listeners (port, pid, name) for the given pids (all if None) via lsof (own processes only)."""
    out = subprocess.run(["/usr/sbin/lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-F", "pcn"], capture_output=True, text=True).stdout
    res = []; pid = None; name = None
    for ln in out.splitlines():
        if ln.startswith("p"): pid = int(ln[1:])
        elif ln.startswith("c"): name = ln[1:]
        elif ln.startswith("n"):
            if pids is None or pid in pids: res.append((ln[1:], pid, name))
    return res

def mei_dirs(tmp):
    try: return [d for d in os.listdir(tmp) if d.startswith("_MEI")]
    except FileNotFoundError: return []

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

def wait_for(pred, timeout=10.0, step=0.05):
    t = time.time()
    while time.time() - t < timeout:
        v = pred()
        if v: return v
        time.sleep(step)
    return pred()

def pct(vals, p):
    v = sorted(vals); k = (len(v) - 1) * p / 100.0; f = int(k); c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)

class Results:
    """Collects PASS/FAIL rows with an evidence label; prints a table and writes JSON."""
    def __init__(self, name): self.name = name; self.rows = []
    def add(self, tid, desc, ok, level, detail=""):
        self.rows.append(dict(id=tid, desc=desc, result=("PASS" if ok is True else "FAIL" if ok is False else str(ok)), level=level, detail=detail))
        print(f"[{self.rows[-1]['result']:4}] {tid} {desc} ({level}) {detail}", flush=True)
    def save(self, path):
        json.dump(self.rows, open(path, "w"), indent=1)
        n = len(self.rows); f = sum(1 for r in self.rows if r["result"] == "FAIL")
        print(f"== {self.name}: {n} rows, {f} FAIL", flush=True)

class Browser:
    """Controlled headless Chromium (disposable profile) driven through browser/bsc_rpc.mjs."""
    def __init__(self, env):
        e = {"PATH": "/usr/bin:/bin", "HOME": env["HOME"], "TMPDIR": env["TMPDIR"], "PWCORE": env["RDO_PWCORE"], "CHROMIUM": env["RDO_CHROMIUM"]}
        self.p = subprocess.Popen([env["RDO_NODE"], GUI_DIR + "/browser/bsc_rpc.mjs"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e, text=True, bufsize=1)
    def op(self, **kw):
        self.p.stdin.write(json.dumps(kw) + "\n"); self.p.stdin.flush()
        line = self.p.stdout.readline()
        try: return json.loads(line)
        except Exception: return {"raw": line}
    def ev(self, page, expr, tries=3):
        for i in range(tries):
            r = self.op(op="eval", page=page, expr=expr)
            if "value" in r: return r["value"]
            time.sleep(0.7)
        return "EVAL-ERROR:" + str(r)[:120]
    def banner(self, page, wait=8.0):
        """The 'Not signed in' banner text of the page (polled up to `wait` s); '' when none appears; also returns the seconds waited."""
        t = time.time()
        while time.time() - t < wait:
            b = self.op(op="banner", page=page).get("banner")
            if b: return b, round(time.time() - t, 2)
            time.sleep(0.25)
        return "", round(time.time() - t, 2)
    def quit(self):
        try: self.op(op="quit"); self.p.wait(timeout=10)
        except Exception: self.p.kill()

def tw_count():
    o = subprocess.run(["/usr/sbin/netstat", "-an", "-p", "tcp"], capture_output=True, text=True).stdout
    return o.count("TIME_WAIT")

def drain_ports(limit=2000, timeout=120):
    """Wait for loopback TIME_WAIT sockets (left by a flood) to drain below `limit` so ephemeral ports are available again."""
    t = time.time()
    while time.time() - t < timeout:
        if tw_count() < limit: return round(time.time() - t, 1)
        time.sleep(2)
    return None

def tw_count():
    o = subprocess.run(["/usr/sbin/netstat", "-an", "-p", "tcp"], capture_output=True, text=True).stdout
    return o.count("TIME_WAIT")

def drain_ports(limit=2000, timeout=120):
    """Wait for loopback TIME_WAIT sockets (left by a flood) to drain below `limit` so ephemeral ports are available again."""
    t = time.time()
    while time.time() - t < timeout:
        if tw_count() < limit: return round(time.time() - t, 1)
        time.sleep(2)
    return None

#!/usr/bin/env python3
"""Smoke test of the embedded sidecar of a built (unsigned) .app WITHOUT launching its GUI.

  python3 -I packaging/candidate/check_app_smoke.py --sidecar "<.../Agentic Apps.app/Contents/MacOS/redevops-sidecar>" --work DIR --log FILE

Everything runs under `env -i` with a throwaway HOME/TMPDIR below --work. Secrets (hand-off secret, launch
codes, cookies, CSRF tokens) are never written to the log. Leftover processes are killed by NAMED PID/PGID only;
`_MEI*` directories created by the run are removed by exact name after being recorded.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
from http.client import HTTPConnection
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

RESULTS: list[tuple[str, bool, str]] = []
LOGF = None


def log(msg: str) -> None:
    print(msg, flush=True)
    if LOGF:
        LOGF.write(msg + "\n")
        LOGF.flush()


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    log(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    return bool(ok)


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Env:
    def __init__(self, work: Path, name: str):
        self.home = work / name / "home"
        self.tmp = work / name / "tmp"
        self.home.mkdir(parents=True)
        self.tmp.mkdir(parents=True)

    def env(self, **extra: str) -> dict[str, str]:
        e = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "TMPDIR": str(self.tmp)}
        e.update(extra)
        return e

    def mei(self) -> list[str]:
        return sorted(p.name for p in self.tmp.glob("_MEI*"))


def ps_table() -> list[tuple[int, int, int, str]]:
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,command="], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(.*)", line)
        if m:
            rows.append((int(m[1]), int(m[2]), int(m[3]), m[4]))
    return rows


def procs_of(sidecar: str) -> list[tuple[int, int, int, str]]:
    return [r for r in ps_table() if r[3].startswith(sidecar) and r[0] != os.getpid()]


def port_open(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def http(port: int, method: str, path: str, headers: dict | None = None, body: bytes | None = None, host: str | None = None):
    h = {"Host": host or f"127.0.0.1:{port}", "Connection": "close"}
    h.update(headers or {})
    for attempt in range(40):          # transient local socket errors (EADDRNOTAVAIL under load) are retried, HTTP answers never
        try:
            c = HTTPConnection("127.0.0.1", port, timeout=10)
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            break
        except OSError:
            if attempt == 39:
                raise
            time.sleep(0.5)
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return r.status, hdrs, data


def wait_for(pred, timeout=30.0, step=0.2):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def reap(proc: subprocess.Popen | None, sidecar: str, env: Env, tag: str) -> None:
    """Kill leftovers by named PID/PGID only and remove the _MEI dirs of THIS run by exact name."""
    leftovers = procs_of(sidecar)
    if leftovers:
        log(f"  [{tag}] leftovers: {[(p[0], p[1], p[2]) for p in leftovers]}")
        for pid, _ppid, _pgid, _cmd in leftovers:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    if proc is not None:
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    for name in env.mei():
        d = env.tmp / name
        log(f"  [{tag}] removing leftover {name} (by exact name)")
        for root, dirs, files in os.walk(d, topdown=False):
            for f in files:
                os.unlink(os.path.join(root, f))
            for dd in dirs:
                p = os.path.join(root, dd)
                os.unlink(p) if os.path.islink(p) else os.rmdir(p)
        os.rmdir(d)


def parse_standalone_url(stderr_path: Path):
    txt = stderr_path.read_text(errors="replace") if stderr_path.exists() else ""
    m = re.search(r"http://127\.0\.0\.1:(\d+)/#lc=([A-Za-z0-9_-]+)", txt)
    return (int(m[1]), m[2]) if m else None


def session(port: int, code: str):
    st, hd, body = http(port, "POST", "/api/local/session", {"Content-Type": "application/json"}, json.dumps({"code": code}).encode())
    if st != 200:
        return st, None, None
    ck = re.match(r"(agentic_os_session=[^;]+)", hd.get("set-cookie", ""))
    return st, (ck[1] if ck else None), json.loads(body)["csrf_token"]


def api_checks(label: str, port: int, code: str) -> None:
    st, _, _ = http(port, "GET", "/api/projects")
    check(f"{label}: /api/projects without a session is 401", st == 401, f"status {st}")
    st, _, _ = http(port, "GET", "/openapi.json")
    check(f"{label}: /openapi.json without a session is 401", st == 401, f"status {st}")
    st, hd, body = http(port, "GET", "/")
    check(f"{label}: the UI root is public (200, html)", st == 200 and (b"<html" in body.lower()[:2000] or b"<!doctype" in body.lower()[:200]), f"status {st}")
    st, cookie, csrf = session(port, code)
    check(f"{label}: launch code exchange gives a session cookie and a CSRF token", st == 200 and bool(cookie) and bool(csrf), f"status {st}")
    st2, _, _ = http(port, "POST", "/api/local/session", {"Content-Type": "application/json"}, json.dumps({"code": code}).encode())
    check(f"{label}: the launch code is single use (replay refused)", st2 == 401, f"status {st2}")
    if not cookie:
        return
    st, hd, body = http(port, "GET", "/api/projects", {"Cookie": cookie})
    try:
        projects = json.loads(body)
    except Exception:
        projects = None
    check(f"{label}: /api/projects with the session is 200 and lists projects", st == 200 and projects is not None, f"status {st}, {len(body)} bytes")
    st, hd, body = http(port, "GET", "/openapi.json", {"Cookie": cookie})
    ok = st == 200
    npaths = 0
    if ok:
        try:
            doc = json.loads(body)
            npaths = len(doc.get("paths", {}))
            ok = bool(doc.get("openapi")) and npaths > 10
        except Exception:
            ok = False
    check(f"{label}: /openapi.json with the session is 200 (defect d)", ok, f"status {st}, {npaths} paths")
    st, hd, _ = http(port, "GET", "/hero.jpg")
    check(f"{label}: /hero.jpg is served as image/jpeg", st == 200 and hd.get("content-type", "").startswith("image/jpeg"), f"status {st}")
    st, hd, _ = http(port, "GET", "/api/projects", {"Cookie": cookie}, host="evil.example")
    check(f"{label}: a foreign Host header is refused (DNS rebinding)", st in (400, 403), f"status {st}")
    st, hd, _ = http(port, "OPTIONS", "/api/projects", {"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
    check(f"{label}: cross-origin preflight is refused and carries no CORS headers", st == 403 and not any(k.startswith("access-control-") for k in hd), f"status {st}")


def start(sidecar: str, env: Env, args: list[str], stdout_path: Path, stderr_path: Path, stdin=subprocess.DEVNULL, **extra):
    fo, fe = open(stdout_path, "wb"), open(stderr_path, "wb")
    p = subprocess.Popen([sidecar, *args], env=env.env(**extra), stdin=stdin, stdout=fo, stderr=fe, start_new_session=True)
    return p


def main() -> int:
    global LOGF
    ap = argparse.ArgumentParser()
    ap.add_argument("--sidecar", required=True)
    ap.add_argument("--work", required=True, type=Path)
    ap.add_argument("--log", required=True, type=Path)
    a = ap.parse_args()
    LOGF = open(a.log, "w")
    sidecar = os.path.realpath(a.sidecar)
    work = a.work
    work.mkdir(parents=True, exist_ok=True)
    log(f"sidecar: {sidecar}")
    log(f"sha256 : {hashlib.sha256(Path(sidecar).read_bytes()).hexdigest()}")
    log(f"time   : {time.strftime('%Y-%m-%dT%H:%M:%S%z')}   host: {os.uname().sysname} {os.uname().release} {os.uname().machine}")

    # ---------------------------------------------------------------- standalone mode
    log("\n== A. standalone mode (serve, PROJECTS_PORT=0, env -i) ==")
    e = Env(work, "standalone")
    p = start(sidecar, e, ["serve"], work / "standalone.out", work / "standalone.err", PROJECTS_PORT="0")
    got = wait_for(lambda: parse_standalone_url(work / "standalone.err"), 40)
    check("standalone: the one-time URL is printed on stderr", got is not None)
    out_txt = (work / "standalone.out").read_text(errors="replace")
    check("standalone: neither the URL nor a code is on stdout", "#lc=" not in out_txt and "lc=" not in out_txt)
    if got:
        port, code = got
        check("standalone: the printed port accepts connections on loopback", bool(wait_for(lambda: port_open(port), 10)))
        api_checks("standalone", port, code)
        rows = procs_of(sidecar)
        log(f"  process tree: {[(r[0], r[1], r[2]) for r in rows]} (pid, ppid, pgid); _MEI: {e.mei()}")
        check("standalone: one extraction directory exists while running", len(e.mei()) == 1, str(e.mei()))
        mei = e.tmp / e.mei()[0] if e.mei() else None
        if mei:
            check("standalone: the extraction directory is owner-only (mode 0700)", (mei.stat().st_mode & 0o777) == 0o700, oct(mei.stat().st_mode & 0o777))
        # SIGTERM to the bootloader process
        t0 = time.time()
        os.kill(p.pid, signal.SIGTERM)
        done = wait_for(lambda: p.poll() is not None, 15)
        wait_for(lambda: not procs_of(sidecar), 10)        # the python child may need a moment on a loaded host
        check("SIGTERM: the bootloader exits", done is not None, f"rc {p.poll()}, {time.time() - t0:.1f}s")
        check("SIGTERM: no process of the sidecar is left", not procs_of(sidecar), str([(r[0], r[1]) for r in procs_of(sidecar)]))
        check("SIGTERM: the port is closed", not port_open(port))
        check("SIGTERM: no _MEI directory is left", not e.mei(), str(e.mei()))
    reap(p, sidecar, e, "standalone")

    # ---------------------------------------------------------------- handshake mode
    log("\n== B. launcher hand-off mode (AGENTIC_OS_HANDOFF=stdout, PROJECTS_PORT=0, --exit-on-stdin-eof) ==")
    e = Env(work, "handoff")
    fo, fe = open(work / "handoff.out", "wb"), open(work / "handoff.err", "wb")
    p = subprocess.Popen([sidecar, "serve", "--exit-on-stdin-eof"], env=e.env(PROJECTS_PORT="0", PROJECTS_HOST="127.0.0.1", AGENTIC_OS_HANDOFF="stdout"),
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=fe, start_new_session=True)
    line = b""
    t0 = time.time()
    while time.time() - t0 < 40 and not line.endswith(b"\n"):
        ch = p.stdout.read(1)
        if not ch:
            break
        line += ch
        if len(line) > 4096:
            break
    rec = None
    try:
        rec = json.loads(line)
    except Exception:
        pass
    check("hand-off: ONE JSON line on stdout ending in a single LF (no CR)", rec is not None and line.endswith(b"\n") and not line.endswith(b"\r\n") and line.count(b"\n") == 1)
    if rec:
        required = {"v", "proto", "event", "host", "port", "pid", "base_path", "secret"}
        check("hand-off: required fields present, event ready, host 127.0.0.1", required <= set(rec) and rec["event"] == "ready" and rec["host"] == "127.0.0.1", f"fields {sorted(set(rec) - {'secret'})}")
        check("hand-off: no launch code in the record by default", "launch_code" not in rec)
        port, secret = int(rec["port"]), b64u_dec(rec["secret"])
        check("hand-off: the secret is 32 bytes", len(secret) == 32)
        # constant-time HMAC handshake as the launcher does (docs/LOCAL_API_SECURITY.md 4.2/4.4)
        nonce = b64u(os.urandom(16))
        st, _, body = http(port, "POST", "/api/local/handshake", {"Content-Type": "application/json"}, json.dumps({"client_nonce": nonce}).encode())
        hs = json.loads(body) if st == 200 else {}
        want = b64u(hmac.new(secret, f"agentic-os-handshake-v1\n{port}\n{nonce}".encode(), hashlib.sha256).digest())
        check("handshake: the sidecar proves possession of the hand-off secret (HMAC verified)", st == 200 and hmac.compare_digest(want, hs.get("proof", "")), f"status {st}")
        # a wrong key must NOT verify (control)
        bad = b64u(hmac.new(os.urandom(32), f"agentic-os-handshake-v1\n{port}\n{nonce}".encode(), hashlib.sha256).digest())
        check("handshake (control): a proof made with another key does not match", not hmac.compare_digest(bad, hs.get("proof", "")))
        sn = hs.get("server_nonce", "")
        mp = b64u(hmac.new(secret, f"agentic-os-mint-v1\n{port}\n{sn}".encode(), hashlib.sha256).digest())
        st, _, body = http(port, "POST", "/api/local/launch-code", {"Content-Type": "application/json", "X-Launcher-Proof": mp}, json.dumps({"server_nonce": sn}).encode())
        code = json.loads(body).get("launch_code") if st == 200 else None
        check("mint: a launch code is issued for a correct proof", st == 200 and bool(code), f"status {st}")
        st, _, _ = http(port, "POST", "/api/local/launch-code", {"Content-Type": "application/json", "X-Launcher-Proof": mp}, json.dumps({"server_nonce": sn}).encode())
        check("mint (control): the challenge is single use (replay refused)", st == 401, f"status {st}")
        if code:
            api_checks("hand-off", port, code)
        banner = (work / "handoff.err").read_text(errors="replace")
        check("hand-off: the secret never appears in stderr", rec["secret"] not in banner)
        # lifecycle: close the stdin tether -> graceful shutdown, no _MEI
        p.stdin.close()
        done = wait_for(lambda: p.poll() is not None, 20)
        time.sleep(1.0)
        check("tether: closing stdin stops the sidecar gracefully", done is not None, f"rc {p.poll()}")
        check("tether: no process left, port closed, no _MEI", not procs_of(sidecar) and not port_open(port) and not e.mei(), f"procs {[(r[0], r[1]) for r in procs_of(sidecar)]} mei {e.mei()}")
    reap(p, sidecar, e, "handoff")

    # ---------------------------------------------------------------- bind conflict
    log("\n== C. a taken port is reported as bind_failed (never adopted) ==")
    e = Env(work, "bind")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    taken = srv.getsockname()[1]
    r = subprocess.run([sidecar, "serve"], env=e.env(PROJECTS_PORT=str(taken), PROJECTS_HOST="127.0.0.1", AGENTIC_OS_HANDOFF="stdout"),
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    srv.close()
    try:
        j = json.loads(r.stdout.decode().strip().splitlines()[0])
    except Exception:
        j = {}
    check("bind conflict: rc 1 and an error line without a secret", r.returncode == 1 and j.get("code") == "bind_failed" and "secret" not in j, f"rc {r.returncode} {j.get('code')}")
    check("bind conflict: no _MEI left", not e.mei(), str(e.mei()))
    reap(None, sidecar, e, "bind")

    # ---------------------------------------------------------------- bootstrap, locales
    log("\n== D. bootstrap with redirected stdout under different locales ==")
    locs = subprocess.run(["locale", "-a"], capture_output=True, text=True).stdout.split()
    variants = [("UTF-8", {"LANG": "en_US.UTF-8"}), ("LC_ALL=C", {"LC_ALL": "C"}), ("LANG=C", {"LANG": "C"}), ("no locale vars", {}),
                ("LC_ALL=POSIX", {"LC_ALL": "POSIX"})]
    for cand in ("en_US.ISO8859-1", "ja_JP.eucJP", "ja_JP.SJIS", "ru_RU.KOI8-R", "ru_RU.ISO8859-5", "zh_CN.GB18030", "zh_TW.Big5"):
        if cand in locs:
            variants.append((f"LC_ALL={cand} (non-UTF-8)", {"LC_ALL": cand}))
    base = None
    for name, extra in variants:
        e = Env(work, "boot-" + re.sub(r"\W+", "_", name))
        outp = work / ("boot-" + re.sub(r"\W+", "_", name) + ".out")
        with open(outp, "wb") as fo:
            r = subprocess.run([sidecar, "bootstrap"], env=e.env(RDO_DISABLE_PAIR="1", **extra), stdin=subprocess.DEVNULL, stdout=fo, stderr=subprocess.PIPE, timeout=120)
        data = outp.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if base is None:
            base = (sha, r.returncode, len(data))
        ok = (sha, r.returncode) == base[:2] and b"UnicodeEncodeError" not in r.stderr and b"Traceback" not in r.stderr and len(data) > 0
        check(f"bootstrap > file under {name}", ok, f"rc {r.returncode}, {len(data)} bytes, sha256 {sha[:12]}")
        reap(None, sidecar, e, "boot")
    log(f"  (rc {base[1]} is the device verdict of THIS Mac, not a failure of the binary)")
    log(f"  non-UTF-8 locales installed on this host: {[l for l in locs if 'UTF-8' not in l and l not in ('C','POSIX')][:8]}")

    # ---------------------------------------------------------------- SIGKILL
    log("\n== E. SIGKILL of the bootloader (standalone, no stdin tether) - observed behaviour ==")
    e = Env(work, "sigkill")
    p = start(sidecar, e, ["serve"], work / "sigkill.out", work / "sigkill.err", PROJECTS_PORT="0")
    got = wait_for(lambda: parse_standalone_url(work / "sigkill.err"), 40)
    if got:
        port, _ = got
        before = procs_of(sidecar)
        os.kill(p.pid, signal.SIGKILL)
        time.sleep(2.0)
        after = procs_of(sidecar)
        log(f"  before: {[(r[0], r[1]) for r in before]}  after SIGKILL of pid {p.pid}: {[(r[0], r[1]) for r in after]}; port open: {port_open(port)}; _MEI: {e.mei()}")
        # record facts only: this is the documented limit of a onefile bootloader (the python child is orphaned)
        check("SIGKILL (record): bootloader is gone", p.poll() is not None, f"rc {p.poll()}")
        log(f"  OBSERVED: orphaned python child still serving = {port_open(port)}; _MEI left = {bool(e.mei())}")
        SIGKILL_FACTS.update({"orphan_serving": port_open(port), "mei_left": bool(e.mei())})
    else:
        check("SIGKILL scenario started", False)
    reap(p, sidecar, e, "sigkill")
    check("SIGKILL cleanup: no process left and no _MEI left after named-PID cleanup", not procs_of(sidecar) and not e.mei())

    log("\n== E2. SIGKILL of the bootloader only, then the stdin tether is closed (what a crashed launcher looks like) ==")
    e = Env(work, "sigkill-group")
    p = subprocess.Popen([sidecar, "serve", "--exit-on-stdin-eof"], env=e.env(PROJECTS_PORT="0", AGENTIC_OS_HANDOFF="stdout"), stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    l2 = p.stdout.readline()
    try:
        port2 = json.loads(l2)["port"]
    except Exception:
        port2 = None
    if port2:
        # kill ONLY the bootloader, then close the tether: the orphaned python child must stop by itself
        os.kill(p.pid, signal.SIGKILL)
        time.sleep(1.0)
        orphan = procs_of(sidecar)
        p.stdin.close()
        gone = wait_for(lambda: not procs_of(sidecar), 15)
        check("tether after SIGKILL of the bootloader: the orphaned python child exits when the pipe closes", gone is not None and not port_open(port2),
              f"orphans before close: {[(r[0], r[1]) for r in orphan]}")
        log(f"  _MEI after this: {e.mei()} (the bootloader that would delete it was SIGKILLed: a leak by construction)")
        SIGKILL_FACTS["mei_left_after_bootloader_kill_with_tether"] = bool(e.mei())
    reap(p, sidecar, e, "sigkill-group")

    log("\n== E3. SIGKILL of the whole process group (what the launcher's 3 s escalation does) ==")
    e = Env(work, "sigkill-pg")
    p = start(sidecar, e, ["serve"], work / "sigkillpg.out", work / "sigkillpg.err", PROJECTS_PORT="0")
    got = wait_for(lambda: parse_standalone_url(work / "sigkillpg.err"), 40)
    if got:
        port3, _ = got
        os.killpg(p.pid, signal.SIGKILL)          # the group was created by start_new_session: pgid == bootloader pid
        time.sleep(1.5)
        check("group SIGKILL: no process left and the port is closed", not procs_of(sidecar) and not port_open(port3), str([(r[0], r[1]) for r in procs_of(sidecar)]))
        log(f"  OBSERVED: _MEI left after a group SIGKILL = {bool(e.mei())} ({e.mei()})")
        SIGKILL_FACTS["mei_left_after_group_sigkill"] = bool(e.mei())
    reap(p, sidecar, e, "sigkill-pg")

    # leftovers anywhere?
    left = procs_of(sidecar)
    check("end: no sidecar process of this run remains", not left, str([(r[0], r[1]) for r in left]))
    fails = [r for r in RESULTS if not r[1]]
    log(f"\nSUMMARY: {len(RESULTS) - len(fails)} passed, {len(fails)} failed of {len(RESULTS)} checks; sigkill facts {SIGKILL_FACTS}")
    (work / "smoke-results.json").write_text(json.dumps({"checks": [{"name": n, "ok": o, "detail": d} for n, o, d in RESULTS], "sigkill_facts": SIGKILL_FACTS}, indent=1))
    return 1 if fails else 0


SIGKILL_FACTS: dict = {}

if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Desktop-shell acceptance run — measures the milestone's criteria against a REAL shell binary and a REAL deployment.

  1. same Today in the shell as in Chromium (the same journey script runs in both; outputs are compared)
  2. restart restores the workspace and server-side state (kill the shell, relaunch, observe)
  3. an unavailable runtime does not freeze the UI and recovers by itself (backend stopped, then restarted)
  4. credentials are not exposed (not in the config file, not in the shell's output; present only in the OS keychain)

Needs: a shell binary built with `--features selftest,custom-protocol`, a running deployment, and (for 1) Playwright.
Usage: run.py --binary PATH --url http://127.0.0.1:8799 [--backend-cmd "…" (enables the outage phase)] [--chromium-script PATH]
macOS only for the keychain phase (uses `security`); it is skipped elsewhere. Windows/Linux are exercised by CI builds + unit tests.
"""
import argparse, json, os, pathlib, platform, shutil, signal, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = pathlib.Path(__file__).resolve().parent
REPORTS, LOCK = [], threading.Lock()

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0) or 0)).decode("utf-8", "replace")
        try: d = json.loads(body)
        except Exception: d = {"raw": body}
        d["_t"] = time.time()
        with LOCK: REPORTS.append(d)
        self.send_response(204); self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers()
    def do_OPTIONS(self):
        self.send_response(204); [self.send_header(k, v) for k, v in (("Access-Control-Allow-Origin", "*"), ("Access-Control-Allow-Headers", "*"), ("Access-Control-Allow-Methods", "POST"))]; self.end_headers()
    def log_message(self, *a): pass

def tail_log(path, stop_evt):          # the shell's own pages report through IPC → a marked line on its stderr
    pos = 0
    while not stop_evt.is_set():
        try:
            with open(path, "rb") as f:
                f.seek(pos); chunk = f.read(); pos += len(chunk)
            for line in chunk.decode("utf-8", "replace").splitlines():
                if line.startswith("[selftest-report] "):
                    try: d = json.loads(line[len("[selftest-report] "):])
                    except Exception: continue
                    d["_t"] = time.time()
                    with LOCK: REPORTS.append(d)
        except FileNotFoundError: pass
        time.sleep(0.2)

def script(name):                      # lib + journey in one expression, written to a temp file the shell reads
    lib = (HERE / "_lib.js").read_text()
    s = (HERE / name).read_text().replace("/*LIB*/", lib)
    p = pathlib.Path(tempfile.mkdtemp()) / name
    p.write_text(s); return str(p)

def wait_report(tag, since, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        with LOCK:
            for r in REPORTS:
                if r.get("tag") == tag and r["_t"] > since: return r
        time.sleep(0.2)
    return None

def launch(binary, env_extra, logfile):
    env = {**os.environ, **env_extra}
    f = open(logfile, "ab")
    return subprocess.Popen([binary], env=env, stdout=f, stderr=f, start_new_session=True)

def stop(p):
    if p and p.poll() is None:
        try: os.killpg(p.pid, signal.SIGTERM)
        except Exception: pass
        try: p.wait(5)
        except Exception:
            try: os.killpg(p.pid, signal.SIGKILL)
            except Exception: pass

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", required=True); ap.add_argument("--url", required=True)
    ap.add_argument("--backend-cmd"); ap.add_argument("--chromium-script")
    ap.add_argument("--token", default="SELFTEST-TOKEN-" + os.urandom(6).hex())
    a = ap.parse_args()
    srv = HTTPServer(("127.0.0.1", 0), H); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    work = pathlib.Path(tempfile.mkdtemp(prefix="rdo-selftest-")); cfg = work / "config"; logf = work / "shell.log"
    stop_tail = threading.Event(); threading.Thread(target=tail_log, args=(logf, stop_tail), daemon=True).start()
    suffix = os.urandom(4).hex()
    base = {"RDO_SELFTEST_ACCOUNT_SUFFIX": suffix, "RDO_CONFIG_DIR": str(cfg), "RDO_SELFTEST_URL": a.url, "RDO_SELFTEST_REPORT": str(port), "RDO_SELFTEST_APP_SCRIPT": script("app.js")}
    res, checks = {}, []
    be = None
    if a.backend_cmd:                  # the deployment under test is ours to start/stop (needed for the outage phase)
        be = subprocess.Popen(a.backend_cmd, shell=True, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            try:
                import urllib.request; urllib.request.urlopen(a.url.rstrip("/") + "/api/projects", timeout=1); break
            except Exception: time.sleep(0.5)
    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail)); print(("PASS " if ok else "FAIL ") + name + (f"  — {detail}" if detail else ""))

    # ── 1+2a: fresh run (with a token, to test credential handling), the full journey ───────────────────────────
    t = time.time(); p = launch(a.binary, {**base, "RDO_SELFTEST_SCRIPT": script("act.js"), "RDO_SELFTEST_TOKEN": a.token}, logf)
    r1 = wait_report("deployment", t, 90); stop(p)
    check("shell: Today rendered and the journey (capture → review → approve → verify) completed", r1 and "result" in r1 and r1["result"].get("after"), json.dumps(r1.get("error", ""))[:200] if r1 else "no report")
    res["shell_fresh"] = r1.get("result") if r1 else None

    # ── 2: restart restores workspace + server-side state ────────────────────────────────────────────────────────
    t = time.time(); p = launch(a.binary, {**base, "RDO_SELFTEST_SCRIPT": script("observe.js")}, logf)
    r2 = wait_report("deployment", t, 60); stop(p)
    o = (r2 or {}).get("result") or {}
    check("restart: relaunch reconnects by itself and shows the same workspace", bool(o.get("headline")), o.get("headline", "no report"))
    check("restart: server-side state survived (the sent message is still 'waiting on others')", (o.get("jordan") or {}).get("group") == "Waiting on others", json.dumps(o.get("jordan")))
    s1 = ((res["shell_fresh"] or {}).get("env") or {}).get("session")
    check("restart: the same server-side session was reused (not a fresh empty ledger)", s1 and s1 == o.get("session"), f"{s1} vs {o.get('session')}")

    # ── 4: credentials ───────────────────────────────────────────────────────────────────────────────────────────────
    out = (work / "shell.log").read_text(errors="replace")
    conf = (cfg / "connection.json").read_text() if (cfg / "connection.json").exists() else ""
    check("credentials: token not in the shell's stdout/stderr across all runs", a.token not in out)
    check("credentials: token not in the connection file on disk", a.token not in conf and conf != "", conf.strip().replace("\n", " ")[:120])
    log = (work / "shell.log").read_text(errors="replace")
    check("credentials: the keychain write succeeded and the shell read back the same value (checked inside the app)", "keychain write ok: true; readback matches: true" in log)
    check("credentials: after a restart the stored token is found in the keychain and used (no re-entry needed)", log.count("probe credential: present") >= 1)
    if platform.system() == "Darwin":
        acct = "conn:" + a.url.rstrip("/") + "/#" + suffix
        # attributes only (no -w / -g): proves the item exists in the OS keychain WITHOUT reading the secret (which would prompt)
        k = subprocess.run(["security", "find-generic-password", "-s", "io.redevops.agentic-apps", "-a", acct], capture_output=True, text=True, timeout=15)
        check("credentials: the token lives in the OS keychain", k.returncode == 0 and acct in k.stdout)
        grep = subprocess.run(["grep", "-rl", a.token, str(work)], capture_output=True, text=True)
        check("credentials: token appears in no file under the run's working directory", grep.stdout.strip() == "", grep.stdout.strip()[:200])
        f = launch(a.binary, {**base, "RDO_SELFTEST_FORGET": "1"}, logf); f.wait(30)
        gone = subprocess.run(["security", "find-generic-password", "-s", "io.redevops.agentic-apps", "-a", acct], capture_output=True, timeout=15)
        check("credentials: the shell removed its own test keychain entry", gone.returncode != 0)

    # ── 1b: the same journey in Chromium, compared ──────────────────────────────────────────────────────────────────
    if a.chromium_script:
        c = subprocess.run(["node", a.chromium_script, a.url, script("act.js")], capture_output=True, text=True, timeout=120)
        try: ch = json.loads(c.stdout.strip().splitlines()[-1])
        except Exception: ch = None
        res["chromium"] = ch
        sh = res["shell_fresh"] or {}
        pick = lambda d: {k: (d or {}).get(k) for k in ("before", "capture", "lifecycle", "after")}
        norm = lambda d: json.loads(json.dumps(pick(d)))
        same = ch is not None and norm(ch) == norm(sh)
        check("parity: the SAME journey gives identical results in Chromium and the desktop shell", same,
              "" if same else "shell=" + json.dumps(norm(sh))[:300] + "  chromium=" + json.dumps(norm(ch) if ch else None)[:300])
        e1, e2 = (sh.get("env") or {}), ((ch or {}).get("env") or {})
        check("parity: same navigation (five places) and same first screen", e1.get("nav") == e2.get("nav") and e1.get("headline") == e2.get("headline"), f"{e1.get('nav')}")

    # ── 3: outage ───────────────────────────────────────────────────────────────────────────────────────────────────────
    if a.backend_cmd:
        t = time.time(); p = launch(a.binary, {**base, "RDO_SELFTEST_SCRIPT": script("observe.js")}, logf)
        pre = wait_report("deployment", t, 60)
        check("outage: shell is up and showing Today before the outage", pre and (pre.get("result") or {}).get("headline"))
        # take the runtime away
        t_kill = time.time(); os.killpg(be.pid, signal.SIGKILL); be.wait()
        seen_app = wait_report("app", t_kill, 60)
        t_app = (seen_app or {}).get("_t")
        check("outage: the window leaves the dead page for an explanation within ~30s (no frozen/blank UI)", seen_app and t_app - t_kill < 30, f"{round(t_app - t_kill, 1)}s" if t_app else "never")
        st = ((seen_app or {}).get("result") or {}).get("status", "")
        check("outage: the explanation says the runtime isn't responding and that work is safe", "isn't responding" in st and "safe" in st, st[:160])
        # bring it back
        t_up = time.time(); be = subprocess.Popen(a.backend_cmd, shell=True, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        back = wait_report("deployment", t_up, 90)
        check("outage: it reconnects by itself and returns to Today when the runtime is back", back and (back.get("result") or {}).get("headline"), f"{round(back['_t'] - t_up, 1)}s after restart" if back else "never")
        # the monitor's next look can be up to 30s away (backoff) — wait for it rather than reading the log too early
        t_end = time.time() + 45
        while time.time() < t_end and "GoOnline" not in (work / "shell.log").read_text(errors="replace"): time.sleep(1)
        log = (work / "shell.log").read_text(errors="replace")
        check("outage: the shell's monitor went offline then back online (state machine acted)", "GoOffline" in log and "GoOnline" in log)
        stop(p)
    if be and be.poll() is None:
        try: os.killpg(be.pid, signal.SIGKILL)
        except Exception: pass

    ok = all(c[1] for c in checks)
    print(f"\n{sum(1 for c in checks if c[1])}/{len(checks)} checks passed  ({'ALL PASS' if ok else 'FAILURES'});  work dir: {work}")
    json.dump({"checks": checks, "results": res}, open(work / "summary.json", "w"), indent=1, default=str)
    shutil.rmtree(cfg, ignore_errors=True)
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()

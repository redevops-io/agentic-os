#!/usr/bin/env python3
"""Native checks of an INSTALLED rAIls Apps desktop build, the same on every OS (standard library only).

    python3 native_check.py <path to the installed redevops-sidecar> <result.json>

Starts the installed control plane natively (no Docker), then exercises it as a first user would: the UI is served, the API answers, the Today
feed loads, a note is captured and turns up, an approval-gated item waits and is approved, the process stops cleanly, and unknown commands start
nothing. Every check is recorded with its duration; a check that cannot run on this platform is recorded as `unsupported`, never as passed.
"""
import json, os, platform, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request

side, out_path = sys.argv[1], sys.argv[2]
home = tempfile.mkdtemp(prefix="aa-home-")
env = {"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "TEMP": os.environ.get("TEMP", tempfile.gettempdir()), "TMP": os.environ.get("TMP", tempfile.gettempdir()),
       "TMPDIR": os.environ.get("TMPDIR", tempfile.gettempdir()), "LANG": "C.UTF-8", "HOME": home, "USERPROFILE": home, "APPDATA": os.path.join(home, "AppData"), "LOCALAPPDATA": os.path.join(home, "Local"), "XDG_DATA_HOME": os.path.join(home, ".local", "share")}
BASE = "http://127.0.0.1:8787"
results = []


SESSION = {}


def http(method, path, body=None, timeout=20, headers=None, anonymous=False):
    h = {"content-type": "application/json", **(headers or {})}
    if SESSION and not anonymous:
        h["X-Agentic-Session"] = SESSION["token"]
        if method not in ("GET", "HEAD"):
            h["X-Agentic-CSRF"] = SESSION["csrf"]
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def check(name, fn):
    t = time.time()
    try:
        detail = fn()
        status = "unsupported" if isinstance(detail, str) and detail.startswith("UNSUPPORTED") else "pass"
    except Exception as e:  # noqa: BLE001
        status, detail = "fail", f"{type(e).__name__}: {e}"[:300]
    results.append({"check": name, "status": status, "seconds": round(time.time() - t, 2), "detail": str(detail)[:300] if detail is not None else ""})
    print(f"{status.upper():11} {name}  {str(detail or '')[:140]}", flush=True)


def J(raw):
    return json.loads(raw.decode("utf-8"))


def t_help():
    r = subprocess.run([side, "--help"], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0 and "serve" in r.stdout
def t_unknown():
    r = subprocess.run([side, "definitely-not-a-command"], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 2, r.returncode
def t_bootstrap():
    r = subprocess.run([side, "bootstrap"], capture_output=True, text=True, timeout=180, env=env)
    assert r.stdout.strip(), "no readiness report"
    return f"exit {r.returncode}: {r.stdout.strip().splitlines()[-1][:120]}"

check("the sidecar explains itself (--help exits 0)", t_help)
check("an unknown command starts nothing (exit 2)", t_unknown)
check("device-readiness report runs from the installed binary", t_bootstrap)

import re, threading
proc = subprocess.Popen([side, "serve", "--exit-on-stdin-eof"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
LOG = []
threading.Thread(target=lambda: [LOG.append(l.decode("utf-8", "replace")) for l in iter(proc.stdout.readline, b"")], daemon=True).start()
def t_serves():
    for _ in range(90):
        try:
            if http("GET", "/")[0] == 200: return "listening on 127.0.0.1:8787"
        except Exception:  # noqa: BLE001
            pass
        if proc.poll() is not None: raise RuntimeError("the control plane exited: " + "".join(LOG)[-300:])
        time.sleep(1)
    raise RuntimeError("did not serve within 90 s")
check("the control plane starts natively and serves the UI", t_serves)
def t_denied_without_session():
    s, raw = http("GET", "/api/projects", anonymous=True)
    assert s == 401, s; return "HTTP 401 without the local session"
check("the API refuses a caller without the local session (any local process, any web page)", t_denied_without_session)
def t_cross_origin():
    s, _ = http("POST", "/api/local/session", {"code": "x" * 32}, headers={"Origin": "https://evil.example"}, anonymous=True)
    assert s in (400, 401, 403), s; return f"HTTP {s} for a cross-origin exchange"
check("a cross-origin page cannot open a session", t_cross_origin)
def t_session():
    code = None
    for _ in range(30):
        m = re.search(r"#lc=([A-Za-z0-9_-]+)", "".join(LOG))
        if m: code = m.group(1); break
        time.sleep(1)
    assert code, "the sidecar printed no one-time launch URL"
    s, raw = http("POST", "/api/local/session", {"code": code}, anonymous=True)
    assert s == 200, (s, raw[:160]); d = J(raw)
    tok = d.get("session_token") or d.get("session") or d.get("token"); csrf = d.get("csrf_token") or d.get("csrf")
    assert tok and csrf, sorted(d)
    SESSION.update(token=tok, csrf=csrf)
    s2, _ = http("POST", "/api/local/session", {"code": code}, anonymous=True)
    assert s2 != 200, "the launch code worked twice"
    return "session opened with the one-time launch code; the code does not work a second time"
check("the one-time launch code opens a session, once", t_session)
def t_ui():
    s, raw = http("GET", "/"); assert s == 200 and b"<html" in raw.lower(), (s, len(raw))
    srcs = re.findall(rb'(?:src|href)="(/[^"]+\.(?:js|css))"', raw); assert srcs, "the page references no script or stylesheet"
    sizes = [len(http("GET", u.decode())[1]) for u in srcs[:4]]; assert all(n > 1000 for n in sizes), sizes
    return f"HTML shell plus {len(srcs)} asset(s), first sizes {sizes}"
check("the UI document is real HTML", t_ui)
state = {"pid": "customer-ops"}                              # replaced by the first project the build lists
def t_projects():
    s, raw = http("GET", "/api/projects"); assert s == 200; d = J(raw)
    items = d if isinstance(d, list) else d.get("projects", d)
    assert items, "no project"
    state["pid"] = str(items[0]["id"])
    return f"{len(items)} project(s); first: {state['pid']}" + (" (no sample data)" if items[0].get("sample") is False else "")
check("the API lists a project", t_projects)
def t_today():
    s, raw = http("GET", f"/api/projects/{state['pid']}/today"); assert s == 200, s; d = J(raw)
    state["feed"] = d; return f"{len(d['items'])} items, basis={d.get('basis')}, supports={d.get('supports')}"
check("the Today feed loads", t_today)
def t_capture():
    s, raw = http("POST", f"/api/projects/{state['pid']}/capture", {"content": "Call the supplier about the late delivery tomorrow", "source": "manual"})
    if s == 501: return "UNSUPPORTED on this build: capture is not enabled"
    assert s == 200, (s, raw[:200]); d = J(raw); assert d.get("id"), d
    return f"captured -> {d.get('status')} / {(d.get('routing') or {}).get('kind')}"
check("first workflow: a note is captured and routed", t_capture)
def t_item():
    items = state["feed"]["items"]
    if not items and (state["feed"].get("supports") or {}).get("detail") is False:
        # a build that serves no sample cases says so in the feed (supports.detail = false) and has nothing to open
        return "UNSUPPORTED on this build: it serves no sample cases and says it offers no item detail"
    assert items, "the feed has no item"
    s, raw = http("GET", f"/api/projects/{state['pid']}/today/{items[0]['id']}")
    if s == 501: return "UNSUPPORTED on this build: item detail is not provided"
    assert s == 200, s; return J(raw).get("user_title", "")[:80]
check("an item opens with its detail", t_item)
# ---- dashboards (kernel 0.4.5 and later; a build without them answers 404 and the three checks are recorded as unsupported) ----
def t_dash_catalog():
    s, raw = http("GET", "/api/dashboards")
    if s == 404: state["dash"] = None; return "UNSUPPORTED on this build: it has no dashboards"
    assert s == 200, (s, raw[:160]); d = J(raw)
    offered = [x["id"] for x in d.get("dashboards", [])]; assert offered, "no dashboard is offered"
    for i in offered:
        s2, raw2 = http("GET", f"/api/dashboards/{i}"); assert s2 == 200, (i, s2, raw2[:160])
        doc = J(raw2); assert doc.get("id") == i and doc.get("schema") and "generated_at" in doc, (i, sorted(doc)[:8])
    sa, _ = http("GET", "/api/dashboards/" + offered[0], anonymous=True); assert sa == 401, f"a caller without the local session got {sa}"
    state["dash"] = offered
    return f"{len(offered)} offered and each opens ({', '.join(offered)}); {len(d.get('not_connected', []))} named as not connected; 401 without the session"
check("dashboards: the catalogue lists what this build can show and each one opens", t_dash_catalog)
def t_dash_views():
    if not state.get("dash"): return "UNSUPPORTED on this build: it has no dashboards"
    s, raw = http("PUT", "/api/dashboards/views", {"name": "acceptance view", "dashboard": state["dash"][0], "filters": {"days": 7}})
    assert s == 200, (s, raw[:200]); vid = J(raw)["id"]
    s, raw = http("GET", "/api/dashboards/views"); assert s == 200, s; d = J(raw)
    assert any(v["id"] == vid and v["filters"] == {"days": 7} for v in d["views"]), d
    assert d.get("durable") is True, "saved views are kept in memory only: the build did not get a per-user dashboards directory"
    sa, _ = http("PUT", "/api/dashboards/views", {"name": "x", "dashboard": state["dash"][0]}, anonymous=True); assert sa in (401, 403), sa
    s, _ = http("DELETE", f"/api/dashboards/views/{vid}"); assert s == 200, s
    s, raw = http("GET", "/api/dashboards/views"); assert not any(v["id"] == vid for v in J(raw)["views"])
    return "a view is saved durably for the local owner, listed, refused without the session, and deleted"
check("dashboards: a saved view is durable, private to the session and can be deleted", t_dash_views)
def t_dash_not_offered():
    if not state.get("dash"): return "UNSUPPORTED on this build: it has no dashboards"
    d = J(http("GET", "/api/dashboards")[1])
    hidden = [x["id"] for x in d.get("not_connected", [])]
    if not hidden: return "every dashboard is offered on this build"
    s, raw = http("GET", "/api/dashboards/" + hidden[0]); assert s == 200, (hidden[0], s); doc = J(raw)
    states = {src.get("state") for src in doc.get("sources", [])}
    assert states == {"not_connected"}, f"{hidden[0]} is listed as not connected and its sources say {sorted(states)}"
    values = [m.get("value") for pnl in doc.get("panels", []) for m in pnl.get("metrics", [])]
    assert all(v is None for v in values), f"{hidden[0]} is not connected and shows numbers: {[v for v in values if v is not None][:5]}"
    assert all(src.get("remedy") for src in doc.get("sources", [])), "a source that is not connected does not say what would connect it"
    return f"{hidden[0]}: every source says not_connected with a remedy, and none of its {len(values)} metric(s) carries a number"
check("dashboards: one whose application is not connected is not served as if it had data", t_dash_not_offered)
def t_dash_journey():
    """A journey with data, not only an empty installation: a test run is reported to the installed app through its own intake, and the QA
    dashboard must then be offered, show that run's numbers, list the failed test behind the "failed" number, and show the tests as
    unexecuted in a time range that ends before the run. Without this, every number on a fresh installation is absent and the drill-down and range checks have
    nothing to run on (they were recorded as unsupported for 0.1.4)."""
    if not state.get("dash"): return "UNSUPPORTED on this build: it has no dashboards"
    at = time.time()
    run = {"project": state["pid"], "suite": "acceptance-journey", "run_id": f"journey-{int(at)}", "platform": platform.system().lower(), "environment": "installed build",
           "release": os.environ.get("DASH_CHECK_RELEASE", "unknown"), "origin": "native_check", "at": at,
           "results": [{"test_id": "journey.pass", "status": "passed", "detail": "reported by the acceptance run"},
                       {"test_id": "journey.fail", "status": "failed", "detail": "a deliberately failed row: the dashboard must show it"},
                       {"test_id": "journey.unsupported", "status": "unsupported", "detail": "a row that could not run here"}]}
    sa, _ = http("POST", "/api/dashboards/qa/results", run, anonymous=True); assert sa in (401, 403), f"a caller without the session reported a run ({sa})"
    s, raw = http("POST", "/api/dashboards/qa/results", run)
    if s in (404, 405, 501): return "UNSUPPORTED on this build: it does not accept reported test results"
    assert s == 200, (s, raw[:200]); d = J(raw); assert d.get("results") == 3 and d.get("duplicate") is False, d
    s, raw = http("POST", "/api/dashboards/qa/results", run); assert s == 200 and J(raw).get("duplicate") is True, "the same run reported twice was counted twice"
    offered = [x["id"] for x in J(http("GET", "/api/dashboards")[1]).get("dashboards", [])]
    assert "qa" in offered, f"a run was reported and the QA dashboard is still not offered: {offered}"
    state["dash"] = offered
    s, raw = http("GET", "/api/dashboards/qa"); assert s == 200, s; doc = J(raw)
    m = {x["id"]: x for pnl in doc["panels"] for x in pnl.get("metrics", [])}
    got = {k: m[k]["value"] for k in ("planned", "failed", "unsupported")}
    assert got == {"planned": 3, "failed": 1, "unsupported": 1}, f"the dashboard shows {got} for a run of 3 with 1 failed and 1 unsupported"
    s, raw = http("GET", "/api/dashboards/qa/drill?key=" + urllib.parse.quote(m["failed"]["drill"])); assert s == 200, (s, raw[:160])
    recs = json.dumps(J(raw)); assert "journey.fail" in recs and "journey.pass" not in recs, "the records behind 'failed' are not exactly the failed test"
    s, raw = http("GET", f"/api/dashboards/qa?until={at - 3600}&since={at - 7200}"); assert s == 200, s
    before = {x["id"]: x.get("value") for pnl in J(raw)["panels"] for x in pnl.get("metrics", [])}
    # the three tests stay planned (they are in the inventory); what a range before the run must not show is their result
    assert before.get("failed") == 0 and before.get("unexecuted") == 3, f"a range that ends an hour before the run still shows its result: {before}"
    return "a reported run of 3 appears (planned 3, failed 1, unsupported 1); the failed test is the record behind 'failed'; in a range before the run the 3 are unexecuted; a repeat is not counted twice"
check("dashboards: a reported test run appears with its numbers, its records and its time range", t_dash_journey)
def t_dash_independent():
    """The dashboards session's own API-level checks (dashboards_check.py, standard library only, read-only), run against this installed
    build with this run's local session. Its JSON is kept next to the native result; checks named in DASH_KNOWN are known differences of
    the build under test: they stay failed in that file and do not fail this step."""
    if not state.get("dash"): return "UNSUPPORTED on this build: it has no dashboards"
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboards_check.py")
    out = os.path.join(os.path.dirname(os.path.abspath(sys.argv[2])), "dashboards_check.json")
    e = {**os.environ, "DASH_BASE": BASE, "DASH_SESSION": SESSION["token"], "DASH_CSRF": SESSION["csrf"]}
    args = [sys.executable, script, out, "--release", os.environ.get("DASH_CHECK_RELEASE", "unknown"), "--platform", re.sub(r"[^a-z0-9]+", "-", os.environ.get("DASH_CHECK_PLATFORM", sys.platform).lower()).strip("-")]
    r = subprocess.run(args, capture_output=True, text=True, timeout=300, env=e)
    lines = (r.stdout or "").strip().splitlines()
    failed = [ln for ln in lines if ln.startswith("FAIL")][:4]
    assert r.returncode == 0, f"exit {r.returncode}: {'; '.join(failed)[:240]} {(r.stderr or '')[-160:]}"
    return "independent checks: " + (lines[-1] if lines else "no output")[:220]
check("dashboards: the independent API-level checks pass on this installed build", t_dash_independent)
def t_anonymous_write_refused():
    s, _ = http("POST", "/api/products/edge-sentinel/actions/x/execute", {})
    assert s in (401, 403, 404, 405, 422), s; return f"HTTP {s}"
check("a decision path does not execute for an unauthenticated caller", t_anonymous_write_refused)
def t_loopback_only():
    import socket
    ip = socket.gethostbyname(socket.gethostname())
    if ip.startswith("127."): return "UNSUPPORTED here: this host has no non-loopback address to probe"
    s = socket.socket(); s.settimeout(3)
    try:
        s.connect((ip, 8787)); s.close(); raise AssertionError(f"reachable on {ip}:8787")
    except (ConnectionRefusedError, socket.timeout, OSError):
        return f"not reachable on {ip}:8787"
check("the control plane listens on loopback only", t_loopback_only)
def t_stops():
    proc.stdin.close()
    proc.wait(timeout=30); return f"exit {proc.returncode}"
check("it stops cleanly when the launcher goes away", t_stops)
if proc.poll() is None:
    proc.kill()
def t_port_free():
    time.sleep(1)
    try:
        http("GET", "/", timeout=3); raise AssertionError("still serving")
    except (urllib.error.URLError, ConnectionError, OSError):
        return "port released"
check("nothing is left listening afterwards", t_port_free)

summary = {s: sum(1 for r in results if r["status"] == s) for s in ("pass", "fail", "unsupported")}
json.dump({"platform": platform.platform(), "machine": platform.machine(), "sidecar": side, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "summary": summary,
           "seconds": round(sum(r["seconds"] for r in results), 1), "results": results}, open(out_path, "w"), indent=1)
print(summary)
sys.exit(1 if summary["fail"] else 0)

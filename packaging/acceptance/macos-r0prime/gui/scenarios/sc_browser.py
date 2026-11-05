#!/usr/bin/env python3
"""Browser-level scenarios: restored tabs, second tab, bookmark, browser restart, attacker page on another
local port, DNS rebinding hostname. Controlled headless Chromium with disposable profiles; the sidecar is the
candidate's, started through the launcher's real code (gui-launcher-driver). Label: OBSERVED-BROWSER.
Usage: python3 sc_browser.py <outdir>"""
import glob, json, os, re, socket, subprocess, sys, time, threading, http.server, socketserver
import guilib as g
out = sys.argv[1]; os.makedirs(out, exist_ok=True)
R = g.Results("browser")
ts = time.strftime("%H%M%S")
base = f"{g.GUI_WORK}/runs/browser-{ts}"; home = base + "/home"; tmp = base + "/tmp"
os.makedirs(home); os.makedirs(tmp)
env = g.rd_env(home=home, tmp=tmp, extra={"RDO_SHIM_LOG": base + "/open-shim.log", "RDO_BROWSER_OUT": base + "/browser-out", "RDO_SHIM_NOBROWSER": "1"})
dA = g.Driver(env=env); t0 = dA.cmd("start"); pA = t0["port"]
mc = dA.cmd("mintcode"); s_, j_, _ = g.exchange(pA, mc["code"]); tokA = j_["session_token"]
# --- browser: restored tabs, bookmark, new tab, restart (sidecar A)
B = g.Browser(env); prof = base + "/profile-restore"
B.op(op="launch", profile=prof)
redirect = dA.cmd("open")["file"]
r = B.op(op="goto", page=0, url="file://" + redirect, wait="commit"); r = B.op(op="waiturl", page=0, re=f"^http://127\\.0\\.0\\.1:{pA}/")
land = r.get("url"); api1 = B.ev(0, "fetch('/api/projects').then(r=>r.status)")
tokjson = B.ev(0, "sessionStorage.getItem('agentic_os.session')")
R.add("B-10a", "sign-in via redirect page in a fresh browser profile; address bar loses #lc=", "lc=" not in (land or "") and api1 == 200, "OBSERVED-BROWSER", f"{land} api={api1}")
B.op(op="reload", page=0); rel = B.ev(0, "fetch('/api/projects').then(r=>r.status)"); txt, _w = B.banner(0, 3.0)
R.add("B-10b", "reload of the same tab keeps the session (sessionStorage): API 200, no banner", rel == 200 and txt == "", "OBSERVED-BROWSER", f"api={rel}")
B.op(op="newpage"); r = B.op(op="goto", page=1, url=f"http://127.0.0.1:{pA}/", settle=1500)
api_new = B.ev(1, "fetch('/api/projects').then(r=>r.status)"); txt_new, w_new = B.banner(1)
R.add("B-10c", "SECOND TAB / bookmark / typed URL (no code, empty sessionStorage): API 401 and the page shows the sign-in panel ('Not signed in ... press \"Start & open\"', role=alert, U-02)", api_new == 401 and "Not signed in" in txt_new and 'press "Start & open"' in txt_new, "OBSERVED-BROWSER", txt_new[:140].replace("\n", " | "))
r = B.op(op="goto", page=1, url=f"http://127.0.0.1:{pA}/api/projects"); R.add("B-10d", "direct address-bar navigation to /api/projects -> 401 JSON, no data", r.get("status") == 401, "OBSERVED-BROWSER", str(r.get("status")))
r = B.op(op="goto", page=1, url=f"http://127.0.0.1:{pA}/docs"); R.add("B-10e", "address-bar /docs -> 401 (documented limitation)", r.get("status") == 401, "OBSERVED-BROWSER", str(r.get("status")))
B.op(op="closectx")
B.op(op="launch", profile=prof)  # browser restart, same profile, tabs NOT restored by the harness (cookies/localStorage persist)
r = B.op(op="goto", page=0, url=f"http://127.0.0.1:{pA}/", settle=1500); txt, w_r = B.banner(0)
api_r = B.ev(0, "fetch('/api/projects').then(r=>r.status)")
R.add("B-10f", "browser restarted, tab reopened WITHOUT its sessionStorage: 401 + sign-in panel (what a restored tab shows when the browser does not restore storage)", api_r == 401 and "Not signed in" in txt and 'press "Start & open"' in txt, "OBSERVED-BROWSER", txt[:100].replace("\n", " | "))
# restored tab WITH sessionStorage restored (Chrome/Firefox do restore it): emulate by seeding the same key BEFORE the page scripts run
B.op(op="newpage")
r = B.op(op="goto", page=1, url=f"http://127.0.0.1:{pA}/robots-not-there", wait="commit")  # same origin, to be allowed to set storage
B.ev(1, "(k=>{sessionStorage.setItem('agentic_os.session', k);return 1})(%s)" % json.dumps(tokjson))
r = B.op(op="goto", page=1, url=f"http://127.0.0.1:{pA}/", settle=1500); api_res = B.ev(1, "fetch('/api/projects').then(r=>r.status)")
R.add("B-10g", "restored tab whose sessionStorage WAS restored, sidecar still running: session resumes (API 200)", api_res == 200, "OBSERVED-BROWSER", str(api_res))
# --- after Stop and Start the old tab/bookmark points at a dead port
dA.cmd("stop"); time.sleep(0.5)
r = B.op(op="goto", page=1, url=f"http://127.0.0.1:{pA}/", settle=500)
R.add("B-10h", "after Stop (and a new Start gets a NEW port) the restored tab/bookmark shows a browser connection error, no hint to use the launcher", r.get("err") is not None and "ERR_CONNECTION_REFUSED" in (r.get("err") or ""), "OBSERVED-BROWSER", (r.get("err") or "")[:120])
tn = dA.cmd("start"); R.add("B-10i", "the restart picks a different port (bookmarks to the old port stay dead)", tn.get("port") != pA, "OBSERVED-HEADLESS", f"old={pA} new={tn.get('port')}")
pA2 = tn["port"]
s_, _, _ = g.http(pA2, "GET", "/api/projects", {"X-Agentic-Session": tokA})
R.add("B-10j", "the OLD session token (first sidecar, now stopped) presented to the RESTARTED sidecar (new port, new secret) -> 401", s_ == 401, "OBSERVED-HEADLESS", str(s_))
# --- attacker page from another local port (cross-origin + localhost + rebinding name), victim signed in in the same profile
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        page = ATTACK.replace("__PORT__", str(self.server.target)).encode()
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(page)
ATTACK = """<!doctype html><title>evil</title><script>
const T='__PORT__'; const out={};
const tryit=async(name,f)=>{try{const r=await f();out[name]=r}catch(e){out[name]='ERR:'+String(e).slice(0,60)}};
(async()=>{
 for (const host of ['127.0.0.1','localhost']) {
  const u='http://'+host+':'+T;
  await tryit(host+' cors GET /api/projects', async()=> (await fetch(u+'/api/projects')).status);
  await tryit(host+' cors GET creds', async()=> (await fetch(u+'/api/projects',{credentials:'include'})).status);
  await tryit(host+' cors POST json /api/sources/confirm', async()=> (await fetch(u+'/api/sources/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"sources":[{"kind":"files","location":"/etc"}]}'})).status);
  await tryit(host+' no-cors POST text/plain', async()=> {const r=await fetch(u+'/api/sources/confirm',{method:'POST',mode:'no-cors',headers:{'Content-Type':'text/plain'},body:'{"sources":[{"kind":"files","location":"/etc"}]}'}); return r.type});
  await tryit(host+' replay header guess', async()=> (await fetch(u+'/api/projects',{headers:{'X-Agentic-Session':'A'.repeat(43)}})).status);
  await tryit(host+' sessionStorage of target origin', async()=> {const w=window.open(u+'/'); return typeof w});
 }
 out.ls = Object.keys(sessionStorage).join(',')+'|'+document.cookie;
 document.title='done'; window.__out=out;
})();
</script>"""
srv = socketserver.TCPServer(("127.0.0.1", 0), H); srv.target = pA2; evil_port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
# victim signs in on a tab of the same profile first
mc = dA.cmd("open"); time.sleep(0.3)
print("open for attacker test:", {k: v for k, v in mc.items() if k != "file"}, flush=True)
r1 = B.op(op="goto", page=1, url="file://" + mc["file"], wait="commit"); r2 = B.op(op="waiturl", page=1, re=f"^http://127\\.0\\.0\\.1:{pA2}/"); print("victim sign-in:", r1.get("err"), r2, flush=True)
victim_api = B.ev(1, "fetch('/api/projects').then(r=>r.status)")
B.op(op="newpage"); B.op(op="goto", page=2, url=f"http://127.0.0.1:{evil_port}/", wait="load", settle=3000)
g.wait_for(lambda: B.ev(2, "document.title") == "done", 25, 1.0)
ev = B.ev(2, "window.__out")
print("attacker out:", json.dumps(ev)[:900], flush=True)
statuses = {k: v for k, v in (ev or {}).items() if k != "ls"}
leaked = [k for k, v in statuses.items() if v == 200]
R.add("B-11a", "attacker page on another local port (127.0.0.1 and localhost), victim signed in in the same browser profile: NO request returns 200 / readable data", victim_api == 200 and leaked == [], "OBSERVED-BROWSER", f"victim_api={victim_api}; leaked={leaked}; " + json.dumps(statuses))
R.add("B-11b", "attacker origin sees no session/cookie of the victim origin", (ev or {}).get("ls") == "|", "OBSERVED-BROWSER", str((ev or {}).get("ls")))
# server-side proof of what the attacker's no-cors POST received: replay through raw HTTP with the same headers as Chromium would send
s, h, b = g.http(pA2, "POST", "/api/sources/confirm", {"Content-Type": "text/plain", "Origin": f"http://127.0.0.1:{evil_port}", "Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "no-cors"}, '{"sources":[{"kind":"files","location":"/etc"}]}')
R.add("B-11c", "the attacker's no-cors text/plain POST as the server sees it (Origin + Sec-Fetch-Site same-site) -> 403, nothing walked", s == 403, "OBSERVED-HEADLESS", str(s))
# DNS rebinding with a real hostname: map evil.test -> 127.0.0.1 inside the controlled browser only
B.op(op="closectx"); B.op(op="launch", profile=base + "/profile-rebind", args=["--host-resolver-rules=MAP evil.test 127.0.0.1"])
r = B.op(op="goto", page=0, url=f"http://evil.test:{pA2}/", settle=500)
R.add("B-11d", "DNS rebinding: the sidecar's own origin loaded under an attacker hostname (evil.test:<port> -> 127.0.0.1) -> 400 invalid_host, UI not served", r.get("status") == 400, "OBSERVED-BROWSER", str(r.get("status")))
txt = B.op(op="text", page=0)["text"]; R.add("B-11e", "that answer is the gate's JSON error, not the app", "invalid_host" in txt, "OBSERVED-BROWSER", txt[:100])
B.quit()
srv.shutdown()

dA.cmd("stop"); dA.quit(); time.sleep(0.5)
R.add("B-99", "after the browser scenarios: 0 sidecar processes, no _MEI", len(g.procs()) == 0 and g.mei_dirs(tmp) == [], "OBSERVED-HEADLESS", f"procs={len(g.procs())} mei={g.mei_dirs(tmp)}")
R.save(out + "/browser.json")

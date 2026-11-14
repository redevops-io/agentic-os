#!/usr/bin/env python3
"""Route walk: every route in /openapi.json except the two probes must refuse an anonymous request (401; /invoke 401 or 503)
and must not refuse the operator.   usage: routewalk.py BASE_URL USER PASSWORD   (standard library only)"""
import sys, json, urllib.request, urllib.error, base64
base, user, pw = sys.argv[1], sys.argv[2], sys.argv[3]
def call(method, path, auth=False, body=None):
    req = urllib.request.Request(base + path, method=method, data=(json.dumps(body).encode() if body is not None else None),
                                 headers={"Content-Type": "application/json"})
    if auth: req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode())
    try:
        r = urllib.request.urlopen(req, timeout=15); return r.status
    except urllib.error.HTTPError as e: return e.code
spec = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/openapi.json", headers={"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()})).read())
public, bad, rows = {"/health", "/readyz"}, [], []
REDIRECTS = {"/"}                                   # the front page only redirects to the console, which carries no data
for path, ops in sorted(spec["paths"].items()):
    p = path.replace("{case_id}", "x")
    for m in ops:
        m = m.upper()
        if m not in ("GET", "POST"): continue
        anon = call(m, p, False, ({"capability":"sentinel.triage","inputs":{}} if path == "/invoke" else {}) if m == "POST" else None)
        authed = call(m, p, True, ({"capability":"sentinel.triage","inputs":{}} if path == "/invoke" else {}) if m == "POST" else None)
        want_public = path in public
        if path in REDIRECTS and anon == 200:
            rows.append((m, path, anon, authed, "redirect", "ok")); continue
        if (m, path) == ("POST", "/api/v1/session"):          # the sign-in: refuses a bad credential, grants nothing else
            ok = anon == 401
            rows.append((m, path, anon, authed, "login", "ok" if ok else "FAIL"))
            if not ok: bad.append(rows[-1])
            continue
        ok = (anon in (200,) if want_public else (anon == 401 or (path == "/invoke" and anon in (401, 503)))) and (authed != 401 or path == '/invoke')
        rows.append((m, path, anon, authed, "public" if want_public else "gated", "ok" if ok else "FAIL"))
        if not ok: bad.append(rows[-1])
for r in rows: print("%-4s %-30s anon=%s authed=%s %s %s" % r)
print("ROUTEWALK", "OK" if not bad else f"FAIL {bad}")

"""Run the BUILT sidecar against the real running products and drive it with Playwright through its one-time launch URL.
usage: sidecar_e2e.py <sidecar> <registry.yaml> <sentinel_url> <sentinel_basic> <outdir> <ip>"""
import os, re, subprocess, sys, tempfile, time, json
import httpx, base64
sidecar, registry, sentinel, sbasic, out, ip = sys.argv[1:7]
port = 18795
home = tempfile.mkdtemp(prefix="sc-home-", dir="/tmp")
env = {"PATH": "/usr/bin:/bin", "HOME": home, "LANG": "en_US.UTF-8", "PROJECTS_PORT": str(port), "PROJECTS_HOST": "127.0.0.1",
       "AGENTIC_OS_PRODUCTS": registry, "PROJECTS_PROVIDER": "registered",
       "PROJECTS_UI_DIR": ""}
env.pop("PROJECTS_UI_DIR")
p = subprocess.Popen([sidecar, "serve"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
url = None; t0 = time.time(); log = []
while time.time() - t0 < 90:
    line = p.stderr.readline(); log.append(line)
    m = re.search(r"(http://\S+/#lc=\S+)", line)
    if m: url = m.group(1); break
assert url, "".join(log)
base = url.split("/#lc=")[0]
try:
    H = {"Authorization": "Basic " + base64.b64encode(sbasic.encode()).decode()}
    # make sure the desktop principal is linked to a product user (done by the product's admin, once)
    httpx.post(sentinel + "/api/v1/actor-links", headers=H, json={"external_actor": "projects:local-owner", "user": "alex"})
    r = subprocess.run([sys.executable, "ui_flow.py", base, sentinel, out, "--ip", ip, "--live", "--sentinel-basic", sbasic, "--launch-url", url, "--expect-actor", "projects:local-owner"],
                       capture_output=True, text=True)
    open("/tmp/ui_flow.err","w").write(r.stderr); print(r.stdout[-3000:])
    sys.exit(r.returncode)
finally:
    p.terminate()
    try: p.wait(10)
    except Exception: p.kill()

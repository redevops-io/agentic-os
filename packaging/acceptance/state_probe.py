"""Start an installed sidecar once with a FIXED per-user home, stop it, and list the state it left there.

    python state_probe.py <path to redevops-sidecar> <home directory> <out.json> [--expect-kept before.json]

Used around an upgrade: run it with the old build, install the new build over it, run it again with `--expect-kept`. The second run fails
if a file the old build left is gone, and reports what the new build added (from launcher 0.1.2: the authority and replay stores).
"""
import json, os, socket, subprocess, sys, tempfile, time

side, home, out = sys.argv[1], os.path.abspath(sys.argv[2]), sys.argv[3]
before = json.load(open(sys.argv[5])) if len(sys.argv) > 5 and sys.argv[4] == "--expect-kept" else None
os.makedirs(home, exist_ok=True)
with socket.socket() as s:
    s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
env = {"PATH": os.environ.get("PATH", ""), "HOME": home, "USERPROFILE": home, "APPDATA": os.path.join(home, "AppData"), "LOCALAPPDATA": os.path.join(home, "Local"),
       "XDG_STATE_HOME": os.path.join(home, ".local", "state"), "XDG_DATA_HOME": os.path.join(home, ".local", "share"), "TMPDIR": os.environ.get("TMPDIR", tempfile.gettempdir()),
       "TEMP": os.environ.get("TEMP", tempfile.gettempdir()), "TMP": os.environ.get("TMP", tempfile.gettempdir()), "LANG": "C.UTF-8", "PROJECTS_PORT": str(port)}
if os.environ.get("SYSTEMROOT"):
    env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
log = open(out + ".log", "wb")
p = subprocess.Popen([side, "serve", "--exit-on-stdin-eof"], stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, env=env)
up = False
for _ in range(120):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            up = True; break
    except OSError:
        if p.poll() is not None:
            break
        time.sleep(1)
time.sleep(3)
try:
    p.stdin.close(); p.wait(timeout=30)
except Exception:  # noqa: BLE001
    p.kill()
log.close()
files = sorted(os.path.relpath(os.path.join(d, f), home).replace("\\", "/") for d, _, fs in os.walk(home) for f in fs)
text = open(out + ".log", "rb").read().decode("utf-8", "replace")
posture = next((l.strip() for l in text.splitlines() if l.startswith("redevops-sidecar: profile=")), "")
res = {"served": up, "files": files, "posture": posture}
if before is not None:
    res["lost"] = [f for f in before["files"] if f not in files and not f.endswith(("-wal", "-shm"))]
    res["added"] = [f for f in files if f not in before["files"]]
json.dump(res, open(out, "w"), indent=1)
print(json.dumps({k: (v if k != "files" else len(v)) for k, v in res.items()}))
sys.exit(0 if up and not res.get("lost") else 1)

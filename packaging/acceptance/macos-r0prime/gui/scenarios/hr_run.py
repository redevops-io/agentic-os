#!/usr/bin/env python3
"""Run a sidecar binary the way the launcher does (hand-off on stdout, port 0, stdin tether) and report whether
`serve` comes up. Usage: hr_run.py <sidecar-binary> [label] [extra NAME=value ...]. Prints one JSON line."""
import json, os, signal, subprocess, sys, time
import guilib as g
binary = sys.argv[1]; label = sys.argv[2] if len(sys.argv) > 2 else binary
extra = dict(a.split("=", 1) for a in sys.argv[3:])
home = f"{g.GUI_WORK}/runs/hr-home"; tmp = f"{g.GUI_WORK}/runs/hr-tmp-{int(time.time()*1000)}"; os.makedirs(tmp)
env = g.rd_env(home=home, tmp=tmp); env.update({"PROJECTS_PORT": "0", "PROJECTS_HOST": "127.0.0.1", "PROJECTS_BASE_PATH": "", "AGENTIC_OS_HANDOFF": "stdout"}); env.update(extra)
t0 = time.time()
p = subprocess.Popen([binary, "serve", "--exit-on-stdin-eof"], env=env, cwd="/", stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
import threading
box = {}
def rd(): box["line"] = p.stdout.readline()
th = threading.Thread(target=rd, daemon=True); th.start(); th.join(40)
res = {"label": label, "binary": binary.replace(g.S, "$S")}
line = box.get("line", b"")
if line and b'"event":"ready"' in line:
    port = json.loads(line)["port"]; res["started"] = True; res["ms_to_handoff"] = round((time.time() - t0) * 1000)
    res["root_status"] = g.http(port, "GET", "/")[0]
else:
    res["started"] = False
    time.sleep(0.5)
    try: err = p.stderr.read1(3000) if p.poll() is not None else b""
    except Exception: err = b""
    res["exit_code"] = p.poll(); res["stdout"] = line.decode(errors="replace")[:200]; res["stderr_tail"] = err.decode(errors="replace")[-700:]
try: p.stdin.close()
except Exception: pass
try: p.wait(timeout=10)
except Exception:
    try: os.killpg(p.pid, signal.SIGKILL)
    except Exception: pass
res["leftover_mei"] = g.mei_dirs(tmp)
print(json.dumps(res))

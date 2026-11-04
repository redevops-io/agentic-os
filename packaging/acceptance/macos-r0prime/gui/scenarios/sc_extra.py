#!/usr/bin/env python3
"""Small extra checks: (1) a launcher SIGKILL leaves the redirect file (its exit hook never runs) - what remains and how long;
(2) what the onefile sidecar extracts (Mach-O count) and the modes of the extraction dir; (3) first-run vs later extraction size.
Usage: python3 sc_extra.py <outdir>"""
import glob, json, os, signal, stat, subprocess, sys, time
import guilib as g
out = sys.argv[1]; os.makedirs(out, exist_ok=True)
R = g.Results("extra"); base = f"{g.GUI_WORK}/runs/extra-{time.strftime('%H%M%S')}"; home = base + "/home"; tmp = base + "/tmp"; os.makedirs(home); os.makedirs(tmp)
env = g.rd_env(home=home, tmp=tmp, extra={"RDO_SHIM_LOG": base + "/shim.log", "RDO_SHIM_NOBROWSER": "1"})
d = g.Driver(env=env); t = d.cmd("start"); o = d.cmd("open"); f = o["file"]
mei = glob.glob(tmp + "/_MEI*")[0]
# (2) extraction contents while running
macho = []
for root, ds, fs in os.walk(mei):
    for n in fs:
        p = os.path.join(root, n)
        if subprocess.run(["/usr/bin/file", "-b", p], capture_output=True, text=True).stdout.startswith("Mach-O"): macho.append(os.path.relpath(p, mei))
size = int(subprocess.run(["/usr/bin/du", "-sk", mei], capture_output=True, text=True).stdout.split()[0])
R.add("X-01", "onefile extraction: Mach-O files in _MEI, size, mode", "INFO", "OBSERVED-HEADLESS", f"{len(macho)} Mach-O files {sorted(macho)}; {size//1024} MB; _MEI mode {oct(stat.S_IMODE(os.stat(mei).st_mode))}")
code_file_before = open(f).read()
d.p.send_signal(signal.SIGKILL); d.p.wait(); g.wait_for(lambda: not g.procs(), 10)
R.add("X-02", "launcher SIGKILL with an open redirect file: the sidecar is gone, the redirect file is NOT removed (no exit hook) but holds a code that is dead (no sidecar)", os.path.exists(f) and not g.procs() and g.http(t["port"], "GET", "/")[0] == -1, "OBSERVED-HEADLESS", f"file kept={os.path.exists(f)} mode={oct(stat.S_IMODE(os.stat(f).st_mode)) if os.path.exists(f) else None}")
# next launcher: file older than 120 s would be swept; one younger is left until then
d2 = g.Driver(env=env); d2.cmd("start"); o2 = d2.cmd("open")
R.add("X-03", "next launcher start+open: the leftover younger than 120 s is NOT swept yet (documented: swept at the next write once older than 120 s)", os.path.exists(f), "OBSERVED-HEADLESS", "")
d2.cmd("stop"); d2.quit()
R.save(out + "/extra.json")

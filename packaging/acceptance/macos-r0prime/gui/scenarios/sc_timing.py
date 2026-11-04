#!/usr/bin/env python3
"""Start-to-verified-handshake and start-to-first-200 distributions. The measured path is the launcher's real
start code (spawn, hand-off read, prove_sidecar HMAC handshake with its retry/deadline logic) against the built
sidecar of the candidate, run by gui-launcher-driver `timed_start`. Writes raw CSV + a markdown summary.
Usage: python3 sc_timing.py <outdir> [--n-cold 40 --n-warm 40 --n-load 20 --n-flood 40 --n-both 10 --n-real 20 --n-first 5]"""
import argparse, csv, json, os, shutil, signal, statistics, subprocess, sys, time
import guilib as g

ap = argparse.ArgumentParser(); ap.add_argument("out")
for k, v in (("n-cold", 40), ("n-warm", 40), ("n-load", 20), ("n-flood", 40), ("n-both", 10), ("n-real", 20), ("n-first", 5)): ap.add_argument("--" + k, type=int, default=v)
a = ap.parse_args(); os.makedirs(a.out, exist_ok=True)
ncpu = os.cpu_count(); nload = ncpu // 2
base = f"{g.GUI_WORK}/runs/timing-{time.strftime('%H%M%S')}"; os.makedirs(base)
home = base + "/home"; os.makedirs(home)
rows = []

def drv(tmp):
    env = g.rd_env(home=home, tmp=tmp); return g.Driver(env=env)

def record(cond, i, r, extra=None):
    row = dict(condition=cond, iter=i, ok=r.get("ok"), verdict=r.get("verdict", r.get("error", ""))[:120], t_spawn_ms=r.get("t_spawn_ms"), t_extract_start_ms=r.get("t_mei_ms"),
               t_child_ms=r.get("t_child_ms"), t_handoff_ms=r.get("t_handoff_ms"), t_verified_ms=r.get("t_verified_ms") if "t_verified_ms" in r else r.get("ms"),
               verify_phase_ms=r.get("verify_phase_ms"), t_first200_ms=r.get("t_first200_ms"), api_status=r.get("api_status"), stop=r.get("stop"), load1=round(os.getloadavg()[0], 1))
    if extra: row.update(extra)
    rows.append(row); return row

def timed(cond, n, tmp_fn, flood=0, label_extra=None):
    d = drv(base + "/tmp-" + cond + "-drv"); os.makedirs(base + "/tmp-" + cond + "-drv", exist_ok=True)
    for i in range(n):
        tmp = tmp_fn(i); os.makedirs(tmp, exist_ok=True)
        r = d.cmd(f"timed_start {tmp} {flood}")
        row = record(cond, i, r)
        left = g.mei_dirs(tmp)
        if left: row["leftover_mei"] = ",".join(left)
        if not r.get("ok") or (r.get("t_verified_ms") or 0) > 10000:
            print("OUTLIER/FAILURE", cond, i, json.dumps(r)[:400], flush=True)
        if flood: g.drain_ports(3000, 60)
    d.quit()

# ---- first start after copying the app (fresh copy each time; page cache cannot be flushed without sudo)
for k in range(a.n_first):
    cp = f"{base}/copy{k}/Agentic Apps.app"; os.makedirs(os.path.dirname(cp)); subprocess.run(["/usr/bin/ditto", g.APP, cp], check=True)
    env = g.rd_env(home=home, tmp=f"{base}/tmp-first{k}"); env["RDO_SIDECAR"] = cp + "/Contents/MacOS/redevops-sidecar"; os.makedirs(env["TMPDIR"], exist_ok=True)
    d = g.Driver(env=env); r = d.cmd(f"timed_start {env['TMPDIR']} 0"); record("first-after-copy", k, r); d.quit()
# ---- real start_sidecar path (the exact function the Start button calls), for cross-checking the stepwise numbers
d = drv(base + "/tmp-real"); os.makedirs(base + "/tmp-real", exist_ok=True)
for i in range(a.n_real):
    tmp = f"{base}/tmp-real-{i}"; os.makedirs(tmp)
    r = d.cmd(f"start {tmp}"); record("real-start_sidecar", i, {"ok": r.get("ok"), "ms": r.get("ms"), "error": r.get("error", "")}); d.cmd("stop")
d.quit()
# ---- cold: a fresh, empty TMPDIR for every start
timed("cold", a.n_cold, lambda i: f"{base}/cold/t{i}")
# ---- warm: one TMPDIR reused
timed("warm", a.n_warm, lambda i: f"{base}/warm")
# ---- CPU load: ncpu/2 busy loops started (and killed) by this script, by exact PID
yes = []
def load_on():
    for _ in range(nload): yes.append(subprocess.Popen(["/usr/bin/yes"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL))
def load_off():
    for p in yes:
        try: p.kill()
        except Exception: pass
    for p in yes:
        try: p.wait(timeout=5)
        except Exception: pass
    yes.clear()
try:
    load_on(); time.sleep(3)
    timed("cpu-load", a.n_load, lambda i: f"{base}/load/t{i}")
    timed("cpu-load+flood8", a.n_both, lambda i: f"{base}/both/t{i}", flood=8)
finally:
    load_off()
timed("flood8", a.n_flood, lambda i: f"{base}/flood/t{i}", flood=8)

# ---- output
fields = sorted({k for r in rows for k in r}, key=lambda k: list(rows[0]).index(k) if k in rows[0] else 99)
with open(a.out + "/timings.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)
def stats(vals):
    v = [x for x in vals if isinstance(x, (int, float)) and x >= 0]
    if not v: return None
    return dict(n=len(v), p50=g.pct(v, 50), p90=g.pct(v, 90), p95=g.pct(v, 95), p99=g.pct(v, 99), max=max(v), min=min(v), mean=statistics.mean(v))
md = ["| condition | n | ok | metric | min | p50 | p90 | p95 | p99 | max |", "|---|---|---|---|---|---|---|---|---|---|"]
summ = {}
for cond in dict.fromkeys(r["condition"] for r in rows):
    rs = [r for r in rows if r["condition"] == cond]; okn = sum(1 for r in rs if r["ok"])
    for metric in ("t_extract_start_ms", "t_child_ms", "t_handoff_ms", "t_verified_ms", "verify_phase_ms", "t_first200_ms"):
        s = stats([r.get(metric) for r in rs])
        if s: md.append(f"| {cond} | {len(rs)} | {okn} | {metric} | {s['min']:.0f} | {s['p50']:.0f} | {s['p90']:.0f} | {s['p95']:.0f} | {s['p99']:.0f} | {s['max']:.0f} |"); summ[f"{cond}:{metric}"] = s
over10 = [r for r in rows if isinstance(r.get("t_verified_ms"), (int, float)) and r["t_verified_ms"] > 10000]
fails = [r for r in rows if not r["ok"]]
md += ["", f"starts total={len(rows)}; failures={len(fails)}; over 10 s={len(over10)}; leftover _MEI in any per-run TMPDIR: {sum(1 for r in rows if r.get('leftover_mei'))}", f"cpus={ncpu}, load generators={nload} (`yes`, killed by exact PID)"]
open(a.out + "/timings-summary.md", "w").write("\n".join(md) + "\n"); json.dump(summ, open(a.out + "/timings-summary.json", "w"), indent=1)
print("\n".join(md))

"""TEMPORARY diagnostic (not for merge): replays tests/test_mission_fork_join_hardening.py::test_second_process_is_refused_and_each_branch_dispatches_once
N times and, on a mismatch, prints what happened: the raw bytes of the shared effects file, one marker file per branch (did invoke() run?), the
BranchStarted/BranchSucceeded events and the outputs of both processes."""
import os, subprocess, sys, tempfile, textwrap, time, json, pathlib
ROOT = str(pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
from tests._fork_support import build
from agentic_os.mission.store import EventStore

PROC = textwrap.dedent("""
    import sys, time, os
    from tests._fork_support import make_rt, spec, GRANTS
    from agentic_os.mission.store import EventStore
    from agentic_os.mission.fork_join import ForkError
    path, mid, role, effects = sys.argv[1:5]
    class Op:
        def invoke(self, operator, capability, inputs, key, *, secrets=None):
            if not capability.startswith("w."):
                return {"ok": True}
            time.sleep(1.5)
            t = time.time()
            fd = os.open(effects, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            os.write(fd, (inputs["tag"] + "\\n").encode()); os.close(fd)
            open(effects + "." + inputs["tag"], "w").write("%r %r" % (t, os.getpid()))
            return {"ok": True}
    rt = make_rt(EventStore(path), Op())
    rt.rehydrate(mid, policy_refs=GRANTS)
    if role == "B":
        for _ in range(400):
            if "ForkDriverClaimed" in open(path).read():
                break
            time.sleep(0.05)
    try:
        r = rt.forks.run_fork(mid, spec(n=3))
        print("DONE", r.status)
    except ForkError as e:
        print("REFUSED", str(e)[:60])
""")

def one(i):
    d = tempfile.mkdtemp(prefix="fj-")
    path, effects = os.path.join(d, "log.jsonl"), os.path.join(d, "effects.txt")
    rt, _, m = build(store=EventStore(path))
    env = {**os.environ, "PYTHONPATH": ROOT}
    run = lambda role: subprocess.Popen([sys.executable, "-c", PROC, path, m.id, role, effects], cwd=ROOT, env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    a = run("A"); time.sleep(0.2); b = run("B")
    ao, ae = a.communicate(timeout=120); bo, be = b.communicate(timeout=120)
    got = sorted(open(effects).read().split()) if os.path.exists(effects) else None
    ok = "DONE satisfied" in ao and "REFUSED" in bo and got == ["b0", "b1", "b2"]
    if not ok:
        print(f"--- iteration {i} MISMATCH: effects={got!r} raw={open(effects,'rb').read()!r}")
        print("A:", ao.strip(), ae[-300:], "| B:", bo.strip(), be[-300:])
        for t in ("b0", "b1", "b2"):
            f = effects + "." + t
            print("  invoke marker", t, open(f).read() if os.path.exists(f) else "MISSING (invoke did not complete)")
        for e in EventStore(path).for_mission(m.id):
            if e.type.startswith(("Branch", "Fork", "Join")):
                print("  ", e.seq, e.type, e.payload.get("branch_id") or e.payload.get("node_id") or "", round(e.ts, 4))
    return ok

if __name__ == "__main__":
    n = int(sys.argv[1]); bad = 0
    for i in range(n):
        if not one(i):
            bad += 1
    print(f"RESULT {n - bad}/{n} passed")
    sys.exit(1 if bad else 0)

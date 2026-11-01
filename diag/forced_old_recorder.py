"""TEMPORARY diagnostic: the regression test's forced interleaving (barrier: three branches leave the call together) but with the OLD shared-append
recorder. Prints, per run, how many branches started/succeeded and how many lines the shared file ended up with."""
import os, sys, tempfile, threading, pathlib
ROOT = str(pathlib.Path(__file__).resolve().parents[1]); sys.path.insert(0, ROOT)
from tests._fork_support import build, spec
from agentic_os.mission.store import EventStore
runs, short = int(sys.argv[1]), 0
for i in range(runs):
    d = tempfile.mkdtemp(); eff = os.path.join(d, "effects.txt"); gate = threading.Barrier(3)
    class Op:
        def invoke(self, operator, capability, inputs, key, *, secrets=None):
            if not capability.startswith("w."):
                return {"ok": True}
            gate.wait(20)
            fd = os.open(eff, os.O_WRONLY | os.O_APPEND | os.O_CREAT); os.write(fd, (inputs["tag"] + "\n").encode()); os.close(fd)
            return {"ok": True}
    rt, _, m = build(store=EventStore(os.path.join(d, "log.jsonl")), op=Op())
    res = rt.forks.run_fork(m.id, spec(n=3))
    evs = rt.store.for_mission(m.id)
    st = sum(e.type == "BranchStarted" for e in evs); ok = sum(e.type == "BranchSucceeded" for e in evs)
    lines = open(eff).read().split()
    if lines != ["b0", "b1", "b2"] and sorted(lines) != ["b0", "b1", "b2"]:
        short += 1
        print(f"run {i}: status={res.status} started={st} succeeded={ok} lines={lines}")
print(f"OLD RECORDER, FORCED INTERLEAVING: {short}/{runs} runs had fewer effect lines than branches that started and succeeded")

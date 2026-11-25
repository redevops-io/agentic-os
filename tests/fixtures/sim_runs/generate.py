"""Regenerate these fixtures with agent-harness on the path:  PYTHONPATH=<agent-harness checkout> python tests/fixtures/sim_runs/generate.py

SYNTHETIC. A scripted proposer, selector and evaluator drive agent-harness's real HarnessOptimizer, so the files have the
exact format the optimizer writes. No model ran and nothing here is a result. The "details" strings contain a marker the
dashboard tests require never to appear in anything the dashboard returns."""
import shutil
from pathlib import Path

from agent_harness.h2.spec import OPENCODE, stock_spec
from agent_harness.sim import MemoryCondition, Provenance, SelfImprovementMemory
from agent_harness.sim.loop import Feedback, HarnessOptimizer, Proposal, TaskResult

HERE = Path(__file__).resolve().parent
TASKS = ["e1", "e2", "e3", "e4"]


class Evaluator:
    def evaluate(self, harness, tasks):
        text = harness.get("systemprompt.md")
        return Feedback(harness_id="", results=tuple(
            TaskResult(t, ("BREAK" not in text) if t == "e4" else (f"[{t}]" in text), detail=f"NEVER-SHOW-THIS reply text for {t}") for t in tasks))


class Proposer:
    def __init__(self, rounds):
        self.rounds = list(rounds)

    def propose(self, **_):
        return self.rounds.pop(0) if self.rounds else []


class Selector:
    def screen(self, proposals, **_):
        return ["screen me out" not in p.what for p in proposals]

    def choose(self, evaluated, *, current, memory):
        best = max(range(len(evaluated)), key=lambda i: evaluated[i][1].score, default=None)
        return best if best is not None and evaluated[best][1].score > current.score else None


def edit(text, cause, predicted, what, path="systemprompt.md"):
    return Proposal(root_cause=cause, what=what, rationale="synthetic rationale", edits={path: text}, predicted=tuple(predicted))


def run(name, rounds, memory, policy):
    spec = stock_spec(OPENCODE)
    spec.set("systemprompt.md", "You are an agent.")
    return HarnessOptimizer(evaluator=Evaluator(), proposer=Proposer(rounds), selector=Selector(), workdir=HERE / name, harness=spec,
                            memory=memory, condition=MemoryCondition.FULL, evolve_tasks=TASKS,
                            provenance=Provenance(domain="synthetic", benchmark="synthetic-bench", policy=policy, optimizer="scripted", evaluator="scripted", run=name))


for old in ("source", "target"):
    shutil.rmtree(HERE / old, ignore_errors=True)
src = run("source", [
    [edit("A [e1]", "markers are missing", ["e1", "e2"], "add one marker"),                     # accepted: e1 passes, e2 missed
     edit("A [e1] BREAK", "markers are missing", ["e1"], "add a marker and break e4")],          # rejected: regresses e4
    [edit("A [e1][e2]", "P1", ["e2"], "add the second marker"),                                  # accepted
     edit("x", "markers are missing", ["e3"], "screen me out"),                                  # screened out
     edit("x", "guardrails are in the way", ["e3"], "edit a protected file", path="guardrails/policy.md")],   # refused
], SelfImprovementMemory(run="source"), "model-a@1")
src.candidates_per_round = 3
first = src.step()
src.step()
src.rollback(first.promoted, reason="synthetic rollback for the fixtures")
run("target", [[edit("A [e1] BREAK", "markers are missing", ["e1", "e3"], "try again on the target")]], src.memory.carry_into("target", domain="synthetic-2"),
    "model-b@1").step()
for artifacts in HERE.glob("*/harnesses"):
    shutil.rmtree(artifacts)                                                                     # the dashboard never reads them

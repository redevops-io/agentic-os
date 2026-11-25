"""Self-improvement of the harness optimizer, from its own run records: what was proposed, what each edit predicted
before it was evaluated, what was measured, what was accepted, what it broke, what was carried to another run and what was
rolled back. Every number counts records the drill-down lists. Nothing here says that a system improves itself: it shows
what the recorded runs did, including when they accepted nothing."""
from __future__ import annotations

from ..build import Build, col, panel, row, tone_of, pct

S = ("sim_runs",)
COLS = ("run", "round", "decision", "root_cause", "predicted", "fixed", "missed", "regressed", "passed", "total", "policy")


def self_improvement(b: Build) -> None:
    d = b.d
    if not b.usable("sim_runs"):
        b.down("No optimizer run records can be read, so there is no improvement history to show.", "runs", "Runs recorded", S)
        return
    runs = b.records("sim_runs", "sim_run", timed=False)
    cands = b.records("sim_runs", "sim_candidate", timed=False)
    backs = b.records("sim_runs", "sim_rollback", timed=False)
    if not runs:
        d.empty = "The runs directory holds no optimizer run yet, so there is nothing to report."
        return
    evaluated = [c for c in cands if c.get("evaluated")]
    accepted = [c for c in cands if c.status == "accepted"]
    rejected = [c for c in cands if c.status != "accepted"]
    broke = [c for c in evaluated if c.get("regressed")]
    predicted = sum(int(c.get("predicted") or 0) for c in evaluated)
    fixed = sum(int(c.get("fixed") or 0) for c in evaluated)
    b.drill("sim:runs", "Optimizer runs", runs, columns=("condition", "rounds", "memory_problems", "memory_attempts", "carried_from", "policy"))
    b.drill("sim:evaluated", "Candidate edits that were evaluated", evaluated, columns=COLS)
    b.drill("sim:accepted", "Accepted edits", accepted, columns=COLS)
    b.drill("sim:rejected", "Edits not adopted", rejected, columns=COLS + ("refused",),
            note="Rejected after evaluation, refused by the loop (for example an edit outside the harness), or screened out before evaluation.")
    b.drill("sim:regressions", "Evaluated edits that broke a task that passed before", broke, columns=COLS + ("regressed_tasks",))
    b.drill("sim:predictions", "Evaluated edits and what they predicted", [c for c in evaluated if c.get("predicted")], columns=COLS + ("predicted_tasks",),
            note="The prediction was written before the edit was evaluated.")
    b.drill("sim:rollbacks", "Rollbacks", backs, columns=("run", "round", "from", "to", "reason"))
    nothing = ""
    if evaluated and not accepted:
        nothing = f"No edit was accepted in {len(runs)} recorded run(s), after {len(evaluated)} evaluated candidate(s)."
    d.summary = nothing
    d.panels.append(panel("summary", "What the runs did", "stats", sources=S, span=2, note=nothing, metrics=[
        b.metric("runs", "Runs recorded", needs=S, value=len(runs), drill="sim:runs"),
        b.metric("evaluated", "Candidate edits evaluated", needs=S, value=len(evaluated), drill="sim:evaluated"),
        b.metric("accepted", "Edits accepted", needs=S, ratio=True, num=len(accepted), den=len(evaluated), unit="%", drill="sim:accepted",
                 note=f"{len(accepted)} of {len(evaluated)} evaluated"),
        b.metric("not_adopted", "Edits not adopted", needs=S, value=len(rejected), drill="sim:rejected"),
        b.metric("prediction_hits", "Predicted tasks that then passed", needs=S, ratio=True, num=fixed, den=predicted, unit="%", drill="sim:predictions",
                 note=f"{fixed} of {predicted} predictions written before evaluation" if predicted else "no evaluated edit predicted a task",
                 tone=tone_of(pct(fixed, predicted), good=70, warn=40)),
        b.metric("regressing", "Evaluated edits that broke something", needs=S, value=len(broke), drill="sim:regressions", good="down",
                 tone="warn" if broke else "ok"),
        b.metric("rollbacks", "Rollbacks", needs=S, value=len(backs), drill="sim:rollbacks"),
    ]))
    per_run = b.group(evaluated, lambda c: c.get("run"))
    if per_run:
        keys = sorted(per_run)
        d.panels.append(panel("predicted_observed", "Predicted and observed, by run", "stacked", sources=S, span=2, x=keys,
                              note="Of the tasks each run's evaluated edits predicted to fix: those that then passed, and those that did not.",
                              series=[{"id": sid, "label": label, "tone": tone, "values": [sum(int(c.get(sid) or 0) for c in per_run[k]) for k in keys]}
                                      for sid, label, tone in (("fixed", "Passed as predicted", "ok"), ("missed", "Predicted, did not pass", "warn"))]))

    def candidate_rows(items):
        return [row({"run": c.get("run"), "round": c.get("round"), "cause": c.get("root_cause"), "change": c.title, "decision": c.status,
                     "predicted": c.get("predicted"), "observed": f"{c.get('fixed')} fixed, {c.get('missed')} missed" if c.get("evaluated") else "not evaluated",
                     "broke": c.get("regressed_tasks") or ("none" if c.get("evaluated") else ""),
                     "score": f"{c.get('passed')}/{c.get('total')} (was {c.get('passed_before')})" if c.get("evaluated") else ""},
                    tone="ok" if c.status == "accepted" else "mut", ref=c.ref) for c in items]

    cand_cols = [col("run", "Run"), col("round", "Round", "right"), col("cause", "Root-cause hypothesis"), col("change", "Change tried"),
                 col("decision", "Decision"), col("predicted", "Predicted", "right"), col("observed", "Observed"), col("broke", "Broke"), col("score", "Evolve tasks passed")]
    ordered = sorted(cands, key=lambda c: (str(c.get("run")), int(c.get("round") or 0), c.id))
    d.panels.append(panel("interventions", "Candidate edits", "table", sources=S, span=2, columns=cand_cols, rows=candidate_rows(ordered[:100]),
                          note=("" if len(ordered) <= 100 else f"The first 100 of {len(ordered)}; the drill-downs list every one."),
                          empty="" if cands else "No candidate edit is recorded."))
    d.panels.append(panel("runs", "Runs, memory and transfer", "table", sources=S, span=2,
                          columns=[col("run", "Run"), col("condition", "Memory shown"), col("rounds", "Rounds", "right"), col("memory", "Memory"),
                                   col("carried", "Carried from"), col("policy", "Task model"), col("optimizer", "Optimizer"), col("benchmark", "Benchmark")],
                          rows=[row({"run": r.get("run"), "condition": r.status, "rounds": r.get("rounds"),
                                     "memory": f"{r.get('memory_problems')} causes, {r.get('memory_attempts')} attempts",
                                     "carried": f"{r.get('carried_from')} ({r.get('carried_domain')})" if r.get("carried_from") else "not carried",
                                     "policy": r.get("policy"), "optimizer": r.get("optimizer"), "benchmark": r.get("benchmark")}, ref=r.ref)
                                for r in sorted(runs, key=lambda r: r.id)]))
    d.panels.append(panel("rollbacks", "Rollback history", "table", sources=S, span=2,
                          columns=[col("run", "Run"), col("round", "After round", "right"), col("from", "From harness"), col("to", "Back to"), col("reason", "Reason")],
                          rows=[row({"run": r.get("run"), "round": r.get("round"), "from": r.get("from"), "to": r.get("to"), "reason": r.get("reason")}, ref=r.ref) for r in backs],
                          empty="" if backs else "No promotion has been rolled back."))

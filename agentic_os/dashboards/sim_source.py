"""Read the records a Self-Improvement optimizer writes (``agent-harness.sim-run/1`` and ``agent-harness.sim/1``).

A run directory holds ``state.json``, ``sim.json`` and ``rounds/round-NNN.outcome.json``. The directory named by the
operator is searched for run directories. Everything here is read from those files; nothing is estimated, and a file
that cannot be read or does not match its schema is reported, never skipped silently.

What is deliberately NOT read: the per-task ``details`` of an evaluation. They carry the model's replies and traces, which
can hold task data; a dashboard needs only which tasks passed.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

from .contracts import Record

RUN_SCHEMA = "agent-harness.sim-run/1"
MEMORY_SCHEMA = "agent-harness.sim/1"
MAX_RUNS = 200
MAX_DEPTH = 3
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROUNDS = 500
TEXT = 300
_OUTCOME = re.compile(r"^round-(\d{3,6})\.outcome\.json$")


class UnreadableRun(ValueError):
    pass


def _text(v: Any, limit: int = TEXT) -> str:
    s = " ".join(str(v or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _load(path: Path, base: Path) -> Any:
    real = path.resolve()
    if base != real and base not in real.parents:                     # a link out of the directory is not followed
        raise UnreadableRun(f"{path.name} points outside the runs directory")
    if real.stat().st_size > MAX_FILE_BYTES:
        raise UnreadableRun(f"{path.name} is larger than {MAX_FILE_BYTES} bytes")
    return json.loads(real.read_text(encoding="utf-8"))


def run_directories(base: Path) -> Tuple[List[Path], bool]:
    """Directories under ``base`` (itself included) that hold a ``state.json``. The flag says whether the list was cut."""
    found: List[Path] = []
    for root, dirs, files in os.walk(base, followlinks=False):
        here = Path(root)
        depth = len(here.relative_to(base).parts)
        if "state.json" in files:
            found.append(here)
            dirs[:] = []
        elif depth >= MAX_DEPTH:
            dirs[:] = []
        dirs.sort()
        if len(found) > MAX_RUNS:
            return found[:MAX_RUNS], True
    return found, False


def _ids(v: Any) -> List[str]:
    return [str(x) for x in v] if isinstance(v, (list, tuple)) else []


def read_run(path: Path, base: Path, *, project: str) -> List[Record]:
    """Records of one run: ``sim_run``, one ``sim_candidate`` per proposed edit, one ``sim_rollback`` per rollback."""
    state = _load(path / "state.json", base)
    if not isinstance(state, Mapping) or state.get("schema") != RUN_SCHEMA:
        raise UnreadableRun(f"state.json is not {RUN_SCHEMA}")
    memory = _load(path / "sim.json", base)
    if not isinstance(memory, Mapping) or memory.get("schema") != MEMORY_SCHEMA:
        raise UnreadableRun(f"sim.json is not {MEMORY_SCHEMA}")
    rel = path.relative_to(base).as_posix() if path != base else path.name
    prov = state.get("provenance") if isinstance(state.get("provenance"), Mapping) else {}
    models = {"policy": _text(prov.get("policy"), 80), "optimizer": _text(prov.get("optimizer"), 80),
              "benchmark": _text(prov.get("benchmark"), 80), "domain": _text(prov.get("domain"), 80),
              "evaluator": _text(prov.get("evaluator"), 80)}
    names = {str(p.get("id")): str(p.get("name", "")) for p in memory.get("problems", []) if isinstance(p, Mapping)}
    aliases = memory.get("aliases") if isinstance(memory.get("aliases"), Mapping) else {}

    def root_cause(ref: str) -> str:
        seen = set()
        while ref in aliases and ref not in seen:
            seen.add(ref)
            ref = str(aliases[ref])
        return _text(names.get(ref, ref))

    outcomes = []
    rounds_dir = path / "rounds"
    if rounds_dir.is_dir():
        outcomes = sorted((int(m.group(1)), f) for f in rounds_dir.iterdir() if (m := _OUTCOME.match(f.name)))[:MAX_ROUNDS]
    records: List[Record] = []
    for number, file in outcomes:
        doc = _load(file, base)
        current = doc.get("current") if isinstance(doc.get("current"), Mapping) else {}
        before = set(_ids(current.get("passed")))
        for i, c in enumerate(doc.get("candidates") or []):
            if not isinstance(c, Mapping):
                raise UnreadableRun(f"{file.name}: candidate {i} is not an object")
            predicted = _ids(c.get("predicted"))
            outcome = c.get("outcome") if isinstance(c.get("outcome"), Mapping) else None
            chosen = doc.get("chosen") == i
            decision = ("accepted" if chosen else "rejected" if outcome is not None else
                        "refused" if c.get("refused") else "screened out")
            f: Dict[str, Any] = {"run": rel, "round": number, "decision": decision, "root_cause": root_cause(str(c.get("root_cause", ""))),
                                 "change": _text(c.get("what")), "files": ", ".join(_ids(c.get("files")))[:TEXT],
                                 "predicted": len(predicted), "predicted_tasks": ", ".join(predicted)[:TEXT],
                                 "evaluated": outcome is not None, "refused": _text(c.get("refused")), **models}
            if outcome is not None:
                passed, failed = set(_ids(outcome.get("passed"))), set(_ids(outcome.get("failed")))
                fixed, regressed = [t for t in predicted if t in passed], sorted(before & failed)
                f.update(fixed=len(fixed), missed=len(predicted) - len(fixed), regressed=len(regressed),
                         regressed_tasks=", ".join(regressed)[:TEXT], passed=len(passed), total=len(passed) + len(failed),
                         passed_before=len(before))
            records.append(Record(source="sim_runs", kind="sim_candidate", id=f"{rel}#{number}.{i}", title=f["change"], status=decision,
                                  project=project, fields=f, ref=f"sim:{rel}#{number}.{i}"))
    promotions = [p for p in state.get("promotions") or [] if isinstance(p, Mapping)]
    for n, p in enumerate(promotions):
        if p.get("kind") == "rollback":
            records.append(Record(source="sim_runs", kind="sim_rollback", id=f"{rel}#rollback{n}", title=_text(p.get("reason")), status="rollback",
                                  project=project, ref=f"sim:{rel}#rollback{n}",
                                  fields={"run": rel, "round": p.get("round"), "to": str(p.get("harness", ""))[:24],
                                          "from": str(p.get("from", ""))[:24], "reason": _text(p.get("reason"))}))
    problems = [p for p in memory.get("problems", []) if isinstance(p, Mapping)]
    carried = memory.get("carried_from") if isinstance(memory.get("carried_from"), Mapping) else None
    records.append(Record(source="sim_runs", kind="sim_run", id=rel, title=_text(state.get("run") or rel, 80), status=str(state.get("condition", "")),
                          project=project, ref=f"sim:{rel}",
                          fields={"run": rel, "condition": str(state.get("condition", "")), "rounds": int(state.get("round") or 0),
                                  "rounds_recorded": len(outcomes),
                                  "memory_problems": len(problems), "memory_attempts": sum(len(p.get("attempts") or []) for p in problems),
                                  "merged_causes": len(aliases), "promotions": sum(p.get("kind") == "promotion" for p in promotions),
                                  "rollbacks": sum(p.get("kind") == "rollback" for p in promotions),
                                  "records_rejected": "yes" if state.get("record_rejected") else "no",
                                  "carried_from": _text((carried or {}).get("arm"), 80), "carried_domain": _text((carried or {}).get("domain"), 80),
                                  **models}))
    return records

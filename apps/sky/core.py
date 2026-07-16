"""SkyPilot core — the command wrappers behind the `sky` operator.

Each function builds a real ``sky`` argv and calls the injected runner, returning a structured
result (so tests / a dry-run pass a stub runner and the operator works without SkyPilot installed;
wire the real runner via ``build_sky_operator(run=…)``). Mirrors ``apps/infra/core.py``.

SkyPilot owns *where* a workload runs — the cheapest available cloud/region/instance, cross-cloud
failover, managed spot, autostop — so these wrappers are the deployment backbone beside Terraform
(substrate) and Ansible (config). ``optimize`` is the key one: its ranked table is the evidence a
human approves at the gate, and its *measured* outcome (real cost, capacity, preemption, time-to-
ready) is what turns a one-shot estimate into a placement the runtime learns per workload.
"""
from __future__ import annotations

import re
import subprocess
from typing import Callable

Runner = Callable[[list], tuple]  # argv -> (rc, stdout, stderr)


def _run(argv: list, cwd: "str | None" = None) -> "tuple[int, str, str]":
    p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _tail(text: str, n: int) -> str:
    text = (text or "").strip()
    return text[-n:]


def _spec_args(spec: dict) -> list:
    """Translate a workload spec → SkyPilot resource flags. No --cloud pins the choice, so the
    optimizer ranks across every enabled cloud (that is the whole point)."""
    args: list = []
    if g := spec.get("gpus"):
        args += ["--gpus", str(g)]
    if c := spec.get("cpus"):
        args += ["--cpus", str(c)]
    if m := spec.get("memory"):
        args += ["--memory", str(m)]
    if spec.get("spot"):
        args += ["--use-spot"]
    if r := spec.get("region"):
        args += ["--region", str(r)]
    if cl := spec.get("cloud"):  # optional pin; omit to optimize across all clouds
        args += ["--cloud", str(cl)]
    return args


# SkyPilot's `--dryrun` prints a "Considered resources" table; parse its rows into candidates.
_ROW = re.compile(
    r"^\s*(?P<cloud>[A-Za-z][\w-]*)\s+(?P<instance>\S+)\s+(?P<vcpus>[\d.]+)\s+(?P<mem>[\d.]+)\s+"
    r"(?P<acc>\S+)\s+(?P<region>\S+)\s+(?P<cost>[\d.]+)\s*(?P<chosen>[✔✓xX*]?)\s*$"
)
_HEADER = re.compile(r"CLOUD\s+INSTANCE", re.IGNORECASE)


def parse_optimizer(out: str) -> list:
    """Best-effort parse of SkyPilot's optimizer table → ranked candidate dicts. Robust to the
    table not being present (returns [])."""
    lines = (out or "").splitlines()
    candidates: list = []
    seen_header = False
    for ln in lines:
        if _HEADER.search(ln):
            seen_header = True
            continue
        if not seen_header:
            continue
        if set(ln.strip()) <= {"-", " "} and ln.strip():
            if candidates:  # trailing separator ends the table
                break
            continue
        m = _ROW.match(ln)
        if not m:
            continue
        cost = float(m.group("cost"))
        candidates.append({
            "cloud": m.group("cloud"), "instance": m.group("instance"),
            "vcpus": m.group("vcpus"), "memory_gb": m.group("mem"),
            "accelerators": m.group("acc"), "region": m.group("region"),
            "hourly_usd": cost, "est_monthly_usd": round(cost * 730, 2),
            "chosen": bool(m.group("chosen")),
        })
    return candidates


def optimize(spec: dict, *, run: Runner = _run) -> dict:
    """Rank {cloud × region × instance × spot} by live price + availability via `sky launch
    --dryrun`. The ranked table is the mission evidence presented at the approval gate."""
    name = spec.get("name", "sk-app")
    argv = ["sky", "launch", "--dryrun", "--yes", "-c", name] + _spec_args(spec)
    if cmd := spec.get("run"):
        argv += ["--", cmd]
    rc, out, err = run(argv)
    candidates = parse_optimizer(out)
    chosen = next((c for c in candidates if c.get("chosen")), candidates[0] if candidates else None)
    return {
        "status": "done" if rc == 0 else "error", "action": "optimize", "spec": spec,
        "candidates": candidates, "chosen": chosen, "n_candidates": len(candidates),
        "optimizer_table": _tail(out, 4000), "rc": rc,
        "error": _tail(err, 500) if rc else "",
    }


def launch(spec: dict, *, run: Runner = _run) -> dict:
    """Provision the chosen candidate (`sky launch`). SkyPilot fails over across regions/clouds on
    capacity/quota, so the mission's saga undo is `sky.down`. The result carries the *measured*
    placement outcome (cloud, cost, spot) — the reward the placement optimizer learns from."""
    import time
    name = spec.get("name", "sk-app")
    argv = ["sky", "launch", "--yes", "-c", name] + _spec_args(spec)
    if y := spec.get("task_yaml"):
        argv += [y]
    elif cmd := spec.get("run"):
        argv += ["--", cmd]
    t0 = time.monotonic()
    rc, out, err = run(argv)
    return {
        "status": "done" if rc == 0 else "error", "action": "launch", "cluster": name,
        "cloud": _grep(out, r"Launching on ([A-Za-z][\w-]*)") or spec.get("cloud"),
        "endpoint": _grep(out, r"(https?://\S+)"), "spot": bool(spec.get("spot")),
        # measured signal for the placement reward loop (see learn.PlacementLedger)
        "time_to_ready_s": round(time.monotonic() - t0, 1),
        "failed_over": bool(re.search(r"fail(?:ing|ed)?\s+over|retrying on|trying next", out or "", re.I)),
        "rc": rc, "summary": _tail(out, 600), "error": _tail(err, 500) if rc else "",
    }


def serve(spec: dict, *, run: Runner = _run) -> dict:
    """Stand up an autoscaled service (SkyServe: `sky serve up`) — replicas, load balancing,
    readiness — across clouds/regions."""
    name = spec.get("name", "sk-svc")
    argv = ["sky", "serve", "up", "--yes", "-n", name]
    if y := spec.get("task_yaml"):
        argv += [y]
    rc, out, err = run(argv)
    return {
        "status": "done" if rc == 0 else "error", "action": "serve", "service": name,
        "endpoint": _grep(out, r"(https?://\S+)"), "rc": rc,
        "summary": _tail(out, 600), "error": _tail(err, 500) if rc else "",
    }


def down(target: str, *, serve: bool = False, run: Runner = _run) -> dict:
    """Tear down a cluster (`sky down`) or service (`sky serve down`). Used both as the saga
    compensation for launch/serve AND as the gated idle-teardown (autostop)."""
    argv = ["sky", "serve", "down", "--yes", target] if serve else ["sky", "down", "--yes", target]
    rc, out, err = run(argv)
    return {"status": "done" if rc == 0 else "error", "action": "down", "target": target,
            "serve": serve, "rc": rc, "error": _tail(err, 500) if rc else ""}


def status(*, serve: bool = False, run: Runner = _run) -> dict:
    """Cluster/service status (`sky status` / `sky serve status`) — the monitor's read for
    utilization + idle detection."""
    argv = ["sky", "serve", "status"] if serve else ["sky", "status"]
    rc, out, err = run(argv)
    return {"status": "done" if rc == 0 else "error", "action": "status", "serve": serve,
            "raw": _tail(out, 3000), "rc": rc}


def check(*, run: Runner = _run) -> dict:
    """Preflight (`sky check`): which clouds are enabled (creds present). Evidence before a launch,
    the executable form of each per-provider skill's Prerequisites checklist."""
    rc, out, err = run(["sky", "check"])
    enabled = re.findall(r"^\s*([A-Za-z][\w-]*)\s*:\s*enabled", out or "", re.MULTILINE)
    return {"status": "done" if rc == 0 else "error", "action": "check",
            "enabled_clouds": enabled, "rc": rc, "raw": _tail(out, 2000)}


def _grep(text: str, pattern: str) -> "str | None":
    m = re.search(pattern, text or "")
    return m.group(1) if m else None

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

import os
import re
import subprocess
from typing import Callable

Runner = Callable[[list], tuple]  # argv -> (rc, stdout, stderr)


class SkyInputError(ValueError):
    """Rejected sky input (bad name / flag value / missing undo target)."""


_NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,62}")
_GPUS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,31}")
_COUNT_RE = re.compile(r"[0-9]+(\.[0-9]+)?\+?")
_REGION_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_INSTANCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_YAML_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,255}")
_CLOUDS = frozenset({"aws", "gcp", "azure", "oci", "lambda", "runpod", "fluidstack", "paperspace", "cudo",
                     "ibm", "scp", "vsphere", "kubernetes", "do", "nebius", "vast"})


def check_name(name, what: str = "name") -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise SkyInputError(f"invalid {what} {name!r}: must match {_NAME_RE.pattern}")
    return name


def _check_val(v, rx, what: str) -> str:
    sv = str(v)
    if not rx.fullmatch(sv) or ".." in sv:
        raise SkyInputError(f"invalid {what} {sv!r}")
    return sv


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
        args += ["--gpus", _check_val(g, _GPUS_RE, "gpus")]
    if c := spec.get("cpus"):
        args += ["--cpus", _check_val(c, _COUNT_RE, "cpus")]
    if m := spec.get("memory"):
        args += ["--memory", _check_val(m, _COUNT_RE, "memory")]
    if spec.get("spot"):
        args += ["--use-spot"]
    if r := spec.get("region"):
        args += ["--region", _check_val(r, _REGION_RE, "region")]
    if cl := spec.get("cloud"):  # optional pin; omit to optimize across all clouds
        if str(cl).lower() not in _CLOUDS:
            raise SkyInputError(f"invalid cloud {cl!r}")
        args += ["--cloud", str(cl)]
    return args


def _autostop_minutes(spec: dict) -> str:
    """Idle minutes before autostop: spec ``autostop_minutes`` overrides SKY_DEFAULT_AUTOSTOP_MINUTES (60).
    Bounded 1..10080 so a cluster is never left running indefinitely by default."""
    raw = spec.get("autostop_minutes", os.environ.get("SKY_DEFAULT_AUTOSTOP_MINUTES", "60"))
    sv = str(raw)
    if isinstance(raw, bool) or not re.fullmatch(r"[0-9]{1,5}", sv) or not 1 <= int(sv) <= 10080:
        raise SkyInputError(f"invalid autostop minutes {sv!r} (1..10080)")
    return str(int(sv))


def _guarded(action: str):
    def wrap(fn):
        def inner(*a, **kw):
            try:
                return fn(*a, **kw)
            except SkyInputError as e:
                return {"status": "error", "action": action, "error": str(e), "rc": None}
        inner.__name__ = fn.__name__
        inner.__doc__ = fn.__doc__
        return inner
    return wrap


def _exists(argv: list, name: str, run: Runner) -> "bool | None":
    """Whether `name` exists: queries status for that one name (`--` so it is never a flag) and looks for a
    row whose first column is exactly the name. None when status could not be read (non-zero exit).
    Note: check-then-launch is not atomic; two concurrent launches of one name can still race. The mission
    runtime serialises writers per cluster (concurrency_key), which is what closes that window."""
    rc, out, _ = run(argv + ["--", name])
    if rc != 0:
        return None
    return any((ln.split() or [""])[0] == name for ln in (out or "").splitlines())


def undo_target(inp: dict, field: str, action: str, flag: str) -> str:
    """The resource an undo may tear down: ONLY the one the forward result recorded (``inp['result']``).
    No defaults; refuses when the forward result is missing, did not create the resource, or a requested
    target differs from it."""
    fwd = inp.get("result")
    if not isinstance(fwd, dict):
        raise SkyInputError("undo requires the forward result (inputs.result) -- refusing")
    if fwd.get("action") != action or fwd.get(flag) is not True:
        raise SkyInputError("forward result does not show this action created the resource -- refusing")
    name = check_name(fwd.get(field), field)
    asked = inp.get("target") or inp.get(field)
    if asked and asked != name:
        raise SkyInputError("requested target does not match the forward resource -- refusing")
    return name


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


@_guarded("optimize")
def optimize(spec: dict, *, run: Runner = _run) -> dict:
    """Rank {cloud × region × instance × spot} by live price + availability via `sky launch
    --dryrun`. The ranked table is the mission evidence presented at the approval gate."""
    name = check_name(spec.get("name", "sk-app"), "cluster")
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


def _estimate(chosen: dict, pinned: dict, out: str) -> dict:
    actual = _grep(out, r"Launching on ([A-Za-z][\w-]*)") or pinned.get("cloud")
    failed_over = bool(re.search(r"fail(?:ing|ed)?\s+over|retrying on|trying next", out or "", re.I)) or bool(
        chosen.get("cloud") and actual and str(actual).lower() != str(chosen["cloud"]).lower())
    if chosen.get("hourly_usd") is None or failed_over:
        return {"hourly_usd": None, "est_monthly_usd": None, "cost_basis": ""}
    return {"hourly_usd": chosen.get("hourly_usd"), "est_monthly_usd": chosen.get("est_monthly_usd"),
            "cost_basis": "sky.optimize estimate of the approved placement"}


@_guarded("launch")
def launch(spec: dict, *, run: Runner = _run) -> dict:
    """Provision the chosen candidate (`sky launch`). SkyPilot fails over across regions/clouds on
    capacity/quota, so the mission's saga undo is `sky.down`. The result carries the *measured*
    placement outcome (cloud, cost, spot) — the reward the placement optimizer learns from."""
    import time
    name = check_name(spec.get("name", "sk-app"), "cluster")
    # Pin to the approved placement: the candidate sky.optimize ranked (threaded in as spec["chosen"])
    # overrides the open spec, so launch provisions EXACTLY what was ranked + approved rather than letting
    # SkyPilot re-optimize at launch time against prices/capacity that moved since approval. Without a
    # chosen candidate this is an open launch (the ungated path).
    chosen = spec.get("chosen") or {}
    pinned = dict(spec)
    if chosen.get("cloud"):
        pinned["cloud"] = chosen["cloud"]
    if chosen.get("region"):
        pinned["region"] = chosen["region"]
    argv = ["sky", "launch", "--yes", "-c", name, "--idle-minutes-to-autostop",
            _autostop_minutes(spec), "--down"] + _spec_args(pinned)
    if chosen.get("instance"):
        argv += ["--instance-type", _check_val(chosen["instance"], _INSTANCE_RE, "instance")]
    if y := spec.get("task_yaml"):
        argv += ["--", _check_val(y, _YAML_RE, "task_yaml")]
    elif cmd := spec.get("run"):
        argv += ["--", cmd]
    # Only launch onto a cluster that does not exist: launching onto an existing name would reuse it, and the
    # saga undo would then tear down something this launch never created.
    present = _exists(["sky", "status"], name, run)
    if present is not False:
        why = "cluster already exists" if present else "could not confirm the cluster name is free"
        return {"status": "error", "action": "launch", "cluster": name, "created": False, "rc": None,
                "error": f"{why} -- refusing to launch", "summary": "", "cluster_preexisting": bool(present)}
    t0 = time.monotonic()
    rc, out, err = run(argv)
    # The placement that ACTUALLY launched — cloud parsed from the output (reveals a capacity failover to a
    # different cloud than pinned), region/instance from the pinned placement. The reward loop keys to THIS.
    return {
        "status": "done" if rc == 0 else "error", "action": "launch", "cluster": name, "created": True,
        "cloud": _grep(out, r"Launching on ([A-Za-z][\w-]*)") or pinned.get("cloud"),
        "region": pinned.get("region"), "instance": chosen.get("instance"),
        "endpoint": _grep(out, r"(https?://\S+)"), "spot": bool(spec.get("spot")),
        # the sky.optimize ESTIMATE of the approved placement (None when launched without one, or when the launch
        # failed over to a different placement: the estimate then describes a cloud that did not run). Lets a failed
        # mission report what the still-provisioned cluster costs; never a measured bill (docs/COMPENSATION.md)
        **_estimate(chosen, pinned, out),
        # measured signal for the placement reward loop (see learn.PlacementLedger)
        "time_to_ready_s": round(time.monotonic() - t0, 1),
        "failed_over": bool(re.search(r"fail(?:ing|ed)?\s+over|retrying on|trying next", out or "", re.I)),
        "rc": rc, "summary": _tail(out, 600), "error": _tail(err, 500) if rc else "",
    }


@_guarded("serve")
def serve(spec: dict, *, run: Runner = _run) -> dict:
    """Stand up an autoscaled service (SkyServe: `sky serve up`) — replicas, load balancing,
    readiness — across clouds/regions."""
    name = check_name(spec.get("name", "sk-svc"), "service")
    argv = ["sky", "serve", "up", "--yes", "-n", name]
    if y := spec.get("task_yaml"):
        argv += ["--", _check_val(y, _YAML_RE, "task_yaml")]
    present = _exists(["sky", "serve", "status"], name, run)
    if present is not False:
        why = "service already exists" if present else "could not confirm the service name is free"
        return {"status": "error", "action": "serve", "service": name, "created": False, "rc": None,
                "error": f"{why} -- refusing to start", "summary": ""}
    rc, out, err = run(argv)
    return {
        "status": "done" if rc == 0 else "error", "action": "serve", "service": name, "created": True,
        "endpoint": _grep(out, r"(https?://\S+)"), "rc": rc,
        "summary": _tail(out, 600), "error": _tail(err, 500) if rc else "",
    }


@_guarded("down")
def down(target: str, *, serve: bool = False, run: Runner = _run) -> dict:
    """Tear down a cluster (`sky down`) or service (`sky serve down`). Used both as the saga
    compensation for launch/serve AND as the gated idle-teardown (autostop)."""
    check_name(target, "target")
    # one named resource only: never `-a`/--all, and `--` so the name can never be parsed as a flag
    argv = ["sky", "serve", "down", "--yes", "--", target] if serve else ["sky", "down", "--yes", "--", target]
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

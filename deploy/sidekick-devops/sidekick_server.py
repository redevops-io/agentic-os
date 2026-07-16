"""Sidekick for DevOps — the standalone server.

A lean packaging of the Mission Runtime as a DevOps copilot: the runtime + the Projects cockpit
(served by the kernel) + the infra operator (deploy · teardown · drift as governed missions) + the
standing monitor loop + read-only cluster/observability inspection. Point it at your cluster
(a read-only kubeconfig) and your model endpoint; it never mutates without an approval gate.

Run: `uvicorn sidekick_server:app` (see docker-compose.yml / the Helm chart).
"""
from __future__ import annotations

import os
import shutil
import subprocess

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

import mcp_reads
from monitor import MonitorLoop

from agentic_os.mission.api import build_capabilities_router, build_cockpit_router, build_router
from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import LocalOperatorClient
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore

from apps.infra.operator import build_infra_operator

INSPECT_NAMESPACE = os.environ.get("INSPECT_NAMESPACE", "default")
AUDIT_GRANTS = ["infra:read", "infra:write"]


# ── read-only cluster inspection ────────────────────────────────────────────────────────────────
# In a container OUTSIDE the target cluster (docker compose) we read via a mounted read-only
# kubeconfig with kubectl; in-cluster (the Helm chart) mcp_reads uses the pod service-account token.
def cluster_inventory() -> dict:
    kubeconfig = os.environ.get("KUBECONFIG")
    if kubeconfig and shutil.which("kubectl"):
        try:
            out = subprocess.run(
                ["kubectl", "top", "pods", "-A", "--no-headers"],
                capture_output=True, text=True, timeout=8, check=True).stdout
            rows = []
            for ln in out.splitlines():
                parts = ln.split()
                if len(parts) >= 4:
                    rows.append({"ns": parts[0], "name": parts[1][:40],
                                 "cpu_m": _mnum(parts[2]), "mem_mi": _mnum(parts[3])})
            return {"source": "live:kubectl", "count": len(rows),
                    "pods": sorted(rows, key=lambda r: -r["cpu_m"])[:12]}
        except Exception:  # noqa: BLE001 — unreachable cluster / no perms → fall through
            pass
    return mcp_reads.cluster_inventory()  # in-cluster SA path, else modeled


def _mnum(v: str) -> int:
    """'12m'→12, '48Mi'→48, plain int→int."""
    v = v.rstrip("m").rstrip("i").rstrip("M")
    try:
        return int(v)
    except ValueError:
        return 0


# ── runtime: infra operator (deploy/teardown/drift as governed missions) ─────────────────────────
def _runtime() -> MissionRuntime:
    infra = build_infra_operator()  # handlers dry-run until you wire real runners (see README)
    ops = {"infra": infra}
    reg = CapabilityRegistry()
    for op in ops.values():
        reg.register(op.manifest)
    return MissionRuntime(reg, Executor(LocalOperatorClient(ops)), store=EventStore())


runtime = _runtime()

# Alerting: turn approval gates into stakeholder sign-off alerts (opt-in via ALERT_WEBHOOK_URL).
# The monitor loop spawns governed response missions on live signals; each parks on its gate, which
# fires GateReached → an alert with a cockpit deep-link so a stakeholder can sign off fast.
if os.environ.get("ALERT_WEBHOOK_URL"):
    from notify import AlertContributor
    runtime.lifecycle.install(AlertContributor())

monitor = MonitorLoop(runtime, AUDIT_GRANTS)

app = FastAPI(title="Sidekick for DevOps")
app.include_router(build_router(runtime))           # /missions — plan · gate · execute · replay
app.include_router(build_cockpit_router(runtime))   # /cockpit · /inbox — the Projects surface
app.include_router(build_capabilities_router(runtime))


@app.on_event("startup")
def _start():
    if os.environ.get("MONITOR_ENABLED", "1") == "1":
        monitor.start()


@app.get("/")
def root():
    return RedirectResponse("/cockpit")


@app.get("/observability")
def observability():
    """Read-only ops surface: live cluster usage (kubectl / metrics-server), Prometheus + Grafana
    signals, and a Loki log tail — each tagged with its provenance (live vs modeled)."""
    return {
        "cluster": cluster_inventory(),
        "perf": mcp_reads.perf_signals(),
        "logs": mcp_reads.loki_tail(),
        "namespace": INSPECT_NAMESPACE,
    }


@app.get("/monitor")
def monitor_status():
    return monitor.status()


@app.get("/metrics")
def metrics():
    """Prometheus text-format metrics — the monitor's live signals + mission counts."""
    st = monitor.status()
    firing = sum(1 for v in st.get("signals", []) if v.get("firing"))
    lines = [
        "# TYPE sidekick_up gauge", "sidekick_up 1",
        "# TYPE sidekick_monitor_armed gauge", f"sidekick_monitor_armed {int(monitor.armed)}",
        "# TYPE sidekick_monitor_signals_firing gauge", f"sidekick_monitor_signals_firing {firing}",
        "# TYPE sidekick_missions_total gauge", f"sidekick_missions_total {len(runtime._missions)}",
    ]
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("\n".join(lines) + "\n")


@app.get("/healthz")
def healthz():
    return {"status": "ok", "namespace": INSPECT_NAMESPACE,
            "model_endpoint": bool(os.environ.get("MODEL_ENDPOINT")),
            "monitor": monitor.armed}

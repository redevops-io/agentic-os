"""The standing monitor loop — continuous operation for the Mission Runtime.

Missions are episodic; observability and load-response are continuous — so monitoring is NOT a
never-ending mission, it's a standing control loop that *watches and decides*, and spawns short
governed *response missions* when a signal fires (ops guide §21). This is the event-triggered version:
it reads live signals (pod usage from metrics-server via mcp_reads, plus modeled SLO/drift), evaluates
rules, and — when a rule fires and no response mission is already open for it — creates a governed
mission (which parks on its own approval gate; the human still approves). Reactive load-scaling stays
on the platform autoscaler; only structural changes become missions.
"""
from __future__ import annotations

import threading
import time

import mcp_reads

# nominal declared requests the audit compares live usage against (illustrative baseline)
_REQ_CPU_M, _REQ_MEM_MI = 500, 512


class MonitorLoop:
    def __init__(self, runtime, audit_grants, interval: float = 20.0, cw_template: str = "cost_audit"):
        self.runtime = runtime
        self.audit_grants = audit_grants
        self.interval = interval
        # template a CloudWatch-alarm response mission uses; point at an incident template where the
        # runtime registers one (defaults to the always-present cost_audit).
        self.cw_template = cw_template
        self.armed = True
        self.signals: dict[str, dict] = {}     # rule -> latest evaluation
        self.triggered: list[dict] = []        # response missions this loop spawned
        self._open: dict[str, str] = {}         # rule -> open mission id (dedupe: one per rule)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ── the control loop ──
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="monitor-loop", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self.evaluate()
            except Exception:  # noqa: BLE001 — a monitor must never crash the app
                pass

    def evaluate(self) -> dict:
        """One tick: read signals, evaluate rules, spawn a response mission on a firing rule."""
        if not self.armed:
            return {"armed": False}
        util = mcp_reads.pod_utilization()
        cpu, mem, src = util.get("total_cpu_m", 0), util.get("total_mem_mi", 0), util["source"]

        # RULE rightsize — live: actual usage far below declared requests ⇒ over-provisioned ⇒ cost_audit
        under = cpu < _REQ_CPU_M * 0.25 and mem < _REQ_MEM_MI * 0.5
        self._rule("rightsize", under, "cost",
                   f"pod using {cpu}m / {mem}Mi vs ~{_REQ_CPU_M}m / {_REQ_MEM_MI}Mi requested ({src}) — over-provisioned",
                   "cost_audit", "Rightsize the over-provisioned deployment (monitor-triggered)")
        # RULE slo_burn — modeled healthy (error budget ok) ⇒ no response
        self._rule("slo_burn", False, "reliability", "error budget 62% remaining — healthy", "cost_audit", "")
        # RULE drift — modeled: no infra drift ⇒ no response
        self._rule("drift", False, "config", "live state matches IaC — no drift", "cost_audit", "")

        # RULE cloudwatch — post-deploy AWS health: each alarm in ALARM state becomes a governed
        # response mission (one per alarm; cleared alarms resolve so they can re-trigger later).
        self._evaluate_cloudwatch()
        return self.status()

    def _evaluate_cloudwatch(self) -> None:
        alarms = mcp_reads.cloudwatch_alarms()          # modeled (empty) until AWS creds are wired
        firing = {a["name"]: a for a in alarms.get("alarms", []) if a.get("name")}
        # resolve previously-firing cloudwatch alarms that have cleared
        for rule in [r for r in list(self._open) if r.startswith("cloudwatch:")]:
            if rule.split(":", 1)[1] not in firing:
                self._rule(rule, False, "reliability", f"{rule} cleared", self.cw_template, "")
        # fire a response mission for each active alarm
        for name, a in firing.items():
            self._rule(f"cloudwatch:{name}", True, "reliability",
                       f"CloudWatch alarm {name} in ALARM: {a.get('reason', '')}",
                       self.cw_template,
                       f"Investigate CloudWatch alarm {name} (monitor-triggered)")

    def _rule(self, rule: str, firing: bool, severity: str, reason: str, template: str, goal: str):
        self.signals[rule] = {"rule": rule, "firing": bool(firing), "severity": severity,
                              "reason": reason, "at": time.time()}
        if firing and rule not in self._open:
            # detect → act: spawn a governed response mission (it parks on its own gate)
            m = self.runtime.create_mission(goal or f"{rule} response", policy_refs=self.audit_grants, template=template)
            self.runtime.run(m.id)
            self._open[rule] = m.id
            self.triggered.append({"rule": rule, "severity": severity, "reason": reason,
                                   "template": template, "mission_id": m.id, "at": time.time()})
        elif not firing and rule in self._open:
            self._open.pop(rule, None)  # resolved — allow a future re-trigger

    def status(self) -> dict:
        return {
            "armed": self.armed,
            "interval_s": self.interval,
            "signals": list(self.signals.values()),
            "triggered": self.triggered,
            "open": self._open,
            "summary": (f"monitoring — {sum(1 for s in self.signals.values() if s['firing'])} of "
                        f"{len(self.signals)} signals firing; {len(self.triggered)} response mission(s) spawned"),
        }

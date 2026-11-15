"""Async scan lifecycle: scan_start returns a job_id immediately; a background thread runs the
scanner; scan_status/scan_results poll it. In-memory only (a single-process service; the client already
polls, so there is nothing to persist across a restart that the operator couldn't just re-run).
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from .findings import Finding, summarize
from .scanners import k8s_exposure, k8s_identity, k8s_netpol, k8s_rbac, nmap_web

#: profile name -> scanner module. Adding a profile is a one-line change here; nothing else in this
#: file knows what a profile actually does.
SCANNERS = {
    "web-baseline": nmap_web,
    "k8s-rbac": k8s_rbac,
    "k8s-netpol": k8s_netpol,
    "k8s-exposure": k8s_exposure,
    "k8s-identity": k8s_identity,
}


@dataclass
class Job:
    job_id: str
    target: str
    profile: str
    state: str = "running"            # running | done | error
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    findings: list[Finding] = field(default_factory=list)
    error: str = ""

    def to_status(self) -> dict:
        return {"job_id": self.job_id, "state": self.state,
                "tools": [self.profile], "tools_done": [self.profile] if self.state != "running" else [],
                "elapsed_s": round((self.finished_at or time.time()) - self.started_at, 1)}

    def to_results(self) -> dict:
        d = self.to_status()
        if self.state == "done":
            d["summary"] = summarize(self.target, self.findings)
            d["findings"] = [f.to_dict() for f in self.findings]
        elif self.state == "error":
            d["error"] = self.error
        return d


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, profile: str, target: str) -> Job:
        job = Job(job_id=uuid.uuid4().hex[:16], target=target, profile=profile)
        with self._lock:
            self._jobs[job.job_id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _run(self, job: Job) -> None:
        scanner = SCANNERS[job.profile]
        try:
            job.findings = scanner.run(job.target)
            job.state = "done"
        except Exception as e:  # noqa: BLE001 — a scanner failure must surface as a job error, not crash the service
            job.error = f"{type(e).__name__}: {e}"
            job.state = "error"
        finally:
            job.finished_at = time.time()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

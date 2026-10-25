"""SEC-CFG-01 evidence from retained host scans: the kube-bench (CIS hardening baseline) and Trivy (operating-system package vulnerabilities) reports of the cluster's nodes, as kept by `ops/scan_aks_nodes.py` in redevops-app-web.

The scans themselves run elsewhere (a short-lived Job on each node) and write a directory per day: the raw reports, a `summary.json` with the tool and image digests, the node's OS and kernel, and a sha256 of every raw report. This collector reads those directories and turns each
successful scan into one `scanner_result` payload (`target_type: "host"`, the node as the target, `critical_open`, `scanned_at`). What it will not do:

  * It re-hashes every raw report against the summary and REFUSES the directory if one does not match: retained evidence that was edited is not evidence.
  * A scan that failed (the Job did not complete) is not a clean scan: it is reported as a gap for that window, never as zero findings.
  * A window with no scan in it is a gap.
  * `critical_open` is whatever the scan script computed and recorded (`mapping` is carried into the payload): FAILs of scored CIS checks for kube-bench, distinct CRITICAL CVE ids for Trivy. Neither tool has a notion of the other's severity, so the two are separate payloads, and either one with a critical finding fails
    the control for that period.

The scope is the nodes the script scanned and the checks the tools run; see the script for what it does not see (container images, the managed control plane, anything outside the OS package manager)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..pipeline import Collected, CollectionFailed


def _epoch(ts: Any) -> Optional[float]:
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
        except ValueError:
            return None
    return None


class RetainedHostScans:
    kind = "scanner_result"
    source = "retained host scans (kube-bench CIS baseline, Trivy OS packages) from ops/scan_aks_nodes.py"

    def __init__(self, directory: str, name: str = "host-scans") -> None:
        self.directory, self.name = Path(directory), name

    def _summaries(self) -> list:
        out = []
        for s in sorted(self.directory.glob("*/summary.json")):
            try:
                d = json.loads(s.read_text())
            except (OSError, ValueError):
                raise CollectionFailed(f"{s.parent.name}: the scan summary is unreadable") from None
            if not isinstance(d, dict) or not isinstance(d.get("nodes"), list):
                raise CollectionFailed(f"{s.parent.name}: the scan summary has an unexpected shape")
            out.append((s.parent, d))
        return out

    def collect(self, window_from: float, window_to: float) -> Collected:
        scans, failed = [], []
        for folder, d in self._summaries():
            for node in d["nodes"]:
                at = _epoch(node.get("scanned_at"))
                if at is None or not (window_from <= at < window_to):
                    continue
                for tool, s in (node.get("scans") or {}).items():
                    raw = folder / str(s.get("raw", ""))
                    try:
                        digest = hashlib.sha256(raw.read_bytes()).hexdigest()
                    except OSError:
                        raise CollectionFailed(f"{folder.name}/{s.get('raw')}: the raw report is missing") from None
                    if digest != s.get("sha256"):
                        raise CollectionFailed(f"{folder.name}/{s.get('raw')}: the raw report does not match its recorded hash: this evidence was changed after it was kept")
                    if not s.get("ok"):
                        failed.append(f"{node.get('node')}/{tool}")
                        continue
                    scans.append({"target_type": "host", "target": node.get("node"), "scanner": tool, "critical_open": int(s.get("critical_open", 0) or 0), "scanned_at": at, "population_complete": True,
                                  "os": node.get("os_image"), "kernel": node.get("kernel"), "tool_image": s.get("image"), "report_sha256": digest, "mapping": s.get("mapping", ""),
                                  "detail": {k: s[k] for k in ("scored_fail", "warn_manual", "pass", "critical_with_fix_available", "critical_without_fix", "high_open") if k in s}})
        if failed:
            raise CollectionFailed("a scan did not complete, which is not a clean scan: " + ", ".join(failed))
        if not scans:
            raise CollectionFailed("no retained host scan falls in this window")
        # one payload per scan; the assessor reads a list of payloads, the pipeline stores one envelope per collection, so carry the scans together
        worst = max(scans, key=lambda x: x["critical_open"])
        return Collected({**worst, "scans": scans, "scan_count": len(scans)})

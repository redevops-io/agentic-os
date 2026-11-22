"""The one finding shape every scanner returns, and the summary deploy_scan.py's results() renders.

Matches exactly what the client (apps/edge-sentinel/deploy_scan.py::results) expects back: a `findings`
list of {severity, name, target, ref} and a `summary` of {target, total, by_severity, max_severity}.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SEVERITIES = ("critical", "high", "medium", "low", "info")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}   # lower index = more severe


@dataclass(frozen=True)
class Finding:
    severity: str            # one of SEVERITIES
    name: str                 # short, human-readable claim ("open port 22/tcp (ssh)")
    target: str                # the specific sub-target this finding is about (host:port, namespace/kind/name, ...)
    ref: str = ""              # a stable reference: a CVE id, a RoleBinding name, a NetworkPolicy gap id, ...
    detail: str = ""           # longer, operator-facing explanation; never includes the raw scanner payload
    recommended_action: str = ""

    def __post_init__(self):
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown severity {self.severity!r}; must be one of {SEVERITIES}")

    def to_dict(self) -> dict:
        return {"severity": self.severity, "name": self.name, "target": self.target,
                "ref": self.ref, "detail": self.detail, "recommended_action": self.recommended_action}


def summarize(target: str, findings: list[Finding]) -> dict:
    by_severity: dict[str, int] = {}
    for f in findings:
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
    max_sev = min((f.severity for f in findings), key=lambda s: _RANK[s], default="")
    return {"target": target, "total": len(findings), "by_severity": by_severity, "max_severity": max_sev}

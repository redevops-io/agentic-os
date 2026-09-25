"""P2 customer-driven vulnerability-scanner BYO adapters: Tenable, Qualys, Rapid7 (moat plan §3.10, §6 P2).

Enterprise vuln/asset intelligence that a customer already runs. Built ready-to-wire but registered only on a
concrete customer requirement (§6 P2). They feed VULNERABILITY_EXPLOITABILITY (and, for Rapid7, THREAT_INTELLIGENCE)
through the same evidence contract as OpenSCAP/CrowdSec, so remediation prioritization is governed evidence — a
score never authorizes a destructive action on its own. All BYO; the open stack runs without them.
"""
from __future__ import annotations

from urllib.parse import quote

from runtime_contracts.protocol import Capability, EvidenceRef, ProviderFamily, content_hash

from ._base import HttpEvidenceProvider


def _severity_confidence(rows: list[dict]) -> float:
    """Confidence tracks the worst exploitability signal present (VPR/CVSS/severity)."""
    best = 0.0
    for r in rows:
        for k in ("vpr_score", "vprScore", "cvss_score", "cvssV3Score", "riskScore"):
            v = r.get(k)
            if isinstance(v, (int, float)):
                best = max(best, float(v) / 10.0)
        sev = str(r.get("severity", r.get("riskFactor", ""))).lower()
        best = max(best, {"critical": 0.95, "high": 0.8, "medium": 0.5, "low": 0.3}.get(sev, 0.0))
    return min(0.99, best) if best else 0.6


class TenableProvider(HttpEvidenceProvider):
    """Tenable.io — asset vulnerabilities with VPR (exploitability) scoring (§3.10 P2). BYO access+secret key.

    Credential is the combined Tenable header value `accessKey=<a>;secretKey=<s>` supplied by the tenant."""
    provider_id = "tenable"
    _caps = (Capability.VULNERABILITY_EXPLOITABILITY,)
    _price = 0.0
    _license = "Tenable.io (customer-licensed)"
    _BASE = "https://cloud.tenable.com"

    def _request(self, request):
        asset = request.subject_refs[0]
        url = f"{self._BASE}/workbenches/assets/{quote(asset)}/vulnerabilities"
        return "GET", url, {"X-ApiKeys": self._cred}, None

    def _extract(self, body, request):
        vulns = body.get("vulnerabilities") or []
        if not vulns:
            return [], [], 0.0
        obs = [{k: v.get(k) for k in ("plugin_id", "plugin_name", "severity", "vpr_score", "count",
                                      "cve") if k in v} for v in vulns[:10]]
        return obs, [EvidenceRef(ref=f"tenable:{request.subject_refs[0]}", source="tenable",
                                 content_hash=content_hash(body), ref_type="record")], _severity_confidence(vulns)


class QualysProvider(HttpEvidenceProvider):
    """Qualys VMDR — host vulnerability detections (§3.10 P2). BYO basic-auth token.

    NOTE: the live Qualys API returns XML; the real transport converts detections→dicts before `_extract`."""
    provider_id = "qualys"
    _caps = (Capability.VULNERABILITY_EXPLOITABILITY,)
    _price = 0.0
    _license = "Qualys VMDR (customer-licensed)"
    _BASE = "https://qualysapi.qualys.com/api/2.0/fo"

    def _request(self, request):
        host = request.subject_refs[0]
        url = f"{self._BASE}/asset/host/vm/detection/?action=list&ips={quote(host)}"
        return "GET", url, {"Authorization": f"Basic {self._cred}",
                            "X-Requested-With": "redevops-agentic-os"}, None

    def _extract(self, body, request):
        # transport normalizes the XML into {"detections": [{"qid","title","severity","cve_id","results"}...]}
        dets = body.get("detections") or []
        if not dets:
            return [], [], 0.0
        obs = [{k: d.get(k) for k in ("qid", "title", "severity", "cve_id", "type", "status") if k in d}
               for d in dets[:10]]
        return obs, [EvidenceRef(ref=f"qualys:{request.subject_refs[0]}", source="qualys",
                                 content_hash=content_hash(body), ref_type="record")], _severity_confidence(dets)


class Rapid7Provider(HttpEvidenceProvider):
    """Rapid7 InsightVM — asset vulnerabilities + threat/exploit exposure (§3.10 P2). BYO Insight API key."""
    provider_id = "rapid7"
    _caps = (Capability.VULNERABILITY_EXPLOITABILITY, Capability.THREAT_INTELLIGENCE)
    _price = 0.0
    _license = "Rapid7 InsightVM (customer-licensed)"
    _BASE = "https://us.api.insight.rapid7.com/vm/v4/integration"

    def _request(self, request):
        asset = request.subject_refs[0]
        url = f"{self._BASE}/assets/{quote(asset)}/vulnerabilities"
        return "GET", url, {"X-Api-Key": self._cred}, None

    def _extract(self, body, request):
        vulns = body.get("data") or body.get("vulnerabilities") or []
        if not vulns:
            return [], [], 0.0
        obs = [{k: v.get(k) for k in ("id", "title", "severity", "riskScore", "cvssV3Score", "cves",
                                      "exploits", "malwareKits") if k in v} for v in vulns[:10]]
        return obs, [EvidenceRef(ref=f"rapid7:{request.subject_refs[0]}", source="rapid7",
                                 content_hash=content_hash(body), ref_type="record")], _severity_confidence(vulns)

"""web-baseline profile: nmap service-version detection against one host, open ports only.

Target shape: "host" or "host:port,port,..." (a bare host scans a small, fixed default port set — this
is a baseline, not a full 65535-port sweep). Requires the `nmap` binary; reports unavailable without it.
"""
from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET

from ..findings import Finding

#: The "baseline" the profile name promises: common web/infra ports, not a full sweep. A caller that
#: wants a specific set names them after a colon in the target.
_DEFAULT_PORTS = "21,22,23,25,80,443,3306,5432,6379,8080,8443,9200,27017"

#: Services whose mere exposure is worth flagging even with no CVE attached (DBs, caches, admin panels
#: that are routinely found open-to-the-world by accident).
_HIGH_RISK_SERVICES = {"mysql", "postgresql", "redis", "mongodb", "elasticsearch", "memcached"}


def _parse_target(target: str) -> tuple[str, str]:
    if ":" in target:
        host, ports = target.split(":", 1)
        return host, ports
    return target, _DEFAULT_PORTS


def run(target: str) -> list[Finding]:
    if not shutil.which("nmap"):
        return [Finding(severity="info", name="nmap is not installed; web-baseline is unavailable",
                         target=target)]

    host, ports = _parse_target(target)
    proc = subprocess.run(
        ["nmap", "-sV", "-T4", "--open", "-oX", "-", "-p", ports, host],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return [Finding(severity="info", name=f"nmap failed: {proc.stderr.strip()[:200]}", target=target)]

    root = ET.fromstring(proc.stdout)
    findings: list[Finding] = []
    for host_el in root.findall("host"):
        addr = next((a.get("addr") for a in host_el.findall("address") if a.get("addrtype") in ("ipv4", "ipv6")), host)
        ports_el = host_el.find("ports")
        if ports_el is None:
            continue
        for port_el in ports_el.findall("port"):
            state = port_el.find("state")
            if state is None or state.get("state") != "open":
                continue
            portid = port_el.get("portid", "?")
            proto = port_el.get("protocol", "tcp")
            svc = port_el.find("service")
            name = svc.get("name", "unknown") if svc is not None else "unknown"
            product = (svc.get("product", "") if svc is not None else "")
            version = (svc.get("version", "") if svc is not None else "")
            where = f"{addr}:{portid}/{proto}"
            service_desc = f"{name}" + (f" ({product} {version})".rstrip(" )") + ")" if product else "")
            severity = "medium" if name in _HIGH_RISK_SERVICES else "info"
            findings.append(Finding(
                severity=severity, name=f"open port {portid}/{proto} ({service_desc})",
                target=where, ref="",
                detail=f"nmap service-version detection identified {service_desc} listening on {where}.",
                recommended_action=(f"Confirm {where} is meant to be reachable from where this scan "
                                     "ran; restrict with a firewall rule or NetworkPolicy if not.")
                if severity != "info" else ""))
    return findings

"""MITRE ATT&CK identity + a deterministic scenario→technique mapping (Phase 2).

The plan wants ATT&CK's official STIX data path and version identity, with evidence/confidence mapping.
This keeps a small, pinned catalog of AttackPattern SDOs (the techniques the current CrowdSec scenarios
actually exercise) rather than vendoring the full ~600-technique bundle — the ingestion seam
(:func:`attack_pattern`) is the same shape the real ATT&CK STIX bundle uses (``external_references`` with
an ``mitre-attack`` source and the ``Txxxx`` id), so swapping in the full bundle later is a data change,
not a code change.

``map_scenario_to_techniques`` is DETERMINISTIC and inspectable — a scenario substring → technique table,
never a model guess. Each mapping carries the matched token as its basis, so a Finding's ``attack_refs``
are explainable.
"""
from __future__ import annotations

import re

from .stix import Provenance, StixObject

# Pinned identity so a case records which ATT&CK version its mapping came from (acceptance: version identity).
ATTACK_VERSION = "ATT&CK v15.1 (enterprise)"
ATTACK_SOURCE = "mitre-attack"

# technique_id -> (name, tactic)
_CATALOG: dict[str, tuple[str, str]] = {
    "T1110": ("Brute Force", "credential-access"),
    "T1110.001": ("Password Guessing", "credential-access"),
    "T1046": ("Network Service Discovery", "discovery"),
    "T1595": ("Active Scanning", "reconnaissance"),
    "T1071": ("Application Layer Protocol", "command-and-control"),
    "T1190": ("Exploit Public-Facing Application", "initial-access"),
    "T1059": ("Command and Scripting Interpreter", "execution"),
    "T1078": ("Valid Accounts", "defense-evasion"),
    "T1499": ("Endpoint Denial of Service", "impact"),
}

# Explicit CrowdSec hub scenario -> technique table (authoritative for these names; the namespace prefix, e.g.
# "crowdsecurity/", is stripped before lookup). Rationale, by family:
#   *-bf / *_bf            repeated credential attempts                         -> T1110 (+T1110.001 for ssh password guessing)
#   *-cve-* on a service   exploitation of a public-facing service              -> T1190
#   log4j / RCE CVEs       JNDI/command injection yields code execution         -> T1190 + T1059 (only where RCE is the CVE's impact)
#   *-probing, scans,      reconnaissance of paths/files/UAs/hosts              -> T1595 (T1046 for port/service scans)
#   crawl, bad-user-agent
#   sqli/xss/traversal     injection probes against a web app                   -> T1190 (+T1595: they are probes, not confirmed exploitation)
#   resource-abuse,        request flooding of an endpoint                      -> T1499
#   req-limit-exceeded
# Scenarios with no honest ATT&CK analogue (e.g. postfix-spam) are intentionally absent: abstain, don't guess.
_SCENARIO_TABLE: dict[str, tuple[str, ...]] = {
    "ssh-bf": ("T1110", "T1110.001"),
    "ssh-slow-bf": ("T1110", "T1110.001"),
    "ssh-cve-2024-6387": ("T1190",),
    "http-bf": ("T1110",),
    "http-bf-wordpress_bf": ("T1110",),
    "http-wordpress_bf": ("T1110",),
    "http-wordpress_bf_xmlrpc": ("T1110",),
    "http-wordpress-scan": ("T1595",),
    "http-generic-403-bf": ("T1595",),
    "http-crawl-non_statics": ("T1595",),
    "http-bad-user-agent": ("T1595",),
    "http-probing": ("T1595",),
    "http-sensitive-files": ("T1595",),
    "http-backdoors-attempts": ("T1595",),
    "http-admin-interface-probing": ("T1595",),
    "http-open-proxy": ("T1595",),
    "http-sqli-probing": ("T1190", "T1595"),
    "http-xss-probing": ("T1190", "T1595"),
    "http-path-traversal-probing": ("T1190", "T1595"),
    "http-cve-2021-41773": ("T1190",),
    "http-cve-2021-42013": ("T1190",),
    "http-cve-2022-26134": ("T1190", "T1059"),
    "http-cve-2022-22965": ("T1190", "T1059"),
    "apache_log4j2_cve-2021-44228": ("T1190", "T1059"),
    "grafana-cve-2021-43798": ("T1190",),
    "jira_cve-2021-26086": ("T1190",),
    "f5-big-ip-cve-2020-5902": ("T1190",),
    "http-resource-abuse": ("T1499",),
    "http-dos-swithcing-ua": ("T1499",),
    "nginx-req-limit-exceeded": ("T1499",),
    "port-scan": ("T1046",),
    "iptables-scan-multi_ports": ("T1046",),
    "smb-bf": ("T1110",),
    "mysql-bf": ("T1110",),
    "mssql-bf": ("T1110",),
    "telnet-bf": ("T1110",),
    "proftpd-bf": ("T1110",),
    "pureftpd-bf": ("T1110",),
}

# Token fallback for scenario names not in the table (custom / newer hub scenarios). CrowdSec scenario substring -> technique ids (deterministic, inspectable)
_SCENARIO_MAP: list[tuple[str, tuple[str, ...]]] = [
    ("ssh-bf", ("T1110", "T1110.001")),
    ("bruteforce", ("T1110",)),
    ("brute", ("T1110",)),
    ("http-bf", ("T1110",)),
    ("credential", ("T1110",)),
    ("port-scan", ("T1046",)),
    ("scan", ("T1595",)),
    ("nmap", ("T1046",)),
    ("probing", ("T1595",)),
    ("sqli", ("T1190",)),
    ("xss", ("T1190",)),
    ("flood", ("T1499",)),
    ("dos", ("T1499",)),
    ("crawl", ("T1595",)),
    ("user-agent", ("T1595",)),
    ("http-probing", ("T1595",)),
    ("exploit", ("T1190",)),
    ("rce", ("T1190", "T1059")),
    ("injection", ("T1190",)),
    ("traversal", ("T1190",)),
    ("c2", ("T1071",)),
]


def attack_pattern(technique_id: str, *, source: str = ATTACK_SOURCE,
                   evidence_refs: tuple[str, ...] = ()) -> StixObject:
    """An ATT&CK technique as a STIX AttackPattern SDO (same shape as the official bundle)."""
    name, tactic = _CATALOG.get(technique_id, (technique_id, "unknown"))
    prov = Provenance(source=source, method="attack-catalog", evidence_refs=tuple(evidence_refs))
    return StixObject(
        type="attack-pattern", key=technique_id,
        props={
            "name": name,
            "x_mitre_version": ATTACK_VERSION,
            "kill_chain_phases": [{"kill_chain_name": "mitre-attack", "phase_name": tactic}],
            "external_references": [
                {"source_name": ATTACK_SOURCE, "external_id": technique_id,
                 "url": f"https://attack.mitre.org/techniques/{technique_id.replace('.', '/')}"}],
        },
        provenance=prov)


def map_scenario_to_techniques(scenario: str) -> list[tuple[str, str]]:
    """Deterministic scenario → [(technique_id, matched_token)]. The matched token IS the basis, so the
    mapping is explainable. First-match-wins per token, de-duplicated, order-stable."""
    s = (scenario or "").lower()
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    name = s.rsplit("/", 1)[-1].strip()
    if name in _SCENARIO_TABLE:                      # explicit table is authoritative for known hub names
        return [(t, name) for t in _SCENARIO_TABLE[name]]
    if re.search(r"(?<![a-z0-9])cve-\d{4}-\d{3,}", s):   # unknown CVE scenario: exploitation of a service
        out.append(("T1190", "cve"))
        seen.add("T1190")
    for token, techniques in _SCENARIO_MAP:
        # Match the token as a DELIMITED unit (bounded by non-alphanumerics), not a raw substring, so a
        # short token like "rce" does not match inside "bruteforce"/"force"/"source" and mislabel a
        # brute-force attack as Exploit-Public-Facing-Application (T1190) + Command Execution (T1059).
        if re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", s):
            for t in techniques:
                if t not in seen:
                    seen.add(t)
                    out.append((t, token))
    return out

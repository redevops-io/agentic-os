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
}

# CrowdSec scenario substring -> technique ids (deterministic, inspectable)
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
    for token, techniques in _SCENARIO_MAP:
        if token in s:
            for t in techniques:
                if t not in seen:
                    seen.add(t)
                    out.append((t, token))
    return out

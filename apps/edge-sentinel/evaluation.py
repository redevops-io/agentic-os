"""Evaluation, then bounded Learn (Phase 10).

Order matters here and is enforced by structure: **evaluation comes first.** Frozen labelled corpora,
deterministic metrics, an explicit baseline — measured against the real Phase 2/4 functions as the system
under test. Only then a Learn adapter, and it is deliberately narrow: it consumes the shared
``discovery_runtime.learn`` contract ("a correction is not a rule"), and lessons may alter **strategy**
(evidence ordering, enrichment/query choice, escalation timing) — **never** authorization policy or the
deterministic security gates. That invariant is not a comment; :func:`assert_strategy_only` refuses any
lesson whose scope touches a gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .attack import map_scenario_to_techniques
from .evidence import canonical_json, sha256_hex


# ──────────────────────────── evaluation (first) ────────────────────────────

@dataclass(frozen=True)
class LabelledExample:
    input: str
    expected: tuple[str, ...]        # the gold labels (e.g. expected ATT&CK technique ids)
    split: str = "test"
    rationale: str = ""              # why this label (source / reasoning), written independently of the mapper


@dataclass(frozen=True)
class Metrics:
    n: int
    precision: float
    recall: float
    accuracy: float                  # exact-set match rate
    abstention_rate: float           # fraction where the system returned nothing

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def score(pairs: list[tuple[tuple[str, ...], tuple[str, ...]]]) -> Metrics:
    """Score (predicted, expected) label sets. Deterministic micro-averaged precision/recall + exact-match
    accuracy + abstention rate."""
    tp = fp = fn = exact = abst = 0
    for pred, exp in pairs:
        ps, es = set(pred), set(exp)
        tp += len(ps & es); fp += len(ps - es); fn += len(es - ps)
        exact += (ps == es)
        abst += (len(ps) == 0)
    n = len(pairs)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return Metrics(n=n, precision=round(precision, 3), recall=round(recall, 3),
                   accuracy=round(exact / n, 3) if n else 0.0,
                   abstention_rate=round(abst / n, 3) if n else 0.0)


# A FROZEN labelled corpus for ATT&CK mapping (the gold split doesn't move; changing it is a deliberate act).
ATTACK_CORPUS: tuple[LabelledExample, ...] = (
    LabelledExample('crowdsecurity/ssh-bf', ('T1110', 'T1110.001'), rationale='SSH password guessing; ATT&CK T1110.001'),
    LabelledExample('crowdsecurity/ssh-slow-bf', ('T1110', 'T1110.001'), rationale='low-and-slow SSH password guessing, same technique'),
    LabelledExample('crowdsecurity/ssh-cve-2024-6387', ('T1190',), rationale='regreSSHion: unauthenticated RCE in public-facing sshd (CVE-2024-6387)'),
    LabelledExample('crowdsecurity/http-bf', ('T1110',), rationale='generic HTTP credential brute force'),
    LabelledExample('crowdsecurity/http-wordpress_bf', ('T1110',), rationale='WordPress login brute force'),
    LabelledExample('crowdsecurity/http-wordpress_bf_xmlrpc', ('T1110',), rationale='xmlrpc multicall credential brute force'),
    LabelledExample('crowdsecurity/http-wordpress-scan', ('T1595',), rationale='scanning for WordPress paths/plugins'),
    LabelledExample('crowdsecurity/http-generic-403-bf', ('T1595',), rationale='many 403s = forced browsing / wordlist enumeration, not credential guessing'),
    LabelledExample('crowdsecurity/http-crawl-non_statics', ('T1595',), rationale='aggressive crawling of dynamic endpoints (enumeration)'),
    LabelledExample('crowdsecurity/http-bad-user-agent', ('T1595',), rationale='known scanner/tool user-agents (Nikto, sqlmap, ...)'),
    LabelledExample('crowdsecurity/http-probing', ('T1595',), rationale='probing for non-existent paths'),
    LabelledExample('crowdsecurity/http-sensitive-files', ('T1595',), rationale='probing for .env/.git/config files'),
    LabelledExample('crowdsecurity/http-backdoors-attempts', ('T1595',), rationale='requests for known webshell paths: reconnaissance, not confirmed exploit'),
    LabelledExample('crowdsecurity/http-admin-interface-probing', ('T1595',), rationale='probing for admin consoles'),
    LabelledExample('crowdsecurity/http-open-proxy', ('T1595',), rationale='probing for open proxies'),
    LabelledExample('crowdsecurity/http-sqli-probing', ('T1190', 'T1595'), rationale='SQLi probes against a web app: exploitation attempt + scanning'),
    LabelledExample('crowdsecurity/http-xss-probing', ('T1190', 'T1595'), rationale='XSS probes against a web app'),
    LabelledExample('crowdsecurity/http-path-traversal-probing', ('T1190', 'T1595'), rationale='traversal probes against a web app'),
    LabelledExample('crowdsecurity/http-cve-2021-41773', ('T1190',), rationale='Apache 2.4.49 path traversal/RCE, public-facing app (CVE-2021-41773)'),
    LabelledExample('crowdsecurity/http-cve-2021-42013', ('T1190',), rationale='Apache 2.4.50 follow-up exploit'),
    LabelledExample('crowdsecurity/http-cve-2022-26134', ('T1190', 'T1059'), rationale='Confluence OGNL injection -> command execution'),
    LabelledExample('crowdsecurity/http-cve-2022-22965', ('T1190', 'T1059'), rationale='Spring4Shell -> RCE'),
    LabelledExample('crowdsecurity/apache_log4j2_cve-2021-44228', ('T1190', 'T1059'), rationale='Log4Shell JNDI -> RCE'),
    LabelledExample('crowdsecurity/grafana-cve-2021-43798', ('T1190',), rationale='Grafana path traversal in public app'),
    LabelledExample('crowdsecurity/jira_cve-2021-26086', ('T1190',), rationale='Jira file read in public app'),
    LabelledExample('crowdsecurity/f5-big-ip-cve-2020-5902', ('T1190',), rationale='F5 TMUI exploit on exposed management'),
    LabelledExample('crowdsecurity/http-resource-abuse', ('T1499',), rationale='excess requests to expensive endpoints = endpoint DoS'),
    LabelledExample('crowdsecurity/http-dos-swithcing-ua', ('T1499',), rationale='HTTP flood rotating UAs'),
    LabelledExample('crowdsecurity/nginx-req-limit-exceeded', ('T1499',), rationale='request-rate limit exceeded'),
    LabelledExample('crowdsecurity/port-scan', ('T1046',), rationale='TCP port sweep = network service discovery'),
    LabelledExample('crowdsecurity/iptables-scan-multi_ports', ('T1046',), rationale='multi-port scan seen by firewall'),
    LabelledExample('crowdsecurity/smb-bf', ('T1110',), rationale='SMB credential brute force'),
    LabelledExample('crowdsecurity/mysql-bf', ('T1110',), rationale='MySQL credential brute force'),
    LabelledExample('crowdsecurity/mssql-bf', ('T1110',), rationale='MSSQL credential brute force'),
    LabelledExample('crowdsecurity/telnet-bf', ('T1110',), rationale='telnet credential brute force'),
    LabelledExample('crowdsecurity/proftpd-bf', ('T1110',), rationale='FTP credential brute force'),
    LabelledExample('crowdsecurity/pureftpd-bf', ('T1110',), rationale='FTP credential brute force'),
    LabelledExample('acme/vpn-password-spray-bruteforce', ('T1110',), rationale='custom: brute force wording'),
    LabelledExample('acme/ssh-brute', ('T1110',), rationale='custom: brute force wording'),
    LabelledExample('acme/http-cve-2099-12345', ('T1190',), rationale='custom: unknown CVE on web service = exploitation of public-facing app'),
    LabelledExample('acme/api-sql-injection', ('T1190',), rationale='custom: injection against a web app'),
    LabelledExample('acme/web-rce', ('T1190', 'T1059'), rationale='custom: remote code execution via public app'),
    LabelledExample('acme/nmap-fingerprint', ('T1046',), rationale='custom: nmap = service discovery'),
    LabelledExample('acme/udp-flood', ('T1499',), rationale='custom: flooding'),
    LabelledExample('crowdsecurity/postfix-spam', (), rationale='mail spam is not an ATT&CK intrusion technique here'),
    LabelledExample('crowdsecurity/ssh-refused-conn', (), rationale='connection noise, no technique'),
    LabelledExample('crowdsecurity/unknown-noise', (), rationale='nothing should map'),
    LabelledExample('crowdsecurity/sshd-source-config', (), rationale="'source' must not trigger rce/force-style substrings"),
)


def evaluate_attack_mapping(corpus=ATTACK_CORPUS) -> Metrics:
    """Run the REAL deterministic scenario→technique mapper over the frozen corpus and score it."""
    pairs = []
    for ex in corpus:
        pred = tuple(t for t, _token in map_scenario_to_techniques(ex.input))
        pairs.append((pred, ex.expected))
    return score(pairs)


#: The frozen baseline result for ATT&CK mapping — a regression tripwire, recorded from the ACTUAL mapper
#: (not an aspiration). Labels were written by hand with rationale, independently of the mapper; the hub-name
#: entries are explicit table rows and the `acme/*` entries exercise the token fallback.
#: Recompute deliberately when the mapper changes.
ATTACK_BASELINE = {"n": 48, "precision": 1.0, "recall": 1.0, "accuracy": 1.0, "abstention_rate": 0.083}


# ────────── negative controls + false-positive regression (dev-time measurement) ──────────

#: Bump deliberately, in the same commit as any corpus change. A metric without a corpus version is not
#: comparable across runs: without it, "recall improved" and "someone deleted the hard cases" look the
#: same in the artifact. (PyRIT or any other generator may PRODUCE cases, offline; the corpus and the
#: gates below stay stdlib-only — the detector under test must not inherit a red-team framework's
#: dependency tree, and a generated case is worth exactly its recorded provenance.)
CORPUS_VERSION = "1"

#: Allowed provenance prefixes for a control. `manual:` = a human wrote it and says why; `observed:` = a
#: trace from a real case/ticket/seed; `generated:` = produced offline by a named tool + version.
_PROVENANCE_PREFIXES = ("manual:", "observed:", "generated:")


@dataclass(frozen=True)
class NegativeControl:
    """Something the system must NOT flag: benign behaviour shaped like something malicious.

    Positive cases only prove the detector fires; a detector that fires on everything scores 100% recall
    and is worth nothing. `source` is provenance and is load-bearing: :func:`assert_quality_gates`
    refuses to let an unsourced case count toward a green gate, because a control nobody can trace is a
    control someone will delete the first time it fails.
    """
    input: str
    why_benign: str
    source: str
    split: str = "test"


# Benign scenario names chosen to be NEAR misses against the token fallback — each one contains a token
# the mapper keys on, in a name whose meaning is the opposite of an attack. Written from the mapper's
# token table, not from what it currently returns (otherwise this file would be a screenshot of the code).
NEGATIVE_CONTROLS: tuple[NegativeControl, ...] = (
    NegativeControl("crowdsecurity/internal-vulnscanner-allowed",
                    "sanctioned internal vulnerability scanner on an allowlist", "manual:near-miss-token-scan"),
    NegativeControl("crowdsecurity/backup-agent-port-scan-benign",
                    "backup appliance enumerating its own targets; 'port-scan' is in the name",
                    "manual:near-miss-token-port-scan"),
    NegativeControl("crowdsecurity/nmap-allowlist-internal",
                    "explicit allowlist entry naming the scanner it permits", "manual:near-miss-token-nmap"),
    NegativeControl("crowdsecurity/ssh-bf-allowlist-bastion",
                    "allowlist for the bastion that legitimately retries keys; contains 'ssh-bf'",
                    "manual:near-miss-token-ssh-bf"),
    NegativeControl("crowdsecurity/modsec2-rule-updates",
                    "WAF ruleset feed traffic; 'c2' appears inside 'modsec2'", "manual:near-miss-substring-c2"),
    NegativeControl("crowdsecurity/source-view-access-log",
                    "read-only source browser; 'rce' appears inside 'source'", "manual:near-miss-substring-rce"),
    NegativeControl("crowdsecurity/http-do-not-crawl-policy",
                    "policy enforcement telemetry; contains 'crawl'", "manual:near-miss-token-crawl"),
    NegativeControl("crowdsecurity/passcvemgr-service",
                    "password CVE-reporting daemon name; not an exploit attempt", "manual:near-miss-adjacent-cve"),
    NegativeControl("crowdsecurity/postfix-spam",
                    "no honest ATT&CK analogue — the mapper must abstain, not guess",
                    "observed:evaluation.ATTACK_CORPUS abstention case"),
)

#: Benign RUNTIME PAYLOADS for the prompt-injection marker scan (the AI-security half). Same near-miss
#: rule: each carries a marker word in its ordinary, non-hostile meaning.
INJECTION_NEGATIVE_CONTROLS: tuple[NegativeControl, ...] = (
    NegativeControl('{"cache_bypass": true}', "config key for a cache-bypass toggle",
                    "manual:over-broad-marker-bypass"),
    NegativeControl('{"summary": "vendor patched a memory leak in the driver"}',
                    "release-note text about a resource leak", "manual:over-broad-marker-leak"),
    NegativeControl('{"note": "use a class override in the theme"}',
                    "front-end development note", "manual:over-broad-marker-override"),
    NegativeControl('{"feature": "developer mode toggle for QA"}',
                    "an app's own QA affordance, not a jailbreak", "manual:over-broad-marker-developer-mode"),
    NegativeControl('{"text": "the layout disregards the aspect ratio"}',
                    "ordinary English use of 'disregard'", "manual:over-broad-marker-disregard"),
    NegativeControl('{"route": "/api/v1/health", "method": "GET"}',
                    "an ordinary tool call with none of the markers", "manual:clean-baseline"),
)


def _scenario_tags(name: str) -> tuple[str, ...]:
    return tuple(t for t, _token in map_scenario_to_techniques(name))


def _scenario_basis(name: str) -> tuple[str, ...]:
    """The matched TOKENS behind a mapping — the actionable half of a false positive. `T1595` is what was
    claimed; `scan` is the rule to go and tighten."""
    return tuple(token for _t, token in map_scenario_to_techniques(name))


def _marker_tags(payload_json: str) -> tuple[str, ...]:
    """Which injection markers fire on a JSON payload string. Kept as tags so a false positive names the
    responsible marker — 'this control was flagged' is not actionable, 'marker `leak` flagged it' is."""
    # Same compiled patterns the detector itself uses — this function must never re-implement the match,
    # or the FP measurement would be measuring a copy of the detector instead of the detector.
    from .ai_security import _INJECTION_MARKERS, _INJECTION_RES   # local import: keeps the graph flat
    low = payload_json.lower()
    return tuple(m for m, rx in zip(_INJECTION_MARKERS, _INJECTION_RES) if rx.search(low))


#: Recorded false positives, as `control input -> token/marker responsible`. A LEDGER, not an approval
#: list: a false positive that is not named here FAILS the gate, and a ledger entry that no longer fires
#: fails too (a stale ledger hides a detector that got sharper than its paperwork). Each entry names the
#: responsible token, because "known false positive" without a token is not a thing anyone can go fix.
#:
#: Why these are carried instead of silenced: every one is an ordinary-English use of a real injection
#: verb. Removing the verb to silence them would drop recall on genuine jailbreak phrasings, and the
#: finding they produce is a 0.7-confidence human-reviewed finding, not an auto-mitigation. The ledger is
#: the honest middle: the number is measured, the token is named, the cost is visible.
FP_LEDGER: dict[str, str] = {
    '{"summary": "vendor patched a memory leak in the driver"}': "leak",
    '{"note": "use a class override in the theme"}': "override",
    '{"feature": "developer mode toggle for QA"}': "developer mode",
    "crowdsecurity/backup-agent-port-scan-benign": "port-scan",
    "crowdsecurity/nmap-allowlist-internal": "nmap",
    "crowdsecurity/ssh-bf-allowlist-bastion": "ssh-bf",
    "crowdsecurity/http-do-not-crawl-policy": "crawl",
}


def corpus_digest() -> str:
    """Content hash of the measured corpus (positives + controls + version). The artifact carries it so a
    changed number can be traced to a changed corpus instead of argued about."""
    payload = {
        "corpus_version": CORPUS_VERSION,
        "positives": [[e.input, list(e.expected), e.rationale] for e in ATTACK_CORPUS],
        "controls": [[c.input, c.why_benign, c.source] for c in NEGATIVE_CONTROLS],
        "injection_controls": [[c.input, c.why_benign, c.source] for c in INJECTION_NEGATIVE_CONTROLS],
        # The ledger is part of the claim: "0 unaccounted" means nothing without saying what was accounted.
        "fp_ledger": dict(sorted(FP_LEDGER.items())),
    }
    return sha256_hex(canonical_json(payload))


def evaluate_negative_controls(controls=NEGATIVE_CONTROLS, detector=_scenario_tags, basis_of=None) -> dict:
    """Flag-rate over negative controls. `detector(input) -> tags` — anything that returns a non-empty
    result on a benign control is a false positive, and the record names both the tags it produced and the
    tokens responsible (`basis_of`; the tags are their own basis when none is given)."""
    cases = []
    for c in controls:
        tags = tuple(detector(c.input))
        basis = tuple(basis_of(c.input)) if basis_of else tags
        if tags:
            cases.append({"input": c.input, "fired": list(tags), "source": c.source,
                          "why_benign": c.why_benign, "basis": list(basis or tags)})
    n = len(controls)
    return {"n": n, "false_positives": len(cases),
            "fp_rate": round(len(cases) / n, 3) if n else 0.0, "cases": cases}


class QualityGateError(Exception):
    """Raised when a measurement would be promoted without meeting its gates."""


def _provenance_failures(controls) -> list[str]:
    bad = []
    for c in controls:
        if not c.source.startswith(_PROVENANCE_PREFIXES):
            bad.append(f"control {c.input!r} has no traceable provenance (source={c.source!r})")
        if not c.why_benign.strip():
            bad.append(f"control {c.input!r} does not say why it is benign")
    return bad


def _ledger_check(block: dict, ledger: dict, controls) -> tuple[list[str], int]:
    """Split one lane's false positives into ledger-covered and unaccounted, and catch that lane's stale
    entries. `controls` is the lane's OWN control list: checking a lane against the whole ledger would
    report every other lane's entry as stale."""
    failures, unaccounted = [], 0
    for case in block["cases"]:
        if ledger.get(case["input"]) not in case["basis"]:
            unaccounted += 1
            failures.append(
                f"unaccounted false positive on benign control {case['input']!r} "
                f"(fired: {case['fired']}, responsible tokens: {case['basis']}); name the token in "
                f"FP_LEDGER or fix the rule")
    firing = {case["input"] for case in block["cases"]}
    for control in controls:
        if control.input in ledger and control.input not in firing:
            failures.append(f"stale FP_LEDGER entry for {control.input!r}: it no longer fires — "
                            f"remove it, the detector got sharper than the paperwork")
    return failures, unaccounted


def quality_report(*, mapping_baseline: dict = ATTACK_BASELINE,
                   max_unaccounted_fp: int = 0, fp_ledger: dict | None = None) -> dict:
    """The gate a detector change has to pass before its numbers mean anything — as data, without raising.

    Split from `assert_quality_gates` on purpose: CI has to publish the artifact on a RED build, and a
    gate that only exists as an exception cannot be reported. Checks, in order of how they get missed:
    (1) a control without traceable provenance, (2) any movement in the frozen ATT&CK mapping baseline,
    (3) a false positive FP_LEDGER does not name, (4) a ledger entry that no longer fires, (5) an
    FP_LEDGER key that matches no control (renamed control = silently un-accounted), (6) more unaccounted
    false positives than `max_unaccounted_fp`. A known false positive is allowed only as a named token
    with a reason; an unaccounted one is a broken measurement, not a pass with a note.
    """
    ledger = FP_LEDGER if fp_ledger is None else fp_ledger
    failures: list[str] = []

    controls = list(NEGATIVE_CONTROLS) + list(INJECTION_NEGATIVE_CONTROLS)
    failures += _provenance_failures(controls)
    known = {c.input for c in controls}
    failures += [f"FP_LEDGER key matches no control: {k!r} — a renamed or mistyped control silently "
                 f"stops being accounted for" for k in ledger if k not in known]

    mapping = evaluate_attack_mapping().to_dict()
    if mapping != mapping_baseline:
        failures.append(f"ATT&CK mapping moved off the frozen baseline: {mapping} != {mapping_baseline}")

    scenario_fp = evaluate_negative_controls(basis_of=_scenario_basis)
    injection_fp = evaluate_negative_controls(INJECTION_NEGATIVE_CONTROLS, _marker_tags)
    for block, lane_controls in ((scenario_fp, NEGATIVE_CONTROLS),
                                 (injection_fp, INJECTION_NEGATIVE_CONTROLS)):
        block_failures, block_unaccounted = _ledger_check(block, ledger, lane_controls)
        failures += block_failures
        block["unaccounted"] = block_unaccounted

    total_n = scenario_fp["n"] + injection_fp["n"]
    total_fp = scenario_fp["false_positives"] + injection_fp["false_positives"]
    unaccounted = scenario_fp["unaccounted"] + injection_fp["unaccounted"]
    fp_rate = round(total_fp / total_n, 3) if total_n else 0.0
    unaccounted_rate = round(unaccounted / total_n, 3) if total_n else 0.0
    if unaccounted > max_unaccounted_fp:
        failures.append(f"{unaccounted} unaccounted false positive(s) over {total_n} benign controls "
                        f"(unaccounted fp rate {unaccounted_rate}) — bound is {max_unaccounted_fp}")

    report = {"passed": not failures, "fp_rate": fp_rate, "unaccounted": unaccounted,
              "unaccounted_fp_rate": unaccounted_rate,
              "scenario": scenario_fp, "injection": injection_fp, "failures": failures}
    return report


def assert_quality_gates(**kw) -> dict:
    """`quality_report` as a gate. Deliberately not a mitigation: it blocks a claim, never takes an action."""
    report = quality_report(**kw)
    if not report["passed"]:
        raise QualityGateError("; ".join(report["failures"]))
    return report


def metrics_artifact(*, recorded_utc: str = "", baseline: dict = ATTACK_BASELINE) -> dict:
    """The versioned artifact a CI run publishes so the next run can diff it. Deterministic unless a
    timestamp is supplied — a metrics file that changes when nothing changed cannot carry a delta."""
    # One measurement, reported twice: the metrics and the gate come from the SAME run, so the artifact
    # cannot show a green number beside a red gate (or the reverse) because two code paths drifted.
    gate = quality_report(mapping_baseline=baseline)
    scenario_fp, injection_fp = gate["scenario"], gate["injection"]
    mapping = evaluate_attack_mapping().to_dict()
    return {
        "schema": "aos.edge-sentinel.evaluation/v1",
        "corpus_version": CORPUS_VERSION,
        "corpus_digest": corpus_digest(),
        "recorded_utc": recorded_utc,
        "mapping": {"metrics": mapping, "baseline": baseline, "on_baseline": mapping == baseline},
        "negative_controls": {"scenario": scenario_fp, "injection": injection_fp},
        "false_positive_rate": round((scenario_fp["false_positives"] + injection_fp["false_positives"])
                                     / (scenario_fp["n"] + injection_fp["n"]), 3),
        "fp_ledger": dict(sorted(FP_LEDGER.items())),
        "gate": {"passed": gate["passed"], "failures": gate["failures"]},
        "counts": {"positives": len(ATTACK_CORPUS),
                   "controls": len(NEGATIVE_CONTROLS) + len(INJECTION_NEGATIVE_CONTROLS),
                   "false_positives": scenario_fp["false_positives"] + injection_fp["false_positives"]},
        # Framing, because a rate whose denominator's shape is unstated is a lie waiting to be quoted:
        # these controls are adversarial near-misses BY CONSTRUCTION (benign input chosen because it
        # resembles a trigger). A 0.467 here is "how much of the look-alike surface fires", not a
        # production false-positive estimate, and anyone pasting this number must paste that sentence too.
        "controls_are": ("adversarial benign near-misses, deliberately shaped to resemble a detection "
                         "trigger; fp_rate measures the look-alike surface, not production traffic"),
        "read_as": ("mapping on baseline; "
                    f"{scenario_fp['false_positives'] + injection_fp['false_positives']} of "
                    f"{scenario_fp['n'] + injection_fp['n']} near-miss controls flagged, "
                    f"{scenario_fp['unaccounted'] + injection_fp['unaccounted']} unaccounted; gate "
                    + ("PASS" if gate["passed"] else f"FAIL ({len(gate['failures'])})")),
    }


def write_metrics_artifact(path, **kw) -> dict:
    """Write the artifact (creating parent dirs). Returns it so the caller can assert on what landed."""
    import json
    import pathlib
    art = metrics_artifact(**kw)
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(art, indent=2, sort_keys=True) + "\n")
    return art


def main(argv=None) -> int:
    """CI entry point: `python -m edge-sentinel.evaluation --out artifacts/edge-sentinel-evaluation.json`.

    Two deliberate choices. The artifact is written EVEN WHEN THE GATE FAILS — that is exactly when the
    numbers are needed, and a red build that also hides its metrics is a build nobody can triage. And the
    summary goes to stdout: a green check that does not show its numbers gets ignored by the next reader,
    which is how a coverage claim turns into folklore.
    """
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="python -m edge-sentinel.evaluation",
                                 description="Score the ATT&CK mapping and the negative-control "
                                             "false-positive gates; write the metrics artifact.")
    ap.add_argument("--out", default="artifacts/edge-sentinel-evaluation.json")
    args = ap.parse_args(argv)

    art = write_metrics_artifact(args.out)
    mapping, gate = art["mapping"]["metrics"], art["gate"]
    print(f"edge-sentinel evaluation [{art['corpus_version']}] digest={art['corpus_digest'][:12]} "
          f"n={mapping['n']} precision={mapping['precision']} recall={mapping['recall']} "
          f"abstain={mapping['abstention_rate']} fp_rate={art['false_positive_rate']} "
          f"controls={art['counts']['controls']} ledger={len(art['fp_ledger'])}")
    print(f"gate: {'PASS' if gate['passed'] else 'FAIL'}")
    for failure in gate["failures"]:
        print(f"  - {failure}", file=sys.stderr)
    print(f"wrote {args.out}")
    return 0 if gate["passed"] else 1


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(main())


# ──────────────────────────── bounded Learn (second) ────────────────────────────

def _learn():
    from discovery_runtime import learn
    return learn


# scopes a lesson is allowed to touch — strategy only. Anything gate/authorization-related is forbidden.
_FORBIDDEN_SCOPE_TOKENS = ("approval", "authorization", "authz", "gate", "policy", "permission",
                           "governance", "block", "publish", "validate")


class LearningScopeError(Exception):
    pass


def assert_strategy_only(context_signature: str) -> None:
    """A lesson may only be about STRATEGY (evidence ordering, enrichment/query choice, escalation). If its
    context signature names a gate/authorization concern, refuse — those are never learnable."""
    low = context_signature.lower()
    for tok in _FORBIDDEN_SCOPE_TOKENS:
        if tok in low:
            raise LearningScopeError(
                f"authorization/gate scope '{tok}' is not learnable — deterministic, not a lesson")


def strategy_experience(context_signature: str, strategy: str, *, good: bool, runtime_version: str = ""):
    """Record a strategy outcome as a discovery_runtime.learn Experience (after the scope guard)."""
    assert_strategy_only(context_signature)
    learn = _learn()
    return learn.observe(context_signature, "choose strategy", strategy,
                         learn.Outcome.GOOD if good else learn.Outcome.BAD, runtime_version=runtime_version)


def propose_strategy_lessons(experiences, *, min_support: int = 3):
    """Mine strategy experiences into candidate lessons via the SHARED learn lifecycle. Every candidate is
    a proposal that still needs review — a single outcome never becomes a rule. Returns learn.LearningCandidates."""
    learn = _learn()
    policy = learn.PromotionPolicy(min_support=min_support)
    out = []
    for pat in learn.mine(experiences):
        out.append(learn.propose(pat, f"Prefer strategy for {pat.scope}", policy=policy))
    return out

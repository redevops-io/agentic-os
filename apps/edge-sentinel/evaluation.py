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

from .attack import KIND_LABEL, KIND_OBSERVATION, adjudicate_scenario, map_scenario_to_techniques
from .authorization import Authorization, AuthorizationContext, NO_AUTHORIZATION
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

    `authorized` is the operator's inventory saying this source is permitted to do this activity, modelled
    as the real :class:`~edge-sentinel.authorization.AuthorizationContext` the detector receives in
    production. It is a SEPARATE field from `input` on purpose: the scanned string never grants permission
    to itself. Four of the original seven false positives were authorized activity misread as reconnaissance,
    and the naive fix — stand down on seeing `allowlist` in a name — is what an attacker would name a scan.
    """
    input: str
    why_benign: str
    source: str
    split: str = "test"
    authorized: tuple[Authorization, ...] = ()


# Benign scenario names chosen to be NEAR misses against the token fallback — each one contains a token
# the mapper keys on, in a name whose meaning is the opposite of an attack. Written from the mapper's
# token table, not from what it currently returns (otherwise this file would be a screenshot of the code).
#
# The four with `authorized=` are the authorized-reconnaissance shape: sanctioned activity the operator's
# inventory knows about. Their authorization is supplied the way production supplies it — as a typed object
# alongside the input — and each cites where it came from, because a suppression with no source is a
# coverage gap. Drop the context and they fire again (pinned by
# test_an_allowlist_word_in_the_name_grants_nothing), which is the whole point.
NEGATIVE_CONTROLS: tuple[NegativeControl, ...] = (
    NegativeControl("crowdsecurity/internal-vulnscanner-allowed",
                    "sanctioned internal vulnerability scanner on an allowlist", "manual:near-miss-token-scan"),
    NegativeControl("crowdsecurity/backup-agent-port-scan-benign",
                    "backup appliance enumerating its own targets; 'port-scan' is in the name",
                    "manual:near-miss-token-port-scan",
                    authorized=(Authorization(observable="crowdsecurity/backup-agent-port-scan-benign",
                                              token="port-scan", principal="backup-agent",
                                              source="imported:inventory/backup-cluster"),)),
    NegativeControl("crowdsecurity/nmap-allowlist-internal",
                    "explicit allowlist entry naming the scanner it permits", "manual:near-miss-token-nmap",
                    authorized=(Authorization(observable="crowdsecurity/nmap-allowlist-internal",
                                              token="nmap", principal="secops-vulnmgmt",
                                              source="manual:secops-scan-policy-2026"),)),
    NegativeControl("crowdsecurity/ssh-bf-allowlist-bastion",
                    "allowlist for the bastion that legitimately retries keys; contains 'ssh-bf'",
                    "manual:near-miss-token-ssh-bf",
                    authorized=(Authorization(observable="crowdsecurity/ssh-bf-allowlist-bastion",
                                              token="ssh-bf", principal="secops-bastion",
                                              source="imported:inventory/bastion-allowlist"),)),
    NegativeControl("crowdsecurity/modsec2-rule-updates",
                    "WAF ruleset feed traffic; 'c2' appears inside 'modsec2'", "manual:near-miss-substring-c2"),
    NegativeControl("crowdsecurity/source-view-access-log",
                    "read-only source browser; 'rce' appears inside 'source'", "manual:near-miss-substring-rce"),
    NegativeControl("crowdsecurity/http-do-not-crawl-policy",
                    "policy enforcement telemetry; contains 'crawl'", "manual:near-miss-token-crawl",
                    authorized=(Authorization(observable="crowdsecurity/http-do-not-crawl-policy",
                                              token="crawl", principal="webops",
                                              source="manual:robots-policy-enforcement"),)),
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


def _scenario_tags(name: str, *, authorization: AuthorizationContext | None = None,
                   kind: str = KIND_OBSERVATION) -> tuple[str, ...]:
    """Techniques this name may STILL claim after adjudication — i.e. what a finding is allowed to assert.
    `authorization` comes from the caller (the operator's inventory), never from `name`."""
    return adjudicate_scenario(name, kind=kind, authorization=authorization).tags


def _scenario_basis(name: str, *, authorization: AuthorizationContext | None = None,
                    kind: str = KIND_OBSERVATION) -> tuple[str, ...]:
    """The matched TOKENS behind a surviving mapping — the actionable half of a false positive. `T1595` is
    what was claimed; `scan` is the rule to go and tighten."""
    return adjudicate_scenario(name, kind=kind, authorization=authorization).tokens


def _scenario_raw_tags(name: str) -> tuple[str, ...]:
    """What the mapper saw before any authorization stood it down. Needed to keep suppressions in the
    numerator of an honest report: precision that only counts surviving claims is self-graded."""
    return tuple(t for t, _token in map_scenario_to_techniques(name))


#: What the near-miss scenario lane is measuring: hub feed NAMES, which are documents about detections, not
#: detections. That is not a convenience — an OBSERVATION is adjudicated against the source address the
#: alert came from (`attack.adjudicate_scenario(..., source_ip=)`), and a name has no address to check.
#: Measuring these controls as observations would test a thing that cannot happen and would demand a scope
#: no inventory row can answer. The observation path, where an authorization must survive the address test,
#: is exercised end to end in test_authorization_enforcement.py.
_SCENARIO_LANE_KIND = KIND_LABEL


def _authorization_of(controls) -> AuthorizationContext:
    """The lane's operator context, assembled from what its controls declare they run under."""
    entries = tuple(a for c in controls for a in getattr(c, "authorized", ()))
    return AuthorizationContext(entries) if entries else NO_AUTHORIZATION


def _marker_tags(payload_json: str) -> tuple[str, ...]:
    """Which injection markers earn a FINDING on a payload. Calls the detector — this function must never
    re-implement the match, or the FP measurement would measure a copy of the detector instead of it. The
    detector is now position-aware, and so the measurement is too, for free."""
    from .ai_security import _as_payload, injection_hits   # local import: keeps the graph flat
    return tuple(h.marker for h in injection_hits(_as_payload(payload_json)) if h.finding_worthy)


def _marker_mentions(payload_json: str) -> tuple[str, ...]:
    """Markers the scan SAW and stood down as prose. Measured because a downgrade nobody counts is
    indistinguishable from a detector that quietly stopped looking."""
    from .ai_security import _as_payload, injection_hits
    return tuple(h.marker for h in injection_hits(_as_payload(payload_json)) if not h.finding_worthy)


def _markers_seen(payload_json: str) -> tuple[str, ...]:
    """Every marker occurrence in the payload, finding-worthy or not — the number BEFORE position and
    grammar decide anything."""
    from .ai_security import _as_payload, injection_hits
    return tuple(h.marker for h in injection_hits(_as_payload(payload_json)))
#: Recorded false positives, as `control input -> token/marker responsible`. A LEDGER, not an approval
#: list: a false positive that is not named here FAILS the gate, and a ledger entry that no longer fires
#: fails too (a stale ledger hides a detector that got sharper than its paperwork). Each entry names the
#: responsible token, because "known false positive" without a token is not a thing anyone can fix.
#:
#: EMPTY — and empty here is the result of a change, not the absence of one. The seven entries this held
#: closed two ways: the scenario four by AUTHORIZED context handed to the detector as data (the operator's
#: inventory, never a word found inside the scanned name), the injection three by WHERE the text sits in a
#: payload plus what the verb acts on. No marker and no scenario token was removed, and recall on the 48
#: positives is still 1.0. Both mechanisms are counted in the artifact — suppressions and downgraded
#: mentions — because "zero false positives" bought by not looking is the exact failure this file exists
#: to catch.
#:
#: An empty ledger is also an empty allowance: the next false positive has nowhere to hide, and the ceiling
#: below is what turns that into a build failure instead of a conversation.
FP_LEDGER: dict[str, str] = {}

#: The ledger is a RATCHET THAT ONLY TURNS ONE WAY. Every entry in it is a detection that fires on benign
#: input, so an improvement is measured by the ledger getting shorter — and a new acknowledged false
#: positive must be a visible line in the diff (bumping this constant), never the quiet cost of a rule that
#: got broader. Fixing one means deleting its entry AND lowering this number in the same change; CI then
#: proves the shrink instead of taking it on trust. It is a ceiling, not a target: the target is 0, and it
#: reached it with the corpus unchanged, which is the only way the number means anything.
FP_LEDGER_CEILING = 0


def corpus_digest() -> str:
    """Content hash of the measured corpus (positives + controls + version). The artifact carries it so a
    changed number can be traced to a changed corpus instead of argued about."""
    def row(c):
        # The authorizations a control runs under are part of the corpus: four controls went from false
        # positive to clean by being handed context, so a digest that ignored the context could not tell
        # "the detector improved" from "someone quietly deleted the allowlist entries".
        auths = [sorted(a.record().items()) for a in
                 sorted(c.authorized, key=lambda a: (a.observable, a.token))]
        return [c.input, c.why_benign, c.source, auths]

    payload = {
        "corpus_version": CORPUS_VERSION,
        "positives": [[e.input, list(e.expected), e.rationale] for e in ATTACK_CORPUS],
        "controls": [row(c) for c in NEGATIVE_CONTROLS],
        "injection_controls": [row(c) for c in INJECTION_NEGATIVE_CONTROLS],
        # The ledger is part of the claim: "0 unaccounted" means nothing without saying what was accounted.
        "fp_ledger": dict(sorted(FP_LEDGER.items())),
    }
    return sha256_hex(canonical_json(payload))


def evaluate_negative_controls(controls=None, detector=None, basis_of=None, *,
                               authorization=None, raw_of=None, mention_of=None) -> dict:
    """Flag-rate over negative controls. `detector(input) -> tags` — anything that still returns a non-empty
    result on a benign control is a false positive, and the record names both the tags it produced and the
    tokens responsible (`basis_of`; the tags are their own basis when none is given).

    Two extra counts, because a detector that fires on nothing after adjudication has to prove it still sees
    the input: `raw_of` names what the rules produced BEFORE context/position (`suppressed`), and
    `mention_of` names what was seen and stood down (`mentions`). A clean rate with both at zero is a
    different instrument from a clean rate with them at seven."""
    # Resolved at call time, not bound as a default: a default captures the list as it was at import, so a
    # test that patches NEGATIVE_CONTROLS to add a hard case would measure the OLD corpus and pass anyway.
    controls = NEGATIVE_CONTROLS if controls is None else controls
    ctx = authorization if authorization is not None else _authorization_of(controls)

    def default_detector(name: str) -> tuple[str, ...]:
        return _scenario_tags(name, authorization=ctx, kind=_SCENARIO_LANE_KIND)

    def default_basis(name: str) -> tuple[str, ...]:
        return _scenario_basis(name, authorization=ctx, kind=_SCENARIO_LANE_KIND)

    detector = detector or default_detector
    basis_of = basis_of or default_basis
    cases, suppressed, mentions = [], 0, 0
    for c in controls:
        tags = tuple(detector(c.input))
        basis = tuple(basis_of(c.input)) if basis_of else tags
        if tags:
            cases.append({"input": c.input, "fired": list(tags), "source": c.source,
                          "why_benign": c.why_benign, "basis": list(basis or tags)})
        elif raw_of is not None and tuple(raw_of(c.input)):
            suppressed += 1                       # the rule fired; an authorization stood it down
        elif mention_of is not None and tuple(mention_of(c.input)):
            mentions += 1                         # the scan saw the word and read it as prose
    n = len(controls)
    return {"n": n, "false_positives": len(cases),
            "fp_rate": round(len(cases) / n, 3) if n else 0.0,
            "suppressed": suppressed, "mentions": mentions, "cases": cases}


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
                   max_unaccounted_fp: int = 0, fp_ledger: dict | None = None,
                   authorization: AuthorizationContext | None = None) -> dict:
    """The gate a detector change has to pass before its numbers mean anything — as data, without raising.

    Split from `assert_quality_gates` on purpose: CI has to publish the artifact on a RED build, and a
    gate that only exists as an exception cannot be reported. Checks, in order of how they get missed:
    (1) a control without traceable provenance, (2) any movement in the frozen ATT&CK mapping baseline,
    (3) a false positive FP_LEDGER does not name, (4) a ledger entry that no longer fires, (5) an
    FP_LEDGER key that matches no control (renamed control = silently un-accounted), (6) more unaccounted
    false positives than `max_unaccounted_fp`. A known false positive is allowed only as a named token
    with a reason; an unaccounted one is a broken measurement, not a pass with a note.

    A seventh check is a ratchet rather than a correctness test: the ledger may not grow past
    `FP_LEDGER_CEILING`. It is here because the honest-looking failure mode of this whole apparatus is a
    PR that broadens a rule, adds a ledger line for the fallout, and reports gate PASS.

    An eighth covers the other new escape hatch. Authorizations are evidence: every entry has to stand
    SOMETHING down, or it is paperwork about a rule that no longer fires and it should be deleted — the same
    rule that keeps FP_LEDGER honest, applied to the mechanism that could otherwise hollow it out. Pass
    `authorization=NO_AUTHORIZATION` to measure the detector without them; the false positives come back,
    and with an empty ledger they are unaccounted, which is the point.
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

    ctx = authorization if authorization is not None else _authorization_of(controls)
    # Hub scenario names ARE labels — documents about detections. Saying so changes what a verdict may be
    # used for (advisory vs evidenced), and changes nothing about whether it is counted here.
    verdicts = [adjudicate_scenario(c.input, kind=_SCENARIO_LANE_KIND, authorization=ctx)
                for c in NEGATIVE_CONTROLS]
    scenario_fp = evaluate_negative_controls(NEGATIVE_CONTROLS, raw_of=_scenario_raw_tags,
                                             authorization=ctx)
    injection_fp = evaluate_negative_controls(INJECTION_NEGATIVE_CONTROLS, _marker_tags,
                                              mention_of=_marker_mentions)
    for block, lane_controls in ((scenario_fp, NEGATIVE_CONTROLS),
                                 (injection_fp, INJECTION_NEGATIVE_CONTROLS)):
        block_failures, block_unaccounted = _ledger_check(block, ledger, lane_controls)
        failures += block_failures
        block["unaccounted"] = block_unaccounted

    standing = {m.authorization.key for v in verdicts for m in v.matches if m.suppressed}
    for entry in ctx.entries:
        if entry.key not in standing:
            failures.append(f"authorization for {entry.observable!r} / {entry.token!r} stands nothing down: "
                            f"either it names a control that no longer exists or the rule moved under it — "
                            f"delete it, an unearned suppression is a hole with paperwork attached")

    total_n = scenario_fp["n"] + injection_fp["n"]
    total_fp = scenario_fp["false_positives"] + injection_fp["false_positives"]
    unaccounted = scenario_fp["unaccounted"] + injection_fp["unaccounted"]
    fp_rate = round(total_fp / total_n, 3) if total_n else 0.0
    unaccounted_rate = round(unaccounted / total_n, 3) if total_n else 0.0
    if unaccounted > max_unaccounted_fp:
        failures.append(f"{unaccounted} unaccounted false positive(s) over {total_n} benign controls "
                        f"(unaccounted fp rate {unaccounted_rate}) — bound is {max_unaccounted_fp}")
    if len(ledger) > FP_LEDGER_CEILING:
        failures.append(f"FP_LEDGER holds {len(ledger)} acknowledged false positive(s), over its ceiling "
                        f"of {FP_LEDGER_CEILING}: the ledger only shrinks. A new acknowledged false "
                        f"positive has to be argued into FP_LEDGER_CEILING in the same diff, next to the "
                        f"rule that caused it.")

    report = {"passed": not failures, "fp_rate": fp_rate, "unaccounted": unaccounted,
              "unaccounted_fp_rate": unaccounted_rate,
              "acknowledged": total_fp - unaccounted, "total_false_positives": total_fp,
              "controls": total_n, "ledger_size": len(ledger), "ledger_ceiling": FP_LEDGER_CEILING,
              # What the clean rate is made of. `raw_false_positives` is what the rules alone do to this
              # corpus with no context and no position reading — the number the previous release published
              # as its headline. `suppressed` + `mentions` is the distance between the two, per mechanism,
              # and `suppressions` names who signed for each one. A reader who only looks at `fp_rate` can
              # still be told the truth by the two fields beside it.
              "raw_false_positives": (sum(1 for c in NEGATIVE_CONTROLS if _scenario_raw_tags(c.input))
                                      + sum(1 for c in INJECTION_NEGATIVE_CONTROLS
                                            if _markers_seen(c.input))),
              "suppressed": scenario_fp["suppressed"] + injection_fp["suppressed"],
              "mentions": scenario_fp["mentions"] + injection_fp["mentions"],
              "suppressions": [r for v in verdicts for r in v.suppressions],
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
    total_controls = scenario_fp["n"] + injection_fp["n"]
    firing_rate = round(gate["total_false_positives"] / total_controls, 3) if total_controls else 0.0
    on_baseline = mapping == baseline
    return {
        # v2: the detector block now separates what the RULES do from what survives ADJUDICATION, and the
        # corpus carries operator authorizations. A consumer reading v1 keys will not misread v2 values —
        # a `false positives: 0` that means "nothing fired" and a `0` that means "context stood it down"
        # are different claims, and the schema says which artifact it is.
        "schema": "aos.edge-sentinel.evaluation/v2",
        "corpus_version": CORPUS_VERSION,
        "corpus_digest": corpus_digest(),
        "recorded_utc": recorded_utc,
        "mapping": {"metrics": mapping, "baseline": baseline, "on_baseline": on_baseline},
        "negative_controls": {"scenario": scenario_fp, "injection": injection_fp},
        # Named for its denominator on purpose. A bare "false_positive_rate" beside a 1.0 recall invites
        # "46.7% of detection is wrong", which is not what this says: the denominator is 15 adversarial
        # near-misses chosen BECAUSE they resemble a trigger, not observed traffic.
        "adversarial_control_firing_rate": firing_rate,
        "fp_ledger": dict(sorted(FP_LEDGER.items())),
        "gate": {"passed": gate["passed"], "failures": gate["failures"]},
        # THREE NUMBERS, NEVER ONE. A green gate proves the MEASUREMENT is honest — nothing fires on benign
        # input without being named, the corpus is sourced, the mapping has not drifted. It says nothing
        # about whether the DETECTOR is good, and one PASS is exactly how those two get confused. `harness`
        # is what the gate defends; `detector` is the open backlog. Read the second block as the work
        # remaining, never as an approval.
        "claims": {
            "harness": {                                    # does the measurement mean anything?
                "positive_recall": mapping["recall"],
                "positive_precision": mapping["precision"],
                "positive_cases": mapping["n"],
                "unaccounted_false_positives": gate["unaccounted"],
                "mapping_on_baseline": on_baseline,
            },
            "detector": {                                   # is the detector any good?
                "acknowledged_false_positives": gate["acknowledged"],
                "of_adversarial_controls": total_controls,
                "adversarial_control_firing_rate": firing_rate,
                "fp_ledger_size": len(FP_LEDGER),
                "fp_ledger_ceiling": FP_LEDGER_CEILING,     # fix one: delete its entry AND lower this
                # …and how the rate got to where it is. Without these three, a 0.0 cannot be told apart
                # from a scan that stopped looking: the first number is the raw rule surface, the next two
                # are the distance from there to here, split by mechanism.
                "raw_adversarial_firing_rate": (round(gate["raw_false_positives"] / total_controls, 3)
                                                if total_controls else 0.0),
                "suppressed_by_authorization": gate["suppressed"],
                "downgraded_to_mention": gate["mentions"],
                "suppressions": gate["suppressions"],       # who authorized what, and from where
            },
        },
        "counts": {"positives": len(ATTACK_CORPUS),
                   "controls": len(NEGATIVE_CONTROLS) + len(INJECTION_NEGATIVE_CONTROLS),
                   "false_positives": gate["total_false_positives"],
                   "acknowledged_false_positives": gate["acknowledged"],
                   "unaccounted_false_positives": gate["unaccounted"]},
        # Framing, because a rate whose denominator's shape is unstated is a lie waiting to be quoted:
        # these controls are adversarial near-misses BY CONSTRUCTION (benign input chosen because it
        # resembles a trigger). A 0.467 here is "how much of the look-alike surface fires", not a
        # production false-positive estimate, and anyone pasting this number must paste that sentence too.
        "controls_are": ("adversarial benign near-misses, deliberately shaped to resemble a detection "
                         "trigger; adversarial_control_firing_rate measures the look-alike surface, not "
                         "production traffic"),
        "read_as": ("harness: recall " + str(mapping["recall"]) + f" on {mapping['n']} positives, "
                    + f"{gate['unaccounted']} unaccounted false positives, mapping "
                    + ("on baseline; " if on_baseline else "OFF baseline; ")
                    + "detector: " + str(gate["acknowledged"]) + f" of {total_controls} near-miss "
                    + "controls flagged, from a raw rule surface of "
                    + str(gate["raw_false_positives"]) + f"/{total_controls} "
                    + f"({gate['suppressed']} authorized, {gate['mentions']} prose mentions) "
                    + f"(ledger {len(FP_LEDGER)}/{FP_LEDGER_CEILING}); gate "
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
    harness, detector = art["claims"]["harness"], art["claims"]["detector"]
    print(f"edge-sentinel evaluation [{art['corpus_version']}] digest={art['corpus_digest'][:12]} "
          f"controls={art['counts']['controls']}")
    # Two lines, never merged into one verdict: the first is the measurement, the second is the detector.
    print(f"  harness:  recall={harness['positive_recall']} (n={harness['positive_cases']}) "
          f"precision={harness['positive_precision']} abstain={mapping['abstention_rate']} "
          f"unaccounted_fp={harness['unaccounted_false_positives']}")
    print(f"  detector: acknowledged_fp={detector['acknowledged_false_positives']}"
          f"/{detector['of_adversarial_controls']} "
          f"adversarial_firing_rate={detector['adversarial_control_firing_rate']} "
          f"ledger={detector['fp_ledger_size']}/{detector['fp_ledger_ceiling']}")
    # …and the mechanism behind the rate, printed next to it. 0.0 with seven suppressions and three
    # downgraded mentions is a different instrument than 0.0 with nothing happening, and the log is where a
    # reviewer actually looks.
    print(f"            raw_rule_surface={detector['raw_adversarial_firing_rate']} "
          f"({detector['suppressed_by_authorization']} authorized, "
          f"{detector['downgraded_to_mention']} mentions)")
    print(f"gate: {'PASS' if gate['passed'] else 'FAIL'}  "
          f"(PASS = the measurement is honest, not that the detector is precise)")
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

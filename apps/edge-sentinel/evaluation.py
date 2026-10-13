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

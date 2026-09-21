"""Bounded investigation + hunt: hypothesis state, evidence sufficiency, and query validation (Phase 3).

Three deterministic guarantees the plan's acceptance demands:
  * **Multi-source evidence, or abstain.** A hypothesis is only SUPPORTED/CONFIRMED when corroborated by
    evidence from more than one source type; a single-source or unsupported conclusion resolves to
    INSUFFICIENT (abstain), never to a confident claim. Contradiction is first-class.
  * **Queries validate before execution.** Every hunt query is checked to be read-only and well-formed
    *before* it may run; a query that writes/mutates (or declares itself non-read-only) is rejected. The
    hunt runner refuses to "execute" anything that did not pass validation.
  * **Bounded roles.** Investigator/Hunter are typed operators, not autonomous authorities — nothing here
    takes an action; it produces findings, negative results, and validated (not executed) queries.

No model in the loop for the gates — sufficiency and query safety are pure functions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from .evidence import sha256_hex


class HypothesisStatus(str, Enum):
    PROPOSED = "PROPOSED"; INVESTIGATING = "INVESTIGATING"; SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"; INSUFFICIENT = "INSUFFICIENT"; CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class Role(str, Enum):
    INVESTIGATOR = "INVESTIGATOR"; HUNTER = "HUNTER"


@dataclass(frozen=True)
class Hypothesis:
    statement: str
    supporting_obs: tuple[str, ...] = ()      # SecurityObservation ids
    contradicting_obs: tuple[str, ...] = ()
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    min_sources: int = 2                        # multi-source requirement to leave 'insufficient'

    @property
    def hypothesis_id(self) -> str:
        return f"hyp-{sha256_hex(self.statement)[:16]}"

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["hypothesis_id"] = self.hypothesis_id
        d["status"] = self.status.value
        d["supporting_obs"] = list(self.supporting_obs)
        d["contradicting_obs"] = list(self.contradicting_obs)
        return d


def _source_types(store, obs_ids) -> set[str]:
    return {store.observations[o].source_type for o in obs_ids if o in store.observations}


def assess_hypothesis(hyp: Hypothesis, store) -> Hypothesis:
    """Resolve a hypothesis's status from the evidence in the store. Deterministic and conservative:
    default is to ABSTAIN (INSUFFICIENT) unless multi-source support clearly outweighs contradiction."""
    from dataclasses import replace
    support_sources = _source_types(store, hyp.supporting_obs)
    n_support = len([o for o in hyp.supporting_obs if o in store.observations])
    n_contra = len([o for o in hyp.contradicting_obs if o in store.observations])

    if n_support == 0:
        status = HypothesisStatus.INSUFFICIENT                      # nothing supports it → abstain
    elif n_contra > n_support:
        status = HypothesisStatus.CONTRADICTED
    elif len(support_sources) >= hyp.min_sources and n_support > n_contra:
        # multi-source corroboration with net support → strong enough to stand
        status = HypothesisStatus.CONFIRMED if n_contra == 0 else HypothesisStatus.SUPPORTED
    else:
        status = HypothesisStatus.INSUFFICIENT                      # single-source / weak → abstain
    return replace(hyp, status=status)


# ──────────────────────────── query contracts + validation ────────────────────────────

class QueryError(Exception):
    """Raised when a hunt tries to run a query that did not pass validation."""


@dataclass(frozen=True)
class Query:
    backend: str                                # crowdsec | wazuh | sql | splunk | ...
    text: str
    read_only: bool = True
    fields: tuple[str, ...] = ()

    @property
    def query_id(self) -> str:
        return f"q-{sha256_hex(self.backend + '|' + self.text)[:16]}"


# Verbs/keywords that mutate state — matched as WHOLE WORDS so an innocent field like `process_create`
# or `post_count` is not flagged (word boundaries treat '_' as a word char).
_WRITE_WORDS = ("delete", "drop", "insert", "update", "truncate", "alter", "create", "exec", "execute",
                "remove", "ban", "block", "grant", "revoke", "shutdown", "kill", "rm", "sudo",
                "unlink", "modify", "set")
_WRITE_RE = re.compile(r"\b(" + "|".join(_WRITE_WORDS) + r")\b", re.IGNORECASE)
# SQL-comment / stacked-statement injection markers (substring is correct here).
_INJECTION = (";--", "; --", "/*", "*/", "xp_")


def validate_query(q: Query) -> tuple[bool, str]:
    """Pure read-only/well-formedness check run BEFORE execution. No side effects, no model. Matches
    mutating verbs as whole words and rejects stacked-statement/comment injection markers."""
    if not q.text or not q.text.strip():
        return False, "empty query"
    if not q.read_only:
        return False, "query is declared not read-only"
    if not q.backend:
        return False, "no backend"
    m = _WRITE_RE.search(q.text)
    if m:
        return False, f"non-read-only token rejected: '{m.group(1).lower()}'"
    low = q.text.lower()
    for tok in _INJECTION:
        if tok in low:
            return False, f"injection marker rejected: '{tok.strip()}'"
    return True, "ok"


@dataclass
class HuntResult:
    hypothesis: Hypothesis
    validated_queries: list = field(default_factory=list)
    rejected_queries: list = field(default_factory=list)   # (Query, reason)
    ran: list = field(default_factory=list)
    negative_result: bool = False


def run_hunt(hyp: Hypothesis, queries, store, *, executor=None) -> HuntResult:
    """Bounded hunt: validate every query, run ONLY the ones that pass (via an injected read-only
    ``executor``; None = dry-run), then re-assess the hypothesis. A query that fails validation is never
    passed to the executor. Returns a HuntResult with a negative_result flag when nothing corroborates."""
    result = HuntResult(hypothesis=hyp)
    for q in queries:
        ok, reason = validate_query(q)
        if not ok:
            result.rejected_queries.append((q, reason))
            continue
        result.validated_queries.append(q)
        if executor is not None:
            # the executor MUST be read-only; validation already guaranteed the query is.
            result.ran.append(executor(q))
    assessed = assess_hypothesis(hyp, store)
    result.hypothesis = assessed
    result.negative_result = assessed.status in (HypothesisStatus.INSUFFICIENT,
                                                 HypothesisStatus.CONTRADICTED)
    return result

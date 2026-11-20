"""Operator-side authorization context — the only input allowed to stand a scenario match down (Phase 9b).

Why this is a TYPE and not a rule inside the matcher. Four of the ledgered false positives are names of
things an operator has *sanctioned*: `crowdsecurity/nmap-allowlist-internal`,
`crowdsecurity/ssh-bf-allowlist-bastion`, a backup agent's own port scan, a do-not-crawl policy feed. The
obvious fix — stand down when the scanned name contains `allowlist`, `benign` or `internal` — is an
evasion grant, not a fix: an attacker who wants a scan to pass names it `nmap-allowlist-internal`, because
that is exactly what the operator's inventory would call it. Authorization therefore never comes from the
scanned string. It comes in through this object, from the inventory/CMDB the operator owns, and the matcher
receives it as a separate argument it cannot derive from its own input.

Two properties this file is responsible for:

* **Exact observable identity.** `observable` is matched after normalisation, never as a substring. An
  authorization for `…/nmap-allowlist-internal` says nothing about `…/nmap-external-scan`.
* **Provenance, or it is not an authorization.** Every entry names a `principal` (who owns the activity)
  and a `source` (`imported:cmdb`, `manual:secops-ticket-452`, `observed:case-118`). A suppression that
  cannot say where it came from is not a suppression, it is a coverage gap wearing a badge — the same rule
  `evaluation.py` applies to a negative control.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Same discipline as evaluation._PROVENANCE_PREFIXES, on purpose: an authorization is evidence.
SOURCE_PREFIXES = ("manual:", "observed:", "imported:")


class AuthorizationError(ValueError):
    """A declared authorization is unusable — unsourced, unnamed principal, or empty identity."""


def _normalize(observable: str) -> str:
    """Inventory identity, normalised. Deliberately conservative: whitespace and case are not identity,
    everything else is. No fuzzy matching, no tokenising, no substring — those are how an authorization
    ends up covering something nobody signed for."""
    return " ".join((observable or "").strip().lower().split())


@dataclass(frozen=True)
class Authorization:
    """`principal` is permitted to do `token`-class activity as `observable`, per `source`."""
    observable: str                       # the id the inventory uses, e.g. "crowdsecurity/nmap-allowlist-internal"
    token: str                            # the scenario token it authorises, e.g. "nmap"
    principal: str                        # who owns the activity — never "anyone"
    source: str                           # where the declaration came from; provenance, not a comment
    note: str = ""

    def __post_init__(self) -> None:
        if not _normalize(self.observable):
            raise AuthorizationError("authorization needs an observable identity to cover")
        if not self.token.strip():
            raise AuthorizationError(f"authorization for {self.observable!r} names no activity token")
        if not self.principal.strip() or _normalize(self.principal) in {"anyone", "everybody", "all", "*"}:
            raise AuthorizationError(f"authorization for {self.observable!r} has no accountable principal")
        if not self.source.startswith(SOURCE_PREFIXES):
            raise AuthorizationError(
                f"authorization for {self.observable!r} is unsourced: source must start with "
                f"{'/'.join(SOURCE_PREFIXES)} — an unsourced suppression is a coverage gap, not a decision")

    @property
    def key(self) -> tuple[str, str]:
        return _normalize(self.observable), _normalize(self.token)

    def record(self) -> dict:
        """The auditable form. A suppression has to be reportable with who signed for it and where the
        declaration came from, or the ledger of suppressions is as fake as an empty false-positive list."""
        return {"observable": self.observable, "token": self.token,
                "principal": self.principal, "source": self.source}


@dataclass(frozen=True)
class AuthorizationContext:
    """The operator's declarations, as supplied to one assessment. Immutable, and empty is the default:
    no context means nothing is authorized, which is the safe reading of an unconfigured deployment."""
    entries: tuple[Authorization, ...] = ()

    def cover(self, *, observable: str, token: str) -> Authorization | None:
        want = (_normalize(observable), _normalize(token))
        for entry in self.entries:
            if entry.key == want:                       # exact identity on BOTH axes
                return entry
        return None

    def observables(self) -> tuple[str, ...]:
        return tuple(sorted({e.observable for e in self.entries}))

    def __len__(self) -> int:
        return len(self.entries)


NO_AUTHORIZATION = AuthorizationContext()
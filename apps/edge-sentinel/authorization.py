"""Operator-side authorization context — the only input allowed to stand a scenario match down (Phase 9b).

Why this is a TYPE and not a rule inside the matcher. Four of the ledgered false positives are names of
things an operator has *sanctioned*: `crowdsecurity/nmap-allowlist-internal`,
`crowdsecurity/ssh-bf-allowlist-bastion`, a backup agent's own port scan, a do-not-crawl policy feed. The
obvious fix — stand down when the scanned name contains `allowlist`, `benign` or `internal` — is an
evasion grant, not a fix: an attacker who wants a scan to pass names it `nmap-allowlist-internal`, because
that is exactly what the operator's inventory would call it. Authorization therefore never comes from the
scanned string. It comes in through this object, from the inventory/CMDB the operator owns, and the matcher
receives it as a separate argument it cannot derive from its own input.

Three properties this file is responsible for:

* **Exact observable identity.** `observable` is matched after normalisation, never as a substring. An
  authorization for `…/nmap-allowlist-internal` says nothing about `…/nmap-external-scan`.
* **Provenance, or it is not an authorization.** Every entry names a `principal` (who owns the activity)
  and a `source` (`imported:cmdb`, `manual:secops-ticket-452`, `observed:case-118`). A suppression that
  cannot say where it came from is not a suppression, it is a coverage gap wearing a badge — the same rule
  `evaluation.py` applies to a negative control.
* **Source scope, once the input is an observation.** An inventory entry that says "our vuln manager runs
  nmap" is not a statement that nmap traffic from any address on earth is sanctioned. So an authorization
  also carries the addresses the activity is authorized FROM, and a stand-down on a live alert requires the
  alert's source to be inside that scope. Without this axis the contract would be a self-inflicted evasion:
  one allowlist entry would silence that scenario for every source in the corpus, which is strictly worse
  than the seven false positives it was introduced to remove.

The last property is why `KIND_OBSERVATION` and `KIND_LABEL` are checked differently (see `attack.py`): a
hub feed name has no source address, an alert does. Failing to know the source of an observation is never a
reason to stand down — it is a reason not to.
"""
from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

#: Same discipline as evaluation._PROVENANCE_PREFIXES, on purpose: an authorization is evidence.
SOURCE_PREFIXES = ("manual:", "observed:", "imported:")

#: The operator config this comes from. A file path, not an inline blob: an authorization list is inventory
#: data that a configuration management system owns, and it must be reviewable, diffable and mountable
#: without anyone being able to reach it over an API.
CONFIG_VAR = "SENTINEL_AUTHORIZATIONS_FILE"
CONFIG_VERSION = 1
_TOP_FIELDS = ("version", "authorizations")
_ENTRY_FIELDS = ("observable", "token", "principal", "source", "note", "expires_at", "source_scope")

#: Why a declared authorization did NOT apply. Reported, never silent: an authorization that silently fails
#: to apply reads as a rule that refuses to fire, and the operator goes and loosens the rule.
DENY_NONE = ""
DENY_NO_ENTRY = "no_entry"
DENY_EXPIRED = "expired"
DENY_NO_SCOPE = "no_source_scope"
DENY_OUT_OF_SCOPE = "source_out_of_scope"


class AuthorizationError(ValueError):
    """A declared authorization is unusable — unsourced, unnamed principal, empty identity, an unparseable
    expiry, or an address scope that cannot be read."""


def _normalize(observable: str) -> str:
    """Inventory identity, normalised. Deliberately conservative: whitespace and case are not identity,
    everything else is. No fuzzy matching, no tokenising, no substring — those are how an authorization
    ends up covering something nobody signed for."""
    return " ".join((observable or "").strip().lower().split())


def _instant(value, what: str) -> datetime | None:
    """`expires_at` as an instant, never a day.

    A bare date is read as 00:00:00Z of the date named, so "expires 2026-12-01" means the authorization is
    dead for the whole of 2026-12-01. Reading it as end-of-day would be the friendlier guess and the more
    dangerous one: an expiry the operator believes has passed is still live for another 24 hours. A value
    that does not parse is an error rather than a permanent authorization — a typo in an expiry date must
    never silently turn a reviewable grant into an unlimited one.
    """
    s = str(value or "").strip()
    if not s:
        return None
    if len(s) == 10:                                   # YYYY-MM-DD -> midnight Z
        try:
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise AuthorizationError(f"{what} is not a date: {s!r} (expected YYYY-MM-DD or a full ISO-8601 instant)") from None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise AuthorizationError(f"{what} is not an ISO-8601 instant: {s!r}") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _nets(values, what: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """The addresses an activity is authorized from. A bare address is its own /32 (or /128). The universe
    is not a scope: `0.0.0.0/0` is the source-side version of principal "anyone" and is refused for the
    same reason."""
    if isinstance(values, str):
        values = [v for v in values.replace(",", " ").split() if v]
    nets = []
    for v in values or ():
        s = str(v).strip()
        if not s:
            continue
        try:
            net = ipaddress.ip_network(s if "/" in s else f"{s}/32" if ":" not in s else f"{s}/128",
                                       strict=False)
        except ValueError:
            raise AuthorizationError(f"{what} has an address scope that is not an IP or CIDR: {s!r}") from None
        if net.prefixlen == 0:
            raise AuthorizationError(f"{what} scopes to {net}, which is every address: an authorization that "
                                     "covers all sources is a rule deletion, not an authorization")
        nets.append(net)
    return tuple(nets)


@dataclass(frozen=True)
class Authorization:
    """`principal` is permitted to do `token`-class activity as `observable`, from `source_scope`, per `source`,
    until `expires_at`."""
    observable: str                       # the id the inventory uses, e.g. "crowdsecurity/nmap-allowlist-internal"
    token: str                            # the scenario token it authorises, e.g. "nmap"
    principal: str                        # who owns the activity — never "anyone"
    source: str                           # where the declaration came from; provenance, not a comment
    note: str = ""
    expires_at: str = ""                  # an instant; empty = no expiry (say so out loud, do not imply it)
    source_scope: tuple[str, ...] = ()    # addresses the activity is authorized from; empty = cannot cover an observation

    def __post_init__(self) -> None:
        scope = self.source_scope
        if isinstance(scope, str):                       # "10.0.4.0/24, 10.0.9.9" is a legitimate JSON shape
            scope = [v for v in scope.replace(",", " ").split() if v]
        elif isinstance(scope, dict):
            raise AuthorizationError(f"authorization for {self.observable!r} has a source_scope that is not a list of addresses")
        if not isinstance(scope, tuple):
            object.__setattr__(self, "source_scope", tuple(str(v) for v in (scope or ())))
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
        # Validated at declaration, not at match time: an unusable entry must fail where it is written.
        _instant(self.expires_at, f"authorization for {self.observable!r}")
        _nets(self.source_scope, f"authorization for {self.observable!r}")

    @property
    def key(self) -> tuple[str, str]:
        return _normalize(self.observable), _normalize(self.token)

    def expiry(self) -> datetime | None:
        return _instant(self.expires_at, f"authorization for {self.observable!r}")

    def is_expired(self, now: datetime | None = None) -> bool:
        """Checked at match time, not at load time: a long-lived process must not keep honouring a grant
        that expired an hour after it started."""
        exp = self.expiry()
        return exp is not None and (now or _now()) >= exp

    def source_denial(self, source_ip: str | None) -> str:
        """DENY_* for the address axis: empty string means the scope does not object."""
        nets = _nets(self.source_scope, f"authorization for {self.observable!r}")
        if not nets:
            return DENY_NO_SCOPE          # nothing is asserted about where this activity comes from
        if source_ip is None:
            return DENY_NO_SCOPE          # an observation whose source we cannot name is not covered
        try:
            addr = ipaddress.ip_address(str(source_ip).split("%")[0])
        except ValueError:
            return DENY_OUT_OF_SCOPE      # an unparseable source is an unauthorized source, never an unknown one
        return DENY_NONE if any(addr.version == n.version and addr in n for n in nets) else DENY_OUT_OF_SCOPE

    def record(self) -> dict:
        """The auditable form. A suppression has to be reportable with who signed for it and where the
        declaration came from, or the ledger of suppressions is as fake as an empty false-positive list."""
        return {"observable": self.observable, "token": self.token,
                "principal": self.principal, "source": self.source,
                "expires_at": self.expires_at, "source_scope": list(self.source_scope)}


@dataclass(frozen=True)
class AuthorizationContext:
    """The operator's declarations, as supplied to one assessment. Immutable, and empty is the default:
    no context means nothing is authorized, which is the safe reading of an unconfigured deployment."""
    entries: tuple[Authorization, ...] = ()

    def check(self, *, observable: str, token: str, source_ip: str | None = None,
              require_scope: bool = False, now: datetime | None = None) -> tuple[Authorization | None, str]:
        """(the authorization that applies, why none does). `require_scope` is set by callers handling an
        OBSERVATION — something that happened at a place. A LABEL (a hub feed name, an inventory row) has no
        source address, so scope cannot and must not apply to it."""
        want = (_normalize(observable), _normalize(token))
        reason = DENY_NO_ENTRY
        for entry in self.entries:
            if entry.key != want:                       # exact identity on BOTH axes
                continue
            if entry.is_expired(now):
                reason = reason if reason != DENY_NO_ENTRY else DENY_EXPIRED
                continue
            if require_scope:
                denial = entry.source_denial(source_ip)
                if denial:
                    reason = reason if reason != DENY_NO_ENTRY else denial
                    continue
            return entry, DENY_NONE
        return None, reason

    def cover(self, *, observable: str, token: str, source_ip: str | None = None,
              require_scope: bool = False, now: datetime | None = None) -> Authorization | None:
        return self.check(observable=observable, token=token, source_ip=source_ip,
                          require_scope=require_scope, now=now)[0]

    def observables(self) -> tuple[str, ...]:
        return tuple(sorted({e.observable for e in self.entries}))

    def stats(self, now: datetime | None = None) -> dict:
        """Config hygiene, not per-match state: what a deployment granted, and how much of it is stale or
        too vague to ever apply to an alert."""
        return {"entries": len(self.entries),
                "expired": sum(1 for e in self.entries if e.is_expired(now)),
                "without_source_scope": sum(1 for e in self.entries if not e.source_scope)}

    def __len__(self) -> int:
        return len(self.entries)


NO_AUTHORIZATION = AuthorizationContext()


# ──────────────────────── the operator config seam ────────────────────────
#
# `config_contract.json` documents SENTINEL_AUTHORIZATIONS_FILE; this is the only place that reads it.
# The shape is deliberately small and closed:
#
#   {"version": 1, "authorizations": [
#      {"observable": "crowdsecurity/backup-agent-port-scan-benign", "token": "port-scan",
#       "principal": "backup-agent", "source": "imported:inventory/backup-cluster",
#       "source_scope": ["10.0.4.9"], "expires_at": "2027-01-31"}]}
#
# Unknown keys are rejected rather than ignored. `expire_at` for `expires_at` is the whole argument: read
# leniently it means "no expiry", so a typo converts a grant the operator believes expires in January into
# a permanent one.

@dataclass(frozen=True)
class AuthorizationConfig:
    """What the deployment's authorization file currently contributes. `status` is never inferred from the
    entry count: 'unconfigured' (no file named) and 'invalid' (a file that could not be read) both suppress
    nothing and must still be distinguishable in a health report."""
    status: str                                     # unconfigured | ok | unreadable | invalid
    detail: str = ""
    path: str = ""
    context: AuthorizationContext = field(default=NO_AUTHORIZATION)

    @property
    def usable(self) -> bool:
        return self.status == "ok"

    def health(self) -> dict:
        """For /api/v1/info (an operator-gated read). Deliberately does NOT list the observables or
        principals: which sanctioned activity a deployment has allowlisted is a map of what an attacker can
        ignore, and a read role is not the audience for that. Counts and failure reasons are."""
        out = {"status": self.status, "source": f"env:{CONFIG_VAR}", "file": self.path,
               **self.context.stats()}
        if self.detail:
            out["detail"] = self.detail
        return out


def _invalid(path: str, status: str, detail: str) -> AuthorizationConfig:
    return AuthorizationConfig(status=status, detail=detail, path=path, context=NO_AUTHORIZATION)


def load_authorization(env=None) -> AuthorizationConfig:
    """Read the operator's authorization file. Every failure mode returns an EMPTY context: a broken,
    missing or unreadable configuration may only ever make the detector louder, never quieter — and it says
    so in `status`/`detail`, because a silently ignored allowlist is an operator whose sanctioned scanners
    start getting banned with nothing in the health report to explain it."""
    env = os.environ if env is None else env
    path = str(env.get(CONFIG_VAR, "") or "").strip()
    if not path:
        return _invalid("", "unconfigured", f"{CONFIG_VAR} is not set: nothing is authorized, every detection stands")
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        return _invalid(path, "unreadable",
                        f"{CONFIG_VAR} names a file that cannot be read ({e.strerror or type(e).__name__}): "
                        "nothing is authorized")
    try:
        doc = json.loads(raw)
    except ValueError as e:
        return _invalid(path, "invalid", f"authorization file is not valid JSON ({e}): nothing is authorized")
    if not isinstance(doc, dict) or set(doc) - set(_TOP_FIELDS):
        return _invalid(path, "invalid",
                        f"authorization file must be an object with only these keys {list(_TOP_FIELDS)}: "
                        "nothing is authorized")
    if doc.get("version") != CONFIG_VERSION:
        return _invalid(path, "invalid",
                        f"unsupported authorization version {doc.get('version')!r} (expected {CONFIG_VERSION}): "
                        "nothing is authorized")
    rows = doc.get("authorizations")
    if not isinstance(rows, list):
        return _invalid(path, "invalid", "authorizations must be a list: nothing is authorized")
    entries: list[Authorization] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            return _invalid(path, "invalid", f"authorization #{i} is not an object: nothing is authorized")
        if set(row) - set(_ENTRY_FIELDS):
            return _invalid(path, "invalid",
                            f"authorization #{i} has unknown keys {sorted(set(row) - set(_ENTRY_FIELDS))} "
                            f"(allowed: {list(_ENTRY_FIELDS)}): nothing is authorized")
        try:
            entries.append(Authorization(**{k: v for k, v in row.items() if k in _ENTRY_FIELDS}))
        except (AuthorizationError, TypeError) as e:
            return _invalid(path, "invalid", f"authorization #{i} is unusable ({e}): nothing is authorized")
    return AuthorizationConfig(status="ok", path=path, context=AuthorizationContext(tuple(entries)))


_CACHE: dict[str, tuple[tuple, AuthorizationConfig]] = {}


def authorization_config(env=None) -> AuthorizationConfig:
    """`load_authorization` with the file's identity as the cache key, so a request path does not re-read
    and re-parse the inventory on every alert while an edit on disk still takes effect on the next check.
    Statting a small file per check is cheap; caching a stale allowlist after an operator tightened it is
    not."""
    path = str((os.environ if env is None else env).get(CONFIG_VAR, "") or "").strip()
    if not path:
        return load_authorization(env)
    try:
        st = Path(path).stat()
        identity: tuple = (st.st_mtime_ns, st.st_size)
    except OSError:
        _CACHE.pop(path, None)
        return load_authorization(env)
    hit = _CACHE.get(path)
    if hit is not None and hit[0] == identity:
        return hit[1]
    cfg = load_authorization(env)
    _CACHE[path] = (identity, cfg)
    return cfg


def deployment_authorization(env=None) -> AuthorizationContext:
    """The context a production call site should use when its caller did not supply one. Empty on any
    problem, which is the same as 'unconfigured deployment': every detection that ever fired still fires."""
    return authorization_config(env).context


def reset_cache() -> None:
    """Test hook: drop the stat-keyed cache."""
    _CACHE.clear()
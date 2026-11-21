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

import hashlib
import ipaddress
import json
import os
import re
import stat as _stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

#: The address of a grant and the address of an alert are compared, and that comparison decides whether a
#: ban is proposed. It is only worth anything if BOTH sides are read the way the ban target is read — as
#: canonical text, with host bits in a CIDR refused and a mapped/NAT64/6to4 IPv6 judged by the IPv4 it
#: carries — because "the address that was stood down" and "the address the edge banned" being different
#: strings is the same defect this file exists to prevent (a scenario name that means two things to two
#: components), relocated onto the source axis.
#:
#: So: use the executor's own parser when it is importable, and where it is not, apply the SAME rule written
#: out below rather than the permissive default of `ipaddress`. Both branches refuse the texts a firewall
#: ban would refuse; they are not the same code, and that is not hidden — `address_rule()` names which one
#: decided, and `/api/v1/info` reports it, because a vendored tree shipping a different `ipguard` must not
#: end up with quietly different authorization semantics. (The vendored `agentic-os-stack` copy really does:
#: it exposes `check_block_target` and refuses IPv4-mapped forms outright.)
try:
    from .ipguard import BlockRefused as _EdgeRefused
    from .ipguard import _embedded_v4 as _edge_carried_v4
    from .ipguard import parse_target as _edge_parse
except Exception:  # pragma: no cover - a vendored subset ships a different ipguard, or none
    _edge_parse = None
    _edge_carried_v4 = None

    class _EdgeRefused(Exception):
        pass

#: Canonical dotted-quad: no leading zeros (`010.0.4.9` is an authorization for a host nobody typed), and
#: a prefix that is a length, never a netmask dressed up as one (`203.0.113.0/255.255.255.0` parses fine in
#: `ipaddress` and is not text a ban target parser accepts).
_V4_CANON = r"(?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){3}"
_PREFIX = r"(?:[0-9]|[1-9][0-9]|1[0-1][0-9]|12[0-8])"

#: Same discipline as evaluation._PROVENANCE_PREFIXES, on purpose: an authorization is evidence.
SOURCE_PREFIXES = ("manual:", "observed:", "imported:")

#: The operator config this comes from. A file path, not an inline blob: an authorization list is inventory
#: data that a configuration management system owns, and it must be reviewable, diffable and mountable
#: without anyone being able to reach it over an API.
CONFIG_VAR = "SENTINEL_AUTHORIZATIONS_FILE"
CONFIG_VERSION = 1
_TOP_FIELDS = ("version", "authorizations")
_ENTRY_FIELDS = ("observable", "token", "principal", "source", "note", "expires_at", "source_scope")

#: A cap, not a tuning knob. An authorization list is a handful of reviewed inventory rows; a file large
#: enough to be a problem is not an allowlist anyone read. Read in full on every check (see
#: `authorization_config`), so the cap is what keeps a mistake on disk from becoming a stall on the path
#: that ingests alerts — and exceeding it fails CLOSED, because "too big to read" must never mean "read as
#: empty and nobody noticed".
MAX_CONFIG_BYTES = 1 << 20

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


def address_rule() -> str:
    """Which reader decided an address identity in this process. Reported, not assumed: the two trees this
    app ships in carry different ban-target guards, and an authorization model whose address semantics
    depend on which one is installed has to say so out loud."""
    return "ipguard.parse_target" if _edge_parse is not None else "canonical-text rule (ipguard not importable)"


def _canonical(text: str):
    """`ipguard.parse_target`'s discipline, spelled out for the case where that parser is not importable.

    Strict where `ipaddress` is permissive, and permissive about nothing: canonical dotted-quad, canonical
    compressed IPv6, a prefix that is a length rather than a netmask, and host bits refused. The failure it
    is guarding against is `ipaddress`'s leniency: `strict=False` masks a CIDR down over a block of
    addresses, and a dotted netmask or a leading-zero octet parses happily there and nowhere else."""
    s = text
    if not s or len(s) > 64:
        return None
    addr, sep, plen = s.partition("/")
    if ":" not in addr:
        if not re.fullmatch(_V4_CANON, addr):      # 010.0.4.9 is not an address anyone meant to type
            return None
        try:
            ip = ipaddress.IPv4Address(addr)       # the pattern allows 999.999.999.999; the range does not
        except ValueError:
            return None
        width = 32
    else:
        try:
            ip = ipaddress.IPv6Address(addr)
        except ValueError:
            return None
        if addr != str(ip):                        # 2001:0db8::1 is not how that address writes itself
            return None
        width = 128
    bits = width
    if sep:
        if not re.fullmatch(_PREFIX, plen):        # /255.255.255.0 is a netmask, not a length
            return None
        bits = int(plen)
    try:
        return ipaddress.ip_network(f"{ip}/{bits}", strict=True)
    except ValueError:
        return None                                # host bits set: the scope would read wider than written


def _identity(raw, *, expand: bool = False) -> tuple:
    """The canonical network(s) a piece of address text denotes, read the way a ban target is read.

    Empty tuple means the text is not an address the edge would act on — which is always an answer that
    suppresses nothing, never an unknown that gets the benefit of the doubt.

    `expand` is set for the OBSERVATION's source and not for a declared scope, and that asymmetry is the
    safe direction: an alert seen as `::ffff:203.0.113.7` is the same host to the edge as `203.0.113.7`, so
    a grant for the address it carries must apply (otherwise a sanctioned scanner gets banned for the
    accident of how its address was written). Expanding a *grant* instead would let one odd spelling cover
    addresses under a second identity, which is widening, and widening is not what the observation needs.
    """
    s = str(raw if raw is not None else "").strip().split("%")[0].strip()   # %zone is interface scope, not identity
    if not s:
        return ()
    if _edge_parse is not None:
        try:
            net = _edge_parse(s)
        except (_EdgeRefused, ValueError):
            return ()
    else:
        net = _canonical(s)
        if net is None:
            return ()
    forms = [net]
    if expand and net.version == 6 and net.prefixlen == net.max_prefixlen:
        carried = _carried_v4(net.network_address)
        if carried is not None:
            forms.append(ipaddress.ip_network(carried))
    return tuple(forms)


#: The IPv6 forms that carry an IPv4 address, mirrored from ipguard for the same reason as the rule above:
#: the comparison must not depend on which ban-target guard happens to be installed.
_MAPPED = ipaddress.ip_network("::ffff:0:0/96")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIXTOFOUR = ipaddress.ip_network("2002::/16")


def _carried_v4(addr: ipaddress.IPv6Address):
    if _edge_carried_v4 is not None:
        return _edge_carried_v4(addr)
    if addr in _MAPPED or addr in _NAT64:
        return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    if addr in _SIXTOFOUR:
        return ipaddress.IPv4Address((int(addr) >> 80) & 0xFFFFFFFF)
    return None


def canonical_source(source_ip) -> str:
    """The address a stand-down was decided on, in the form a ban would name. For the audit trail: a
    suppression that quotes a string the edge would never have recognised is not attributable."""
    forms = _identity(source_ip)
    return str(forms[0].network_address) if forms else ""


def _nets(values, what: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """The addresses an activity is authorized from. A bare address is its own /32 (or /128).

    Two refusals, both about a scope meaning more than the operator wrote:
    * `0.0.0.0/0` — the universe is the source-side version of principal "anyone".
    * a CIDR with host bits set, e.g. `203.0.113.7/26`. `ipaddress(..., strict=False)` silently masks it to
      `203.0.113.0/26`, so a grant written for one host quietly authorises 64 of them. The edge refuses that
      text as a ban target, so authorising it would mean standing down addresses no ban could ever name.
    """
    if isinstance(values, str):
        values = [v for v in values.replace(",", " ").split() if v]
    nets = []
    for v in values or ():
        s = str(v).strip()
        if not s:
            continue
        forms = _identity(s)
        if not forms:
            raise AuthorizationError(f"{what} has an address scope that is not a canonical IP or CIDR: {s!r} "
                                     "(written as the edge would accept it: dotted-quad, no host bits in a CIDR)")
        net = forms[0]
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
        forms = _identity(source_ip, expand=True)
        if not forms:
            return DENY_OUT_OF_SCOPE      # an unparseable source is an unauthorized source, never an unknown one
        # subnet_of, not `in`: the thing being authorized is the address the alert names, and a grant covers
        # it only if every address in the alert's own network is inside the grant. An alert whose source is a
        # /24 is therefore not stood down by a grant for one address inside that /24.
        covered = any(f.version == n.version and f.subnet_of(n) for f in forms for n in nets)
        return DENY_NONE if covered else DENY_OUT_OF_SCOPE

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
    status: str                                     # unconfigured | ok | unreadable | invalid | unsafe_permissions
    detail: str = ""
    path: str = ""
    context: AuthorizationContext = field(default=NO_AUTHORIZATION)
    warnings: tuple[str, ...] = ()                   # usable-but-unsafe things, e.g. who may write the file
    digest: str = ""                                 # the exact bytes in force, so "did my edit land?" has an answer

    @property
    def usable(self) -> bool:
        return self.status == "ok"

    def health(self) -> dict:
        """For /api/v1/info (an operator-gated read). Deliberately does NOT list the observables or
        principals: which sanctioned activity a deployment has allowlisted is a map of what an attacker can
        ignore, and a read role is not the audience for that. Counts and failure reasons are."""
        out = {"status": self.status, "source": f"env:{CONFIG_VAR}", "file": self.path,
               **self.context.stats()}
        out["address_rule"] = address_rule()          # which reader decided identities in this process
        if self.detail:
            out["detail"] = self.detail
        if self.digest:
            out["content_digest"] = self.digest[:12]
        if self.warnings:
            out["warnings"] = list(self.warnings)
        return out


def _invalid(path: str, status: str, detail: str, warnings: tuple[str, ...] = ()) -> AuthorizationConfig:
    return AuthorizationConfig(status=status, detail=detail, path=path, context=NO_AUTHORIZATION,
                               warnings=tuple(warnings))


def _parse(raw: str, path: str, warnings: tuple[str, ...] = (), digest: str = "") -> AuthorizationConfig:
    """The document, validated. Every failure mode returns an EMPTY context: a broken, missing or unreadable
    configuration may only ever make the detector louder, never quieter — and it says so in `status`/`detail`,
    because a silently ignored allowlist is an operator whose sanctioned scanners start getting banned with
    nothing in the health report to explain it."""
    try:
        doc = json.loads(raw)
    except ValueError as e:
        return _invalid(path, "invalid", f"authorization file is not valid JSON ({e}): nothing is authorized", warnings)
    if not isinstance(doc, dict) or set(doc) - set(_TOP_FIELDS):
        return _invalid(path, "invalid",
                        f"authorization file must be an object with only these keys {list(_TOP_FIELDS)}: "
                        "nothing is authorized", warnings)
    if doc.get("version") != CONFIG_VERSION:
        return _invalid(path, "invalid",
                        f"unsupported authorization version {doc.get('version')!r} (expected {CONFIG_VERSION}): "
                        "nothing is authorized", warnings)
    rows = doc.get("authorizations")
    if not isinstance(rows, list):
        return _invalid(path, "invalid", "authorizations must be a list: nothing is authorized", warnings)
    entries: list[Authorization] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            return _invalid(path, "invalid", f"authorization #{i} is not an object: nothing is authorized", warnings)
        if set(row) - set(_ENTRY_FIELDS):
            return _invalid(path, "invalid",
                            f"authorization #{i} has unknown keys {sorted(set(row) - set(_ENTRY_FIELDS))} "
                            f"(allowed: {list(_ENTRY_FIELDS)}): nothing is authorized", warnings)
        try:
            entries.append(Authorization(**{k: v for k, v in row.items() if k in _ENTRY_FIELDS}))
        except (AuthorizationError, TypeError) as e:
            return _invalid(path, "invalid", f"authorization #{i} is unusable ({e}): nothing is authorized", warnings)
    return AuthorizationConfig(status="ok", path=path, context=AuthorizationContext(tuple(entries)),
                               warnings=tuple(warnings), digest=digest)


def _permissions_check(path: str) -> tuple[tuple[str, ...], str]:
    """(group-writable warnings, world-writable rejection detail or "").

    Group-writable only warns: the file may legitimately be group-writable on a shared CM host, where "the
    group" is a controlled set of operators or tooling, so the health report says it and loading proceeds.
    World-writable is not that case — mode 0o002 means any unrelated local account on the box, not just the
    owning group, can rewrite the file — which is exactly what requiring a named principal and a provenance
    string was supposed to make impossible. A warning that never limits who can write is not a control, so
    this is refused outright rather than merely reported."""
    warns: list[str] = []
    rejects: list[str] = []
    target = Path(path)
    for label, p in (("the authorization file", target), ("the directory holding it", target.parent)):
        try:
            mode = _stat.S_IMODE(os.stat(p).st_mode)
        except OSError:
            continue
        if mode & 0o002:
            rejects.append(f"{label} is world-writable (mode {mode:o}): any local account could add a suppression")
        elif mode & 0o020:
            warns.append(f"{label} is group-writable (mode {mode:o}): anyone in the owning group can add a suppression")
    return tuple(warns), "; ".join(rejects)


def _slurp(path: str) -> tuple[str | None, str, tuple[str, ...], str]:
    """(the text, why-not, warnings, status-on-failure). Read whole and decoded here so that the bytes that
    were hashed are the bytes that were parsed — a re-read further down could see a different file than the
    one digested. A world-writable file or directory is rejected before the bytes are even opened: its
    content doesn't matter if any unrelated local account can rewrite it out from under the check."""
    warns, reject = _permissions_check(path)
    if reject:
        return None, reject, (), "unsafe_permissions"
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        return None, f"names a file that cannot be read ({e.strerror or type(e).__name__})", warns, "unreadable"
    if len(data) > MAX_CONFIG_BYTES:
        return None, f"is {len(data)} bytes, over the {MAX_CONFIG_BYTES}-byte cap", warns, "unreadable"
    try:
        return data.decode("utf-8"), "", warns, "ok"
    except UnicodeDecodeError as e:
        return None, f"is not UTF-8 text ({e.reason})", warns, "unreadable"


def load_authorization(env=None) -> AuthorizationConfig:
    """Read the operator's authorization file, now, uncached. See `_parse` for what it refuses."""
    env = os.environ if env is None else env
    path = str(env.get(CONFIG_VAR, "") or "").strip()
    if not path:
        return _invalid("", "unconfigured", f"{CONFIG_VAR} is not set: nothing is authorized, every detection stands")
    raw, why, warns, status = _slurp(path)
    if raw is None:
        return _invalid(path, status, f"{CONFIG_VAR} {why}: nothing is authorized", warns)
    return _parse(raw, path, warns)


_CACHE: dict[str, tuple[str, AuthorizationConfig]] = {}


def authorization_config(env=None) -> AuthorizationConfig:
    """`load_authorization` with the file's CONTENT digest as the cache key.

    Deliberately not `(mtime, size)`, which is the efficient answer and not a complete one. An edit that
    leaves the length unchanged — one allowlisted address replaced by another of the same length — keeps the
    size, and a tool that restores the old timestamp (a configuration-management apply, `cp -p`, an image
    rebuild from a layer cache) keeps the mtime. The key would match, and the process would carry on
    honouring a grant the operator had just revoked. For ordinary configuration a stale value is an
    annoyance; here the stale direction is the dangerous one — a cache that never misses is a revocation
    that never lands. So the key is sha256 of the bytes, and the bytes are read on every check: a file
    bounded by MAX_CONFIG_BYTES costs less than being wrong about who is still authorized."""
    env = os.environ if env is None else env
    path = str(env.get(CONFIG_VAR, "") or "").strip()
    if not path:
        return load_authorization(env)
    raw, why, warns, status = _slurp(path)
    if raw is None:
        _CACHE.pop(path, None)
        return _invalid(path, status, f"{CONFIG_VAR} {why}: nothing is authorized", warns)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    hit = _CACHE.get(path)
    if hit is not None and hit[0] == digest and hit[1].warnings == warns:
        return hit[1]
    cfg = _parse(raw, path, warns, digest)
    _CACHE[path] = (digest, cfg)
    return cfg


def deployment_authorization(env=None) -> AuthorizationContext:
    """The context a production call site should use when its caller did not supply one. Empty on any
    problem, which is the same as 'unconfigured deployment': every detection that ever fired still fires."""
    return authorization_config(env).context


def reset_cache() -> None:
    """Test hook: drop the stat-keyed cache."""
    _CACHE.clear()
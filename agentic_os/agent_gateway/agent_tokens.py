"""One token per external agent, with its own grants, expiry and revocation.

The mounted gateway had a single static bearer shared by every holder: no identity per agent, so no per-agent grant, audit
attribution or revocation. This is the :class:`~agentic_os.agent_gateway.auth.TokenVerifier` that gives each agent its own.

On a deployment with tenants (``AGENTIC_OS_TENANT_LIMITS``) an agent reads only what its token's ``tenant`` is granted
there (see ``scope.py``). ``dashboards.read`` is not a default grant: it must be named.

The operator names a JSON file in ``AGENT_GATEWAY_AGENTS_FILE``. It stores only the SHA-256 of each token:

    {"schema": "aos.agent-gateway.agents/v1",
     "agents": [{"id": "claude-laptop", "token_sha256": "…", "grants": ["projects.read"], "expires_at": 1830000000,
                 "tenant": "default", "label": "…", "revoked": false, "rate_per_minute": 30}]}

    python -m agentic_os.agent_gateway.agent_tokens issue  agents.json --id claude-laptop --grant projects.read --days 30
    python -m agentic_os.agent_gateway.agent_tokens issue  agents.json --id demo-visitor-1 --grant projects.read --minutes 60 --rate 30
    python -m agentic_os.agent_gateway.agent_tokens revoke agents.json --id claude-laptop
    python -m agentic_os.agent_gateway.agent_tokens list   agents.json

Under ``AGENTIC_OS_PROFILE=production`` the gateway is fail-closed on authority as well: every call needs a stored
authority record for that agent. ``issue`` writes one with ``--authority-db`` (default ``AGENTIC_OS_AUTHORITY_DB``, the
store the gateway reads): the agent's read capabilities only, its tenant, the token's own end time and an action budget.
``revoke`` with the same store ends it too.

The file is read again whenever it changes, so a revocation takes effect on the next call. Everything fails closed: a file
that cannot be read or does not validate authenticates nobody; an entry with a grant outside the allowed set, a missing or
malformed field, a past expiry, or a duplicate id or token authenticates nobody.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, FrozenSet, List, Optional

from agentic_os.overlays import Principal

from .app_record_capabilities import GRANTS as APP_GRANTS_TEXT
from .economics_capabilities import GRANTS as ECONOMICS_GRANTS
from .media_capabilities import GRANTS as MEDIA_GRANTS
from .contracts import GatewayPrincipal

SCHEMA = "aos.agent-gateway.agents/v1"
#: what an agent may be granted here. The mounted gateway exposes reads only; a write grant would grant nothing and is
#: refused, so that a file cannot appear to give an agent something it cannot have.
READ_GRANTS: FrozenSet[str] = frozenset({"projects.read", "missions.read", "sources.read"})
#: read grants that expose business figures. Never part of a default set: a token has one only when the operator names it.
NAMED_GRANTS: FrozenSet[str] = frozenset({"dashboards.read",
                                          # the integrated products' tools (product_capabilities.py); two of these stage or start work
                                          "security.read", "security.propose", "quality.read", "quality.run",
                                          # ERP, support and CRM: record lookups and proposals (app_record_capabilities.py)
                                          *APP_GRANTS_TEXT,
                                          # economic intelligence and reconciliation (economics_capabilities.py)
                                          *ECONOMICS_GRANTS,
                                          # video, images and social posts (media_capabilities.py)
                                          *MEDIA_GRANTS})
ALLOWED_GRANTS: FrozenSet[str] = READ_GRANTS | NAMED_GRANTS
TOKEN_PREFIX = "aga_"
MAX_DAYS = 366
MAX_MINUTES = 24 * 60                 # a lifetime given in minutes is for a temporary identity: at most a day
MAX_RATE = 600                        # capability calls a minute
MAX_ACTIONS = 5000                    # calls one authority record allows over its life
_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = {"id", "token_sha256", "grants", "expires_at", "tenant", "label", "revoked", "created_at", "rate_per_minute"}
_log = logging.getLogger("agentic_os.agent_gateway.agents")


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _entries(doc: Any) -> List[Dict[str, Any]]:
    """The valid entries of a file. A broken FILE raises; a broken ENTRY is left out (and named), so one mistake does not
    lock every other agent out - while an ambiguous file (two entries with one id or one token) authenticates nobody."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or not isinstance(doc.get("agents"), list):
        raise ValueError(f"not an agents file (schema {SCHEMA})")
    out: List[Dict[str, Any]] = []
    ids: set = set()
    hashes: set = set()
    for i, a in enumerate(doc["agents"]):
        if not isinstance(a, dict):
            raise ValueError(f"agent #{i} is not an object")
        aid, h = a.get("id"), a.get("token_sha256")
        if aid in ids or h in hashes:
            raise ValueError(f"two entries share an id or a token (at #{i})")
        ids.add(aid)
        hashes.add(h)
        grants, exp = a.get("grants"), a.get("expires_at")
        problem = ("unknown field(s) " + ", ".join(sorted(set(a) - _FIELDS)) if set(a) - _FIELDS else
                   "bad id" if not (isinstance(aid, str) and _ID.match(aid)) else
                   "bad token_sha256" if not (isinstance(h, str) and _HASH.match(h)) else
                   "grants must be a non-empty list of read grants" if not (isinstance(grants, list) and grants and set(map(str, grants)) <= ALLOWED_GRANTS) else
                   "expires_at must be a number" if not (isinstance(exp, (int, float)) and not isinstance(exp, bool)) else
                   "revoked must be true or false" if not isinstance(a.get("revoked", False), bool) else
                   f"rate_per_minute must be a whole number from 1 to {MAX_RATE}" if "rate_per_minute" in a and not (type(a["rate_per_minute"]) is int and 1 <= a["rate_per_minute"] <= MAX_RATE) else "")
        if problem:
            _log.warning("agent entry #%d (%r) is ignored: %s", i, aid if isinstance(aid, str) else "?", problem)
            continue
        out.append(a)
    return out


class AgentTokenStore:
    """A :class:`TokenVerifier` over the agents file. ``authorize`` answers from the SAME entry the token resolved to."""

    def __init__(self, path: str, *, clock: Callable[[], float] = time.time) -> None:
        self.path, self._clock = path, clock
        self._lock = threading.Lock()
        self._stamp: Any = None
        self._agents: List[Dict[str, Any]] = []
        self._complained = ""

    def _load(self) -> List[Dict[str, Any]]:
        with self._lock:
            try:
                st = os.stat(self.path)
                stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
                if stamp != self._stamp:
                    with open(self.path, encoding="utf-8") as fh:
                        self._agents = _entries(json.load(fh))
                    self._stamp, self._complained = stamp, ""
            except (OSError, ValueError) as exc:
                self._agents, self._stamp = [], None                    # never the previous contents: they may name a revoked agent
                why = f"{type(exc).__name__}: {exc}" if isinstance(exc, ValueError) else type(exc).__name__
                if why != self._complained:
                    self._complained = why
                    _log.warning("the agents file %s cannot be used, so no agent can sign in: %s", self.path, why)
            return self._agents

    def _live(self, a: Dict[str, Any]) -> bool:
        return not a.get("revoked", False) and float(a["expires_at"]) > self._clock()

    def verify(self, token: str) -> Optional[GatewayPrincipal]:
        if not token or not token.startswith(TOKEN_PREFIX):
            return None
        want = digest(token)
        found = None
        for a in self._load():                                          # every entry is compared, whichever matches
            if hmac.compare_digest(want, a["token_sha256"]):
                found = a
        if found is None or not self._live(found):
            return None
        tenant = str(found.get("tenant") or "default")
        return GatewayPrincipal(Principal(f"agent:{found['id']}", "service", (), tenant), client_id=found["id"],
                                workspace=tenant, scopes=tuple(sorted(found["grants"])))

    def label_of(self, agent_id: str) -> str:
        """What the operator labelled this agent's token (for an approval card); nothing else of the entry."""
        return str(next((a.get("label", "") for a in self._load() if a.get("id") == agent_id), ""))

    def rate_of(self, principal: Principal) -> Optional[int]:
        """This agent's calls-per-minute limit, read from the entry as it is now; ``None`` = the entry sets none."""
        for a in self._load():
            if principal.id == f"agent:{a['id']}" and principal.tenant == str(a.get("tenant") or "default"):
                return a.get("rate_per_minute")
        return None

    def authorize(self, principal: Principal, permission: str) -> bool:
        """Deny by default; the grants are read again, so a revocation also stops a principal already resolved."""
        for a in self._load():
            if principal.id == f"agent:{a['id']}" and principal.tenant == str(a.get("tenant") or "default"):
                return self._live(a) and permission in a["grants"]
        return False


class AgentRateLimit:
    """The gateway's :class:`BudgetGuard` for per-agent tokens: at most ``rate_per_minute`` capability calls in any 60
    seconds. A refused call does not count against the limit. Held in memory, per process: a restart empties the
    window, and N processes allow N times the rate."""

    def __init__(self, store: AgentTokenStore, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.store, self._clock = store, clock
        self._lock = threading.Lock()
        self._calls: Dict[str, "collections.deque[float]"] = {}

    def check(self, principal: GatewayPrincipal, manifest: Any):
        who = principal.principal
        limit = self.store.rate_of(who) if who.id.startswith("agent:") else None
        if limit is None:
            return True, "", ()
        with self._lock:
            now = self._clock()
            window = self._calls.setdefault(f"{who.tenant}/{who.id}", collections.deque())
            while window and now - window[0] >= 60.0:
                window.popleft()
            if len(window) >= limit:
                return False, f"rate_limited: at most {limit} calls a minute for this agent", ("budget:agent_rate",)
            window.append(now)
        return True, "", ("budget:agent_rate",)


# ── authority (the production profile) ────────────────────────────────────────────────────────────
def envelope_key(agent_id: str) -> str:
    return f"gateway-agent:{agent_id}"


def envelope_key_of(principal: GatewayPrincipal) -> str:
    """Which stored authority covers a caller. Only a per-agent principal has one: the shared bearer maps to a key that
    is never written, so in production every call it makes is denied."""
    if str(principal.subject).startswith("agent:"):
        return envelope_key(principal.client_id)
    return "gateway-shared-bearer"


def record_authority(authority_db: str, agent_id: str, grants: List[str], *, tenant: str, issued_at: float, expires_at: float,
                     max_actions: int = MAX_ACTIONS) -> str:
    """Store what this agent may do: exactly the capabilities its grants name, each at its own risk tier, for its tenant,
    until its token ends. No delegation, no spend. Returns the record's digest."""
    from agentic_os.agent_gateway.app_record_capabilities import manifests as app_manifests  # noqa: PLC0415
    from agentic_os.agent_gateway.capabilities import READ_CAPABILITIES  # noqa: PLC0415
    from agentic_os.agent_gateway.economics_capabilities import manifests as economics_manifests  # noqa: PLC0415
    from agentic_os.agent_gateway.media_capabilities import manifests as media_manifests  # noqa: PLC0415
    from agentic_os.agent_gateway.product_capabilities import GRANTS as PRODUCT_GRANTS, manifests as product_manifests  # noqa: PLC0415
    from agentic_os.mission.authority import AuthorityEnvelope, AutonomyBudget, Grant, SqliteEnvelopeStore  # noqa: PLC0415
    held = set(grants) & ALLOWED_GRANTS
    # the product tools only for a token that holds one of their grants: they need the products package's dependencies
    known = (*READ_CAPABILITIES, *app_manifests(), *economics_manifests(), *media_manifests(), *(product_manifests() if held & set(PRODUCT_GRANTS) else ()))
    tiers = {c.name: int(c.risk_tier) for c in known if c.permissions and set(c.permissions) <= held}
    env = AuthorityEnvelope(
        tenant=tenant, principal=f"agent:{agent_id}", mission_id=envelope_key(agent_id),
        goal="access for an external agent through the governed gateway",
        # each capability at its own tier and no higher: a read at 0, a proposal or a test run at 1
        grants=tuple(Grant(capability=n, max_tier=t) for n, t in sorted(tiers.items())),
        budget=AutonomyBudget(usd=0.0, max_actions=int(max_actions), max_children=0, max_depth=0, max_parallel=4),
        may_delegate=False, issued_at=issued_at, expires_at=expires_at)
    SqliteEnvelopeStore(authority_db).put(env)
    return env.digest()


# ── the operator's commands ────────────────────────────────────────────────────────────────────────
def _read(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"schema": SCHEMA, "agents": []}
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    _entries(doc)                                                       # refuse to rewrite a file that does not validate
    return doc


def _write(path: str, doc: Dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".agents-", dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def issue(path: str, agent_id: str, grants: List[str], *, days: Optional[int] = None, minutes: Optional[int] = None,
          rate_per_minute: Optional[int] = None, tenant: str = "default", label: str = "", now: Optional[float] = None,
          authority_db: Optional[str] = None) -> str:
    """Add an agent and return its token. The token is not stored anywhere: it is shown once."""
    now = time.time() if now is None else now
    if not _ID.match(agent_id):
        raise ValueError("an agent id is lower-case letters, digits, '.', '_' or '-' (at most 64)")
    if not grants or not set(grants) <= ALLOWED_GRANTS:
        raise ValueError(f"grants must be among {', '.join(sorted(ALLOWED_GRANTS))}")
    if (days is None) == (minutes is None):
        raise ValueError("give a lifetime in --days or in --minutes (exactly one): every agent token expires")
    if days is not None and not 1 <= days <= MAX_DAYS:
        raise ValueError(f"--days must be between 1 and {MAX_DAYS}: every agent token expires")
    if minutes is not None and not 1 <= minutes <= MAX_MINUTES:
        raise ValueError(f"--minutes must be between 1 and {MAX_MINUTES}")
    if rate_per_minute is not None and not 1 <= rate_per_minute <= MAX_RATE:
        raise ValueError(f"--rate must be between 1 and {MAX_RATE} calls a minute")
    lifetime = days * 86400 if days is not None else minutes * 60
    doc = _read(path)
    if any(a.get("id") == agent_id for a in doc["agents"]):
        raise ValueError(f"{agent_id} already exists: revoke it and choose a new id, so the audit trail stays unambiguous")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    doc["agents"].append({"id": agent_id, "token_sha256": digest(token), "grants": sorted(set(grants)), "tenant": tenant,
                          "label": label, "created_at": int(now), "expires_at": int(now + lifetime), "revoked": False,
                          **({"rate_per_minute": rate_per_minute} if rate_per_minute is not None else {})})
    _write(path, doc)
    if authority_db:
        try:
            record_authority(authority_db, agent_id, grants, tenant=tenant, issued_at=int(now), expires_at=int(now + lifetime))
        except Exception:
            revoke(path, agent_id)                          # no live credential without the authority that bounds it
            raise
    return token


def revoke(path: str, agent_id: str, *, authority_db: Optional[str] = None) -> bool:
    doc = _read(path)
    for a in doc["agents"]:
        if a.get("id") == agent_id:
            a["revoked"] = True
            _write(path, doc)
            if authority_db:
                from agentic_os.mission.authority import SqliteEnvelopeStore  # noqa: PLC0415
                SqliteEnvelopeStore(authority_db).revoke(envelope_key(agent_id))
            return True
    return False


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agentic_os.agent_gateway.agent_tokens", description="Per-agent gateway tokens.")
    ap.add_argument("command", choices=("issue", "revoke", "list"))
    ap.add_argument("file")
    ap.add_argument("--id")
    ap.add_argument("--grant", action="append", default=[])
    ap.add_argument("--days", type=int, help="lifetime in days (30 when neither --days nor --minutes is given)")
    ap.add_argument("--minutes", type=int, help="lifetime in minutes, for a temporary identity")
    ap.add_argument("--rate", type=int, help="most capability calls a minute")
    ap.add_argument("--tenant", default="default")
    ap.add_argument("--authority-db", default=os.environ.get("AGENTIC_OS_AUTHORITY_DB", "").strip() or None,
                    help="the authority store the gateway reads (needed under the production profile)")
    ap.add_argument("--label", default="")
    args = ap.parse_args(argv)
    try:
        if args.command == "list":
            for a in _read(args.file)["agents"]:
                state = "revoked" if a.get("revoked") else "expired" if a["expires_at"] <= time.time() else "active"
                print(f"{a['id']:32s} {state:8s} expires {time.strftime('%Y-%m-%d', time.gmtime(a['expires_at']))}  {','.join(a['grants'])}")
            return 0
        if not args.id:
            print("--id is required", file=sys.stderr)
            return 2
        if args.command == "revoke":
            if not revoke(args.file, args.id, authority_db=args.authority_db):
                print(f"no agent {args.id}", file=sys.stderr)
                return 1
            print(f"revoked {args.id}")
            return 0
        if os.environ.get("AGENTIC_OS_PROFILE", "").strip().lower() == "production" and not args.authority_db:
            print("the production profile needs --authority-db (or AGENTIC_OS_AUTHORITY_DB): a token without an authority "
                  "record would be denied every call", file=sys.stderr)
            return 1
        days = 30 if args.days is None and args.minutes is None else args.days
        token = issue(args.file, args.id, args.grant, days=days, minutes=args.minutes, rate_per_minute=args.rate,
                      tenant=args.tenant, label=args.label, authority_db=args.authority_db)
        print(f"token for {args.id} (shown once; give it to that agent and nobody else):", file=sys.stderr)
        print(token)                                                    # stdout only: it can be piped into a secret store
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

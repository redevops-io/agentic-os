"""One token per external agent, with its own grants, expiry and revocation.

The mounted gateway had a single static bearer shared by every holder: no identity per agent, so no per-agent grant, audit
attribution or revocation. This is the :class:`~agentic_os.agent_gateway.auth.TokenVerifier` that gives each agent its own.

The operator names a JSON file in ``AGENT_GATEWAY_AGENTS_FILE``. It stores only the SHA-256 of each token:

    {"schema": "aos.agent-gateway.agents/v1",
     "agents": [{"id": "claude-laptop", "token_sha256": "…", "grants": ["projects.read"], "expires_at": 1830000000,
                 "tenant": "default", "label": "…", "revoked": false}]}

    python -m agentic_os.agent_gateway.agent_tokens issue  agents.json --id claude-laptop --grant projects.read --days 30
    python -m agentic_os.agent_gateway.agent_tokens revoke agents.json --id claude-laptop
    python -m agentic_os.agent_gateway.agent_tokens list   agents.json

The file is read again whenever it changes, so a revocation takes effect on the next call. Everything fails closed: a file
that cannot be read or does not validate authenticates nobody; an entry with a grant outside the allowed set, a missing or
malformed field, a past expiry, or a duplicate id or token authenticates nobody.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, FrozenSet, List, Optional

from agentic_os.overlays import Principal

from .contracts import GatewayPrincipal

SCHEMA = "aos.agent-gateway.agents/v1"
#: what an agent may be granted here. The mounted gateway exposes reads only; a write grant would grant nothing and is
#: refused, so that a file cannot appear to give an agent something it cannot have.
READ_GRANTS: FrozenSet[str] = frozenset({"projects.read", "missions.read", "sources.read"})
TOKEN_PREFIX = "aga_"
MAX_DAYS = 366
_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = {"id", "token_sha256", "grants", "expires_at", "tenant", "label", "revoked", "created_at"}
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
                   "grants must be a non-empty list of read grants" if not (isinstance(grants, list) and grants and set(map(str, grants)) <= READ_GRANTS) else
                   "expires_at must be a number" if not (isinstance(exp, (int, float)) and not isinstance(exp, bool)) else
                   "revoked must be true or false" if not isinstance(a.get("revoked", False), bool) else "")
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

    def authorize(self, principal: Principal, permission: str) -> bool:
        """Deny by default; the grants are read again, so a revocation also stops a principal already resolved."""
        for a in self._load():
            if principal.id == f"agent:{a['id']}" and principal.tenant == str(a.get("tenant") or "default"):
                return self._live(a) and permission in a["grants"]
        return False


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


def issue(path: str, agent_id: str, grants: List[str], *, days: int, tenant: str = "default", label: str = "",
          now: Optional[float] = None) -> str:
    """Add an agent and return its token. The token is not stored anywhere: it is shown once."""
    now = time.time() if now is None else now
    if not _ID.match(agent_id):
        raise ValueError("an agent id is lower-case letters, digits, '.', '_' or '-' (at most 64)")
    if not grants or not set(grants) <= READ_GRANTS:
        raise ValueError(f"grants must be among {', '.join(sorted(READ_GRANTS))}")
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"--days must be between 1 and {MAX_DAYS}: every agent token expires")
    doc = _read(path)
    if any(a.get("id") == agent_id for a in doc["agents"]):
        raise ValueError(f"{agent_id} already exists: revoke it and choose a new id, so the audit trail stays unambiguous")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    doc["agents"].append({"id": agent_id, "token_sha256": digest(token), "grants": sorted(set(grants)), "tenant": tenant,
                          "label": label, "created_at": int(now), "expires_at": int(now + days * 86400), "revoked": False})
    _write(path, doc)
    return token


def revoke(path: str, agent_id: str) -> bool:
    doc = _read(path)
    for a in doc["agents"]:
        if a.get("id") == agent_id:
            a["revoked"] = True
            _write(path, doc)
            return True
    return False


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agentic_os.agent_gateway.agent_tokens", description="Per-agent gateway tokens.")
    ap.add_argument("command", choices=("issue", "revoke", "list"))
    ap.add_argument("file")
    ap.add_argument("--id")
    ap.add_argument("--grant", action="append", default=[])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--tenant", default="default")
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
            if not revoke(args.file, args.id):
                print(f"no agent {args.id}", file=sys.stderr)
                return 1
            print(f"revoked {args.id}")
            return 0
        token = issue(args.file, args.id, args.grant, days=args.days, tenant=args.tenant, label=args.label)
        print(f"token for {args.id} (shown once; give it to that agent and nobody else):", file=sys.stderr)
        print(token)                                                    # stdout only: it can be piped into a secret store
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

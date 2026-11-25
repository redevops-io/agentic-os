"""Scoped credentials for external agents on a locally installed app, and discovery of the running local gateway.

The desktop app serves on loopback and admits only the window that holds its launch code. An external agent (Claude
Desktop, Gemini CLI, a local model) needs a way in that is not the window's session and is not unrestricted:

* ``LocalAgentStore`` keeps one record per connected agent in the per-user state directory: a name, the grants it was
  given, when it was created, last used and revoked, and the SHA-256 of its token. The token itself is written once to
  a file only the user can read (``tokens/<id>.token``, mode 0600), so a client is configured with a file PATH and never
  holds a secret in its own settings; nobody types or constructs a token.
* **No agent exists until the user connects one**, and an agent gets the grants named when it was connected, from the
  read grants this build's gateway offers. There is no grant that means "everything", an unknown grant is refused, and
  a revoked or unknown token is nobody.
* ``publish_gateway`` writes ``gateway.json`` (address, process id, version) next to the store while the app is
  serving and removes it when it stops; ``discover_gateway`` reads it and reports a stale file (the process is gone) as
  "not running" instead of pointing an agent at a dead or reused port.

Standard library only.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ENV_DIR = "AGENTIC_OS_LOCAL_AGENTS_DIR"
#: what this build's local gateway can grant. Read grants only: the mounted gateway registers no write capability.
GRANTABLE = ("projects.read", "missions.read", "sources.read", "dashboards.read")
#: dashboards.read is NOT a default: the dashboards hold test results and business figures, so it is granted by naming it.
DEFAULT_GRANTS = ("projects.read", "missions.read", "sources.read")
TOKEN_PREFIX = "rla_"                                   # rAIls local agent
SCHEMA = "agentic-os.local-agents/1"
GATEWAY_SCHEMA = "agentic-os.local-gateway/1"


class LocalAgentError(ValueError):
    pass


@dataclass
class AgentRecord:
    id: str
    name: str
    grants: Tuple[str, ...]
    token_sha256: str
    created_at: float
    last_used_at: Optional[float] = None
    revoked_at: Optional[float] = None
    client: str = ""                                    # which client it was connected for (free text, for the user)

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    def public(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("token_sha256")
        d["grants"] = list(self.grants)
        d["active"] = self.active
        return d


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass                                            # Windows: the per-user profile's ACL is the protection there


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


class LocalAgentStore:
    def __init__(self, directory: "str | Path", *, clock=time.time) -> None:
        self.dir = Path(directory)
        self._clock = clock

    @staticmethod
    def from_env(env: Optional[Dict[str, str]] = None) -> Optional["LocalAgentStore"]:
        d = ((env if env is not None else os.environ).get(ENV_DIR) or "").strip()
        return LocalAgentStore(d) if d else None

    # ── storage ──────────────────────────────────────────────────────────────────────────────────────────
    @property
    def _file(self) -> Path:
        return self.dir / "agents.json"

    def _load(self) -> List[AgentRecord]:
        try:
            doc = json.loads(self._file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            # an unreadable store admits nobody; it is not replaced by an empty one that would hide the revocations in it
            raise LocalAgentError(f"the local agents file cannot be read ({type(exc).__name__}); no agent is admitted") from exc
        if doc.get("schema") != SCHEMA:
            raise LocalAgentError("the local agents file has an unknown schema; no agent is admitted")
        return [AgentRecord(id=a["id"], name=a["name"], grants=tuple(a["grants"]), token_sha256=a["token_sha256"], created_at=a["created_at"],
                            last_used_at=a.get("last_used_at"), revoked_at=a.get("revoked_at"), client=a.get("client", "")) for a in doc.get("agents", [])]

    def _save(self, records: List[AgentRecord]) -> None:
        _private_dir(self.dir)
        _write_private(self._file, json.dumps({"schema": SCHEMA, "agents": [asdict(r) | {"grants": list(r.grants)} for r in records]}, indent=1) + "\n")

    # ── the user's actions ───────────────────────────────────────────────────────────────────────────────
    def connect(self, name: str, *, grants: Iterable[str] = DEFAULT_GRANTS, client: str = "") -> Tuple[AgentRecord, Path]:
        """Create a credential for one agent. Returns the record and the path of its token file (the token is not returned)."""
        name = " ".join(str(name).split())[:80]
        if not name:
            raise LocalAgentError("an agent needs a name the user will recognise")
        wanted = tuple(dict.fromkeys(str(g).strip() for g in grants))
        unknown = [g for g in wanted if g not in GRANTABLE]
        if unknown or not wanted:
            raise LocalAgentError(f"not grantable on this installation: {unknown or 'nothing was named'}; grantable: {', '.join(GRANTABLE)}")
        records = self._load()
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        rec = AgentRecord(id="agent-" + secrets.token_hex(6), name=name, grants=wanted, token_sha256=hashlib.sha256(token.encode()).hexdigest(),
                          created_at=self._clock(), client=str(client)[:40])
        records.append(rec)
        self._save(records)
        _private_dir(self.dir / "tokens")
        path = self.dir / "tokens" / f"{rec.id}.token"
        _write_private(path, token + "\n")
        return rec, path

    def revoke(self, agent_id: str) -> bool:
        records = self._load()
        hit = False
        for r in records:
            if r.id == agent_id and r.active:
                r.revoked_at, hit = self._clock(), True
        if hit:
            self._save(records)
        try:
            (self.dir / "tokens" / f"{agent_id}.token").unlink()
        except OSError:
            pass
        return hit

    def list(self) -> List[AgentRecord]:
        return self._load()

    # ── what the gateway asks ────────────────────────────────────────────────────────────────────────────
    def verify(self, token: str, *, touch: bool = True) -> Optional[AgentRecord]:
        """The active agent this token belongs to, or None. Constant-time over the stored hashes; a store that cannot be
        read admits nobody."""
        if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX) or len(token) > 200:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        try:
            records = self._load()
        except LocalAgentError:
            return None
        found = None
        for r in records:
            if hmac.compare_digest(r.token_sha256, digest) and r.active:
                found = r
        if found is not None and touch:
            now = self._clock()
            if found.last_used_at is None or now - found.last_used_at > 60:        # not a disk write per call
                found.last_used_at = now
                try:
                    self._save(records)
                except OSError:
                    pass
        return found

    def grants_of(self, agent_id: str) -> Tuple[str, ...]:
        """The grants of an agent as they are NOW (so a revocation takes effect on the next call)."""
        try:
            return next((r.grants for r in self._load() if r.id == agent_id and r.active), ())
        except LocalAgentError:
            return ()


# ── discovery of the running local gateway ───────────────────────────────────────────────────────────────

def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)             # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def publish_gateway(directory: "str | Path", *, port: int, version: str = "", pid: Optional[int] = None) -> Path:
    d = Path(directory)
    _private_dir(d)
    path = d / "gateway.json"
    _write_private(path, json.dumps({"schema": GATEWAY_SCHEMA, "url": f"http://127.0.0.1:{int(port)}", "pid": int(pid or os.getpid()),
                                     "started_at": time.time(), "version": version}, indent=1) + "\n")
    return path


def unpublish_gateway(directory: "str | Path", *, pid: Optional[int] = None) -> None:
    """Remove the discovery file, but only our own: a newer instance's file is left alone."""
    path = Path(directory) / "gateway.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if int(doc.get("pid", -1)) == int(pid or os.getpid()):
            path.unlink()
    except (OSError, ValueError):
        pass


def discover_gateway(directory: "str | Path") -> Dict[str, Any]:
    """``{"running": True, "url": ...}`` or ``{"running": False, "reason": ...}``. Never a URL for a process that is gone."""
    path = Path(directory) / "gateway.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"running": False, "reason": "the app is not running (no local gateway is published)"}
    except (OSError, ValueError):
        return {"running": False, "reason": "the local gateway file cannot be read"}
    url = str(doc.get("url", ""))
    if doc.get("schema") != GATEWAY_SCHEMA or not url.startswith("http://127.0.0.1:"):
        return {"running": False, "reason": "the local gateway file is not one this app wrote"}
    if not _alive(int(doc.get("pid", -1))):
        return {"running": False, "reason": "the app is not running (its last gateway file is stale)"}
    return {"running": True, "url": url, "pid": int(doc["pid"]), "version": str(doc.get("version", ""))}


# ── authority: what a connected agent may do, for how long ───────────────────────────────────────────────

#: which capabilities each grant covers in the agent's authority envelope. max_tier 0: nothing side-effecting can run under it.
GRANT_PATTERNS = {"projects.read": "projects.*", "missions.read": "missions.*", "sources.read": "sources.*", "dashboards.read": "dashboards.*"}
DEFAULT_DAYS = 30.0
MAX_DAYS = 90.0
TENANT = "local"


def envelope_key(agent_id: str) -> str:
    return f"{TENANT}:{agent_id}"


def issue_authority(record: AgentRecord, authority_db: str, *, days: float = DEFAULT_DAYS, max_actions: int = 5000) -> str:
    """Store the authority envelope of a connected agent and return its digest. The production profile runs the gateway
    fail-closed: without this envelope the agent's token admits it to the gateway and every call is then denied. The
    envelope expires (30 days by default, 90 at most), grants read capabilities at tier 0 only, cannot delegate, and has
    an action budget; renewing is connecting again."""
    from agentic_os.mission.authority import AuthorityEnvelope, AutonomyBudget, Grant, SqliteEnvelopeStore   # noqa: PLC0415
    days = float(days)
    if not 0 < days <= MAX_DAYS:
        raise LocalAgentError(f"an agent's authority lasts between a moment and {int(MAX_DAYS)} days")
    now = time.time()
    env = AuthorityEnvelope(
        tenant=TENANT, principal=f"local-agent:{record.id}", mission_id=envelope_key(record.id),
        goal=f"external agent {record.name!r} reading this installation through the governed gateway",
        grants=tuple(Grant(capability=GRANT_PATTERNS[g], max_tier=0) for g in record.grants),
        budget=AutonomyBudget(usd=0.0, max_actions=int(max_actions), max_children=0, max_depth=0, max_parallel=4),
        may_delegate=False, issued_at=now, expires_at=now + days * 86400)
    SqliteEnvelopeStore(authority_db).put(env)
    return env.digest()


def revoke_authority(agent_id: str, authority_db: str) -> None:
    from agentic_os.mission.authority import SqliteEnvelopeStore   # noqa: PLC0415
    SqliteEnvelopeStore(authority_db).revoke(envelope_key(agent_id))


def connect_agent(name: str, *, grants: Iterable[str] = DEFAULT_GRANTS, client: str = "", days: float = DEFAULT_DAYS,
                  env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Everything connecting an agent means on an installed app: the credential, its token file and its authority."""
    env = dict(os.environ if env is None else env)
    store = LocalAgentStore.from_env(env)
    db = (env.get("AGENTIC_OS_AUTHORITY_DB") or "").strip()
    if store is None or not db:
        raise LocalAgentError("this is not an installed app profile: no agents directory or no authority store is configured")
    if not 0 < float(days) <= MAX_DAYS:
        raise LocalAgentError(f"an agent's authority lasts between a moment and {int(MAX_DAYS)} days")
    rec, token_file = store.connect(name, grants=grants, client=client)
    try:
        digest = issue_authority(rec, db, days=days)
    except Exception:
        store.revoke(rec.id)                              # no credential without the authority that bounds it
        raise
    return {"agent": rec.public(), "token_file": str(token_file), "authority_digest": digest, "expires_in_days": float(days),
            "gateway": discover_gateway(store.dir)}


def disconnect_agent(agent_id: str, *, env: Optional[Dict[str, str]] = None) -> bool:
    env = dict(os.environ if env is None else env)
    store = LocalAgentStore.from_env(env)
    db = (env.get("AGENTIC_OS_AUTHORITY_DB") or "").strip()
    if store is None:
        return False
    hit = store.revoke(agent_id)
    if db:
        revoke_authority(agent_id, db)
    return hit


# ── the command the installed app offers ─────────────────────────────────────────────────────────────────

def bridge_path() -> Path:
    """The MCP bridge shipped inside the app (a copy of integrations/mcp-bridge/server/index.js)."""
    return Path(__file__).resolve().parent / "mcp_bridge" / "index.js"


def mcp_server_config(token_file: str, agents_dir: str) -> Dict[str, Any]:
    """How an MCP client starts the bridge for one connected agent: two paths, no address and no secret."""
    return {"command": "node", "args": [str(bridge_path())], "env": {"RAILS_AGENTS_DIR": str(agents_dir), "RAILS_TOKEN_FILE": str(token_file)}}


def write_client_settings(path: "str | Path", server: Dict[str, Any], *, name: str = "rails") -> None:
    """Add (or replace) ONE entry under ``mcpServers`` in a client's JSON settings file (the Gemini CLI layout) and leave the rest of
    the file as it is. Refuses a file that is not a JSON object rather than overwriting it."""
    path = Path(path)
    doc: Dict[str, Any] = {}
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise LocalAgentError(f"{path} is not JSON; it was not changed") from exc
        if not isinstance(doc, dict) or not isinstance(doc.get("mcpServers", {}), dict):
            raise LocalAgentError(f"{path} does not have the expected layout; it was not changed")
    doc.setdefault("mcpServers", {})[name] = server
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_private(path, json.dumps(doc, indent=2) + "\n")


def cli(argv: List[str], *, env: Optional[Dict[str, str]] = None, out=None) -> int:
    """``agent connect|list|disconnect|status``. Prints JSON. A token is never printed."""
    import argparse
    import sys
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="agent", description="Connect external agents to this installation, each with its own scoped, expiring credential.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("connect", help="create a credential for one agent")
    c.add_argument("--name", required=True, help="a name you will recognise, for example 'Gemini CLI'")
    c.add_argument("--client", default="generic", choices=("generic", "gemini-cli", "claude-desktop", "local-model"))
    c.add_argument("--grants", default=",".join(DEFAULT_GRANTS), help="comma-separated; grantable: " + ", ".join(GRANTABLE))
    c.add_argument("--days", type=float, default=DEFAULT_DAYS, help=f"how long the authority lasts (at most {int(MAX_DAYS)})")
    c.add_argument("--write-settings", default="", help="also add the MCP server entry to this client settings file (for example .gemini/settings.json)")
    sub.add_parser("list", help="the connected agents")
    d = sub.add_parser("disconnect", help="revoke one agent")
    d.add_argument("id")
    sub.add_parser("status", help="whether the app is running and where")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    env = dict(os.environ if env is None else env)
    store = LocalAgentStore.from_env(env)
    try:
        if store is None:
            raise LocalAgentError("no agents directory is configured for this installation")
        if a.cmd == "connect":
            res = connect_agent(a.name, grants=[g for g in a.grants.split(",") if g.strip()], client=a.client, days=a.days, env=env)
            res["mcp_server"] = mcp_server_config(res["token_file"], str(store.dir))
            if a.write_settings:
                write_client_settings(a.write_settings, res["mcp_server"])
                res["settings_written"] = str(a.write_settings)
        elif a.cmd == "list":
            res = {"agents": [r.public() for r in store.list()]}
        elif a.cmd == "disconnect":
            res = {"disconnected": disconnect_agent(a.id, env=env), "id": a.id}
        else:
            res = discover_gateway(store.dir)
    except LocalAgentError as e:
        out.write(json.dumps({"error": str(e)}) + "\n")
        return 1
    out.write(json.dumps(res, indent=1) + "\n")
    return 0

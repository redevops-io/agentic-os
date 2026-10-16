"""Gate notifications — push a human-approval gate to people, durably and honestly.

Opt-in (``AGENTIC_OS_GATE_NOTIFY=1`` + a configured recipient); otherwise nothing is installed.

* ``GateNotifier`` is a ``LifecycleContributor``: ``on_gate_reached`` only ENQUEUES outbox rows.
* The outbox is a stdlib SQLite file. Row key = sha256(mission, node, gate_version, recipient, channel, kind),
  so duplicate hook calls, retries and restarts can never create a second row. ``gate_version`` is the seq of
  the ``NodeParked`` event, so re-parking a node later is a new gate.
* ``tick(now)`` (also run by an optional daemon thread) sends due rows, retries with exponential backoff
  and a cap, sends ONE reminder and ONE escalation, and appends a ``GateExpired`` event after the TTL.
  It NEVER approves or rejects: the mission stays parked.
* Transport is at-least-once; processing is idempotent. A row with a recorded receipt is never re-sent, and
  a due row is claimed with an atomic compare-and-swap so concurrent threads/processes cannot both send it.
  DUPLICATE external messages remain possible when a provider processes a message but the ack is lost
  (timeout) or the process crashes between a send and the receipt write: the retry sends again.
* A notice carries only mission/capability and a link to the authenticated approval surface. Never the
  approval token, never a secret. Replies, link clicks and callbacks can only record "acknowledged".
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
import warnings
from typing import Any, Callable

from .lifecycle import GateReached, LifecycleContributor

ENV_ENABLE = "AGENTIC_OS_GATE_NOTIFY"
ENV_CONFIG = "AGENTIC_OS_GATE_NOTIFY_CONFIG"           # path to a JSON config file
ENV_RECIPIENTS = "AGENTIC_OS_GATE_NOTIFY_RECIPIENTS"   # "name:channel[:address],..."
ENV_ESCALATE = "AGENTIC_OS_GATE_NOTIFY_ESCALATE_TO"    # same syntax
ENV_CALLBACK_SECRET = "AGENTIC_OS_GATE_NOTIFY_CALLBACK_SECRET"
ENV_DB = "AGENTIC_OS_GATE_NOTIFY_DB"                   # durable outbox file (default: <state dir>/gate_notify.db)
ENV_STATE_DIR = "AGENTIC_OS_STATE_DIR"

DEFAULTS = {"reminder_after": 900.0, "escalate_after": 1800.0, "expire_after": 86400.0,
            "backoff_base": 5.0, "backoff_cap": 300.0, "max_attempts": 8,
            "rate_max": 5, "rate_per": 60.0, "link_base": "", "retention": 7 * 86400.0}
CALLBACK_WINDOW = 300.0
MAX_CALLBACK_BYTES = 16384
_TS_RE = re.compile(r"\d{1,12}(\.\d{1,6})?")
_OUTBOX_COLS = frozenset({"key", "gate", "mission_id", "node_id", "gate_version", "recipient", "channel", "kind", "status", "ack_ref"})


def _safe(text: Any, limit: int = 80) -> str:
    """Neutralise chat markup/mentions (@, <>, backticks, links) in values interpolated into a notice."""
    return re.sub(r"[^\w.\-: /]", "_", str(text))[:limit]


def default_db_path(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    base = env.get(ENV_STATE_DIR) or os.path.join(os.path.expanduser("~"), ".agentic-os")
    return os.path.join(base, "gate_notify.db")


def notification_key(mission_id: str, node_id: str, gate_version: Any, recipient: str,
                     channel: str, kind: str) -> str:
    raw = json.dumps([mission_id, node_id, str(gate_version), recipient, channel, kind])
    return hashlib.sha256(raw.encode()).hexdigest()


def _gate_id(mission_id: str, node_id: str, version: Any) -> str:
    return f"{mission_id}|{node_id}|{version}"


def open_gates(store, mission_id: str) -> dict[str, int]:
    """node_id -> gate_version (seq of the NodeParked event) for gates still awaiting a human."""
    out: dict[str, int] = {}
    for e in store.for_mission(mission_id):
        if e.type == "NodeParked":
            out[e.payload["node_id"]] = e.seq
        elif e.type in ("NodeResumed", "NodeSkipped"):
            out.pop(e.payload.get("node_id"), None)
    return out


class Outbox:
    """Durable, idempotent notification outbox + gate registry (SQLite; ``:memory:`` for tests)."""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        if path != ":memory:":
            d = os.path.dirname(os.path.abspath(path))
            existed = os.path.isdir(d)
            os.makedirs(d, mode=0o700, exist_ok=True)
            if existed and os.stat(d).st_mode & 0o077:
                warnings.warn(f"gate_notify DB directory {d} is accessible to other users (holds ack refs/addresses)",
                              RuntimeWarning)
            old = os.umask(0o077)               # new db/-wal/-shm files are created 0600
            try:
                self._db = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
                self._db.execute("PRAGMA busy_timeout=30000")
                self._db.execute("PRAGMA journal_mode=WAL")
            finally:
                os.umask(old)
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.chmod(path + suffix, 0o600)
                except OSError:
                    pass
        else:
            self._db = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript("""
            CREATE TABLE IF NOT EXISTS outbox(
              key TEXT PRIMARY KEY, gate TEXT, mission_id TEXT, node_id TEXT, gate_version TEXT,
              recipient TEXT, channel TEXT, address TEXT, kind TEXT, text TEXT,
              status TEXT, attempts INTEGER DEFAULT 0, next_attempt REAL, created_at REAL,
              sent_at REAL, provider_msg_id TEXT, last_error TEXT, ack_at REAL, ack_ref TEXT);
            CREATE TABLE IF NOT EXISTS gates(
              gate TEXT PRIMARY KEY, mission_id TEXT, node_id TEXT, gate_version TEXT, capability TEXT,
              created_at REAL, reminded INTEGER DEFAULT 0, escalated INTEGER DEFAULT 0,
              expired INTEGER DEFAULT 0, resolved_at REAL);
            CREATE TABLE IF NOT EXISTS sends(recipient TEXT, ts REAL);
            CREATE TABLE IF NOT EXISTS seen_sigs(sig TEXT PRIMARY KEY, ts REAL);
            """)
            self._db.commit()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._db.execute(sql, args)
            cols = [c[0] for c in cur.description or []]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            self._db.commit()
            return rows

    def register_gate(self, mission_id, node_id, version, capability, now: float) -> None:
        self._q("INSERT OR IGNORE INTO gates(gate,mission_id,node_id,gate_version,capability,created_at) "
                "VALUES(?,?,?,?,?,?)", (_gate_id(mission_id, node_id, version), mission_id, node_id,
                                        str(version), capability, now))

    def enqueue(self, *, mission_id, node_id, version, recipient, channel, address, kind, text, now) -> bool:
        """Insert-if-absent. Returns True only when a NEW row was created."""
        key = notification_key(mission_id, node_id, version, recipient, channel, kind)
        ack_ref = secrets.token_urlsafe(18)       # opaque, random; NOT an approval token and not derived from one
        text = text.replace("{ACK}", ack_ref)
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO outbox(key,gate,mission_id,node_id,gate_version,recipient,channel,address,"
                "kind,text,status,attempts,next_attempt,created_at,ack_ref) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, _gate_id(mission_id, node_id, version), mission_id, node_id, str(version), recipient,
                 channel, address, kind, text, "pending", 0, now, now, ack_ref))
            self._db.commit()
            return cur.rowcount == 1

    def rows(self, **where) -> list[dict]:
        bad = set(where) - _OUTBOX_COLS
        if bad:
            raise ValueError(f"unknown outbox column(s): {sorted(bad)}")
        clause = " AND ".join(f"{k}=?" for k in where) or "1=1"
        return self._q(f"SELECT * FROM outbox WHERE {clause} ORDER BY created_at, key", tuple(where.values()))

    def gates(self) -> list[dict]:
        return self._q("SELECT * FROM gates")

    def notices_for(self, mission_id: str, node_id: str) -> list[dict]:
        """Read-only receipt view for the Needs-You projection. No addresses, no message text."""
        return [{"kind": r["kind"], "recipient": r["recipient"], "channel": r["channel"],
                 "state": "acknowledged" if r["ack_at"] else {"sent": "delivered", "pending": "pending",
                                                              "retrying": "retrying", "failed": "failed",
                                                              "cancelled": "cancelled"}.get(r["status"], r["status"]),
                 "attempts": r["attempts"], "provider_msg_id": r["provider_msg_id"], "sent_at": r["sent_at"]}
                for r in self._q("SELECT * FROM outbox WHERE mission_id=? AND node_id=? ORDER BY created_at",
                                 (mission_id, node_id))]

    def acknowledge(self, ack_ref: str, now: float) -> bool:
        if not ack_ref:
            return False
        with self._lock:
            cur = self._db.execute("UPDATE outbox SET ack_at=COALESCE(ack_at,?) WHERE ack_ref=?", (now, ack_ref))
            self._db.commit()
            return cur.rowcount == 1

    def sig_seen_or_record(self, sig: str, now: float) -> bool:
        with self._lock:
            cur = self._db.execute("INSERT OR IGNORE INTO seen_sigs(sig,ts) VALUES(?,?)", (sig, now))
            self._db.execute("DELETE FROM seen_sigs WHERE ts<?", (now - 2 * CALLBACK_WINDOW,))
            self._db.commit()
            return cur.rowcount == 0

    def claim(self, key: str, attempts: int, now: float, lease: float) -> bool:
        """Atomic compare-and-swap lease: exactly one worker (thread or process) wins a due row."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE outbox SET attempts=attempts+1, next_attempt=? WHERE key=? AND attempts=? "
                "AND status IN ('pending','retrying') AND next_attempt<=? AND sent_at IS NULL",
                (now + lease, key, attempts, now))
            self._db.commit()
            return cur.rowcount == 1

    def prune(self, now: float, retention: float, rate_per: float) -> None:
        """Drop rate-limit samples and RESOLVED gates (with their rows) past retention. Unresolved rows are kept."""
        with self._lock:
            self._db.execute("DELETE FROM sends WHERE ts<?", (now - max(rate_per, 1.0) * 2,))
            old = [r[0] for r in self._db.execute(
                "SELECT gate FROM gates WHERE resolved_at IS NOT NULL AND resolved_at<?", (now - retention,))]
            for g in old:
                self._db.execute("DELETE FROM outbox WHERE gate=? AND status NOT IN ('pending','retrying')", (g,))
                self._db.execute("DELETE FROM gates WHERE gate=?", (g,))
            self._db.commit()


def _parse_recipients(spec: str, default_addr: Callable[[str], str] = lambda c: "") -> list[dict]:
    out = []
    for part in (spec or "").split(","):
        bits = [b.strip() for b in part.strip().split(":")]
        if len(bits) >= 2 and bits[0] and bits[1]:
            out.append({"id": bits[0], "channel": bits[1], "address": bits[2] if len(bits) > 2 else ""})
    return out


def load_config(env: dict | None = None) -> dict | None:
    """None unless explicitly enabled AND at least one recipient is configured (default OFF)."""
    env = os.environ if env is None else env
    if env.get(ENV_ENABLE) != "1":
        return None
    cfg: dict = {}
    path = env.get(ENV_CONFIG, "")
    if path:
        try:
            with open(path) as fh:
                cfg = json.load(fh)
        except (OSError, ValueError):
            return None
    cfg.setdefault("recipients", _parse_recipients(env.get(ENV_RECIPIENTS, "")))
    cfg.setdefault("escalation", _parse_recipients(env.get(ENV_ESCALATE, "")))
    cfg.setdefault("link_base", env.get("AGENTIC_OS_GATE_NOTIFY_LINK_BASE", ""))
    cfg["outbox"] = env.get(ENV_DB) or cfg.get("outbox") or default_db_path(env)
    return cfg if cfg.get("recipients") else None


class GateNotifier(LifecycleContributor):
    def __init__(self, store, channels: dict[str, Any], config: dict, outbox: Outbox | None = None,
                 clock: Callable[[], float] = time.time):
        self.store, self.channels, self.clock = store, channels, clock
        self.cfg = {**DEFAULTS, **{k: v for k, v in config.items() if k in DEFAULTS}}
        self.recipients = list(config.get("recipients") or [])
        self.escalation = list(config.get("escalation") or [])
        self.outbox = outbox or Outbox(config.get("outbox") or ":memory:")
        self._tick_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ── enqueue side (the lifecycle seam) ──
    def _text(self, kind: str, mission_id: str, node_id: str, capability: str) -> str:
        head = {"initial": "Approval needed", "reminder": "Reminder: approval still pending",
                "escalation": "Escalation: approval pending", "expired": "Approval gate expired (still parked)"}[kind]
        base = self.cfg["link_base"]
        q = urllib.parse.urlencode({"mission": mission_id, "node": node_id, "ack": "{ACK}"}, safe="{}")
        link = f"{base}{'&' if '?' in base else '?'}{q}" if base else f"mission {_safe(mission_id)} node {_safe(node_id)} ack={{ACK}}"
        return (f"{head}: '{_safe(capability)}' in mission {_safe(mission_id)}. Review and decide in the "
                f"authenticated console: {link} . (Replying to this message does not approve anything.)")

    def _fanout(self, who: list[dict], kind: str, mission_id, node_id, version, capability, now) -> int:
        n = 0
        for r in who:
            if self.outbox.enqueue(mission_id=mission_id, node_id=node_id, version=version, recipient=r["id"],
                                   channel=r["channel"], address=r.get("address", ""), kind=kind, now=now,
                                   text=self._text(kind, mission_id, node_id, capability)):
                n += 1
        return n

    def on_gate_reached(self, event: GateReached, capabilities: dict[str, Any]) -> None:
        # GateReached is also dispatched for policy-denied blocks (no human can act) - only real parks notify.
        version = open_gates(self.store, event.mission_id).get(event.node_id)
        if version is None:
            return
        now = self.clock()
        self.outbox.register_gate(event.mission_id, event.node_id, version, event.capability, now)
        self._fanout(self.recipients, "initial", event.mission_id, event.node_id, version, event.capability, now)

    # ── sweep ──
    def tick(self, now: float | None = None) -> dict:
        with self._tick_lock:                       # one sweep at a time per process; claim() covers other processes
            return self._tick(self.clock() if now is None else now)

    def _tick(self, now: float) -> dict:
        stats = {"sent": 0, "failed": 0, "retry": 0, "reminders": 0, "escalations": 0, "expired": 0, "deferred": 0}
        for g in self.outbox.gates():
            self._sweep_gate(g, now, stats)
        self._deliver_due(now, stats)
        self.outbox.prune(now, self.cfg["retention"], self.cfg["rate_per"])
        return stats

    def _sweep_gate(self, g: dict, now: float, stats: dict) -> None:
        if g["resolved_at"] is not None:
            return
        mid, nid, ver = g["mission_id"], g["node_id"], g["gate_version"]
        if str(open_gates(self.store, mid).get(nid)) != ver:     # decided (by the authenticated path) or superseded
            self.outbox._q("UPDATE gates SET resolved_at=? WHERE gate=?", (now, g["gate"]))
            self.outbox._q("UPDATE outbox SET status='cancelled' WHERE gate=? AND status IN ('pending','retrying')",
                           (g["gate"],))
            return
        age, cap = now - g["created_at"], g["capability"]
        if not g["reminded"] and age >= self.cfg["reminder_after"]:
            if not any(r["ack_at"] for r in self.outbox.rows(gate=g["gate"])):
                stats["reminders"] += self._fanout(self.recipients, "reminder", mid, nid, ver, cap, now)
            self.outbox._q("UPDATE gates SET reminded=1 WHERE gate=?", (g["gate"],))
        if not g["escalated"] and age >= self.cfg["escalate_after"]:
            if self.escalation:
                stats["escalations"] += self._fanout(self.escalation, "escalation", mid, nid, ver, cap, now)
            self.outbox._q("UPDATE gates SET escalated=1 WHERE gate=?", (g["gate"],))
        if not g["expired"] and age >= self.cfg["expire_after"]:
            # Record-then-flag: a crash in between re-checks the event log, so exactly one GateExpired.
            if not any(e.type == "GateExpired" and e.payload.get("node_id") == nid
                       and e.payload.get("gate_version") == ver for e in self.store.for_mission(mid)):
                self.store.append("GateExpired", mid, {"node_id": nid, "gate_version": ver, "capability": cap,
                                                       "expired_at": now, "auto_decision": None})
            self.outbox._q("UPDATE gates SET expired=1 WHERE gate=?", (g["gate"],))
            stats["expired"] += 1
            self._fanout(self.recipients + self.escalation, "expired", mid, nid, ver, cap, now)

    def _rate_ok(self, recipient: str, now: float) -> bool:
        lo = now - self.cfg["rate_per"]
        n = self.outbox._q("SELECT COUNT(*) AS n FROM sends WHERE recipient=? AND ts>?", (recipient, lo))[0]["n"]
        return n < self.cfg["rate_max"]

    def _send(self, r: dict):
        from ..channels import SendResult, redact
        ch = self.channels.get(r["channel"])
        if ch is None:
            return SendResult(False, 0, error="no such channel", permanent=True)
        try:
            res = ch.send(r["text"], reply_to=r["address"] or None)
        except Exception as e:  # noqa: BLE001
            return SendResult(False, 0, error=redact(type(e).__name__))
        return res if isinstance(res, SendResult) else SendResult(False, 0, error="no result")

    def _deliver_due(self, now: float, stats: dict) -> None:
        due = self.outbox._q("SELECT * FROM outbox WHERE status IN ('pending','retrying') AND next_attempt<=? "
                             "ORDER BY created_at, key", (now,))
        for r in due:
            if r["provider_msg_id"] or r["sent_at"]:           # receipt known: never re-send
                self.outbox._q("UPDATE outbox SET status='sent' WHERE key=?", (r["key"],))
                continue
            if not self._rate_ok(r["recipient"], now):
                self.outbox._q("UPDATE outbox SET next_attempt=? WHERE key=?",
                               (now + self.cfg["rate_per"] / max(1, self.cfg["rate_max"]), r["key"]))
                stats["deferred"] += 1
                continue
            # atomic lease first: only one worker wins; a crash mid-send leaves the row retryable later
            if not self.outbox.claim(r["key"], r["attempts"], now, max(self.cfg["backoff_base"], 1.0)):
                continue
            attempts = r["attempts"] + 1
            self.outbox._q("INSERT INTO sends(recipient,ts) VALUES(?,?)", (r["recipient"], now))
            res = self._send(r)
            if res.ok:
                self.outbox._q("UPDATE outbox SET status='sent', sent_at=?, provider_msg_id=?, last_error='' "
                               "WHERE key=?", (now, res.message_id or "ok", r["key"]))
                stats["sent"] += 1
            elif res.permanent or attempts >= self.cfg["max_attempts"]:
                self.outbox._q("UPDATE outbox SET status='failed', last_error=? WHERE key=?", (res.error[:200], r["key"]))
                stats["failed"] += 1
            else:
                delay = min(self.cfg["backoff_cap"], self.cfg["backoff_base"] * 2 ** (attempts - 1))
                self.outbox._q("UPDATE outbox SET status='retrying', next_attempt=?, last_error=? WHERE key=?",
                               (now + delay, res.error[:200], r["key"]))
                stats["retry"] += 1

    # ── inbound: acknowledgement only ──
    def handle_callback(self, body: bytes, timestamp: str, signature: str, secret: str,
                        now: float | None = None) -> dict:
        """Verify a relay callback (HMAC-SHA256 over ``v0:<ts>:<body>``, 5-minute window, replay protected) and
        record ACKNOWLEDGED for the opaque ``ref`` carried in the delivered notice. Any other action - approve
        included - is refused. Approval only ever happens on the authenticated control path. The signature is
        consumed only after the whole request validated."""
        now = self.clock() if now is None else now
        if not secret:
            return {"ok": False, "reason": "callbacks disabled"}
        if len(body) > MAX_CALLBACK_BYTES:
            return {"ok": False, "reason": "body too large"}
        if not isinstance(timestamp, str) or not _TS_RE.fullmatch(timestamp):
            return {"ok": False, "reason": "bad timestamp"}
        ts = float(timestamp)
        if not math.isfinite(ts) or abs(now - ts) > CALLBACK_WINDOW:
            return {"ok": False, "reason": "stale timestamp"}
        expect = sign_callback(secret, timestamp, body)
        if not hmac.compare_digest(expect, signature or ""):
            return {"ok": False, "reason": "bad signature"}
        try:
            d = json.loads(body.decode("utf-8"))
        except ValueError:
            return {"ok": False, "reason": "bad body"}
        if not isinstance(d, dict):
            return {"ok": False, "reason": "bad body"}
        if d.get("action") != "ack" or not isinstance(d.get("ref"), str) or not d["ref"]:
            return {"ok": False, "reason": "only 'ack' with a ref is accepted; approval requires the authenticated console"}
        if self.outbox.sig_seen_or_record(expect, now):
            return {"ok": False, "reason": "replay"}
        if not self.outbox.acknowledge(d["ref"], now):
            return {"ok": False, "reason": "unknown notification"}
        return {"ok": True, "acknowledged": True}

    # ── metrics hooks ──
    def time_to_decision(self) -> list[dict]:
        """Per notified gate: seconds from its NodeParked event to the human decision event
        (NodeResumed/NodeSkipped), from the event log itself - no tick needed. Superseded gates (re-parked
        before a decision) and still-open gates are excluded."""
        out = []
        for g in self.outbox.gates():
            parked = None
            for e in self.store.for_mission(g["mission_id"]):
                if e.type == "NodeParked" and e.payload.get("node_id") == g["node_id"]:
                    if parked is not None:
                        break                                   # re-parked before a decision: superseded
                    if str(e.seq) == g["gate_version"]:
                        parked = e
                elif parked is not None and e.type in ("NodeResumed", "NodeSkipped") \
                        and e.payload.get("node_id") == g["node_id"]:
                    out.append({"gate": g["gate"], "seconds": e.ts - parked.ts})
                    break
        return out

    # ── optional background sweep ──
    def start(self, interval: float = 5.0) -> threading.Thread:
        def loop():
            while not self._stop.wait(interval):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 - the sweep must never die
                    pass
        self._thread = threading.Thread(target=loop, name="gate-notify", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()


def sign_callback(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), b"v0:" + str(timestamp).encode() + b":" + body, hashlib.sha256).hexdigest()


_CURRENT: GateNotifier | None = None


def current() -> GateNotifier | None:
    return _CURRENT


def install_from_env(runtime, env: dict | None = None, start_thread: bool = True) -> GateNotifier | None:
    """Opt-in wiring: returns None (and changes nothing) unless enabled AND a recipient is configured.

    This is the hook ANY host (demo unified_server today; the production mission API later) calls once per
    runtime: ``gate_notify.install_from_env(runtime)``. Idempotent per runtime. Refuses a non-durable outbox."""
    global _CURRENT
    cfg = load_config(env)
    if cfg is None:
        return None
    if cfg.get("outbox") in (None, "", ":memory:"):
        warnings.warn("gate notifications disabled: refusing a non-durable (:memory:) outbox", RuntimeWarning)
        return None
    if _CURRENT is not None and _CURRENT.store is runtime.store:
        return _CURRENT
    from ..channels import load_channels
    channels = {c.name: c for c in load_channels()}
    if not any(r["channel"] in channels for r in cfg["recipients"]):
        return None
    try:
        n = GateNotifier(runtime.store, channels, cfg)
    except (sqlite3.Error, OSError) as e:       # fail closed: the mission runtime is unaffected
        warnings.warn(f"gate notifications disabled: outbox unavailable ({type(e).__name__})", RuntimeWarning)
        return None
    runtime.lifecycle.install(n)
    if start_thread:
        n.start()
    _CURRENT = n
    return n

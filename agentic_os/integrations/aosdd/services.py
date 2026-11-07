"""Wiring: the redacting hash-chained ledger, and the bundle of services the operator client and the runtime share."""
from __future__ import annotations

import time

from agentic_os.mission.store import EventStore, MemoryEventStore
from agentic_os.mission.types import to_jsonable

from .apps import adapters
from .config import AosddConfig, ConfigError
from .http import AppHttp, Transport, UrllibTransport, auth_for
from .oplog import OpLog, new_owner
from .redact import Redactor
from .verify import ReadBack


class _Redacting:
    """Mixin: every payload is scrubbed BEFORE it is hashed and written, whatever produced it (a result, an error text, a
    reconciliation note). A credential can therefore not reach the ledger, even from code that was never told about it."""
    _redactor: Redactor

    def append(self, type, mission_id, payload):                        # noqa: A003
        return super().append(type, mission_id, self._redactor.scrub(to_jsonable(payload)))


class RedactingEventStore(_Redacting, EventStore):
    def __init__(self, path: str, redactor: Redactor):
        self._redactor = redactor
        super().__init__(path)


class RedactingMemoryStore(_Redacting, MemoryEventStore):
    def __init__(self, redactor: Redactor):
        self._redactor = redactor
        super().__init__()


class Services:
    def __init__(self, cfg: AosddConfig, store, oplog: OpLog, guard, *, transport: "Transport | None" = None,
                 clock=time.time, owner: "str | None" = None):
        self.cfg, self.store, self.oplog, self.guard, self.clock = cfg, store, oplog, guard, clock
        if oplog.lease_ttl_s < cfg.http_timeout_s:
            raise ConfigError(f"the operation lease ({oplog.lease_ttl_s}s) is shorter than the HTTP timeout ({cfg.http_timeout_s}s): a request could outlive its lease")
        self.transport = transport or UrllibTransport()
        self.owner = owner or new_owner()
        self.writers = {a: AppHttp(a, cfg.base_urls[a], auth_for(a, cfg.writer[a]), self.transport, cfg.redactor, timeout=cfg.http_timeout_s, role="writer") for a in cfg.writer}
        self.adapters = adapters(self.writers, cfg.base_urls, cfg.account_id)
        self.readback = ReadBack(cfg, self.transport)        # its own reader clients: no object shared with self.writers

    def approval_key(self) -> str:
        """The plan-approval signing key (its own secret source, never an application credential)."""
        return self.cfg.approval_secret.reveal()

    def emit(self, type: str, mission_id: str, payload: dict):          # noqa: A003
        return self.store.append(type, mission_id, payload)

    def plan_for(self, mission_id: str) -> "dict | None":
        """The plan the LEDGER holds now (last successful plan.build result), never an in-memory copy."""
        plan = None
        for e in self.store.for_mission(mission_id):
            if e.type == "NodeSucceeded" and e.payload.get("capability") == "aosdd.plan.build":
                plan = (e.payload.get("result") or {}).get("plan")
        return plan

    def approval_for(self, mission_id: str) -> "dict | None":
        a = None
        for e in self.store.for_mission(mission_id):
            if e.type == "AosddPlanApproved":
                a = e.payload
        return a

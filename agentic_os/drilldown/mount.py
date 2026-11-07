"""Mounting the drill-down in the Projects app: configuration, who the caller is, and what the server says about its data source.

**Configuration.** The drill-down is mounted only when the app is given a runtime-backed event store, a grant store, an audit sink and
the project the store's missions belong to: by injection (``create_app(drilldown=DrilldownConfig(...))``) or from explicit
environment keys (``drilldown_from_env``). No path is invented here; a half-set environment is an error at start-up, never a quiet
fall-back to something else.

**Who the caller is** (the session-to-principal bridge). There are two disjoint authentication paths, chosen by how the app was built:

* behind the local capability gate (the desktop sidecar): the gate validates the launch-capability session and records WHICH session
  it validated in the ASGI scope; :class:`LocalSessionBridge` re-checks that session with the capability and only then issues the
  ``local-owner`` principal (method ``local-capability``, the local tenant). Nothing in the request (header, query, body, cookie) can
  select, name or mint a principal, and the browser holds no bearer token. The principal still has to pass the permission plane:
  ``evidence.<project>.inspect`` is checked on every request and a fresh installation has no such grant (``operator.py``).
* without the gate (hosted): ``Authorization: Bearer`` through ``control_auth`` (:class:`ControlTokenAuthenticator`), or an
  authenticator the deployment injects. A capability-session header means nothing there.

**What the server says about its data** (``GET /api/ui-config``, schema ``aos.ui-config/v1``): ``runtime`` only when a store is
configured AND the routes are mounted, else ``sample``; durability and integrity facts are the ones the store's own checks return, and
are given only to an authenticated caller.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from ..evidence_query.audit import AuditSink, JsonlAuditSink
from ..evidence_query.authz import Authenticator, Ceiling, GrantsCeiling
from ..local_capability import SESSION_SCOPE_KEY, LocalCapability
from ..overlays import DEFAULT_TENANT, Principal
from .models import UI_CONFIG_SCHEMA_ID
from .operator import LOCAL_OWNER_ID

LOCAL_METHOD = "local-capability"

ENV_EVENT_PATH = "PROJECTS_DRILLDOWN_EVENT_PATH"
ENV_EVENT_BACKEND = "PROJECTS_DRILLDOWN_EVENT_BACKEND"
ENV_PROJECT = "PROJECTS_DRILLDOWN_PROJECT"
ENV_GRANTS_DIR = "PROJECTS_DRILLDOWN_GRANTS_DIR"
ENV_AUDIT_PATH = "PROJECTS_DRILLDOWN_AUDIT_PATH"
ENV_AUTHORITY_DB = "PROJECTS_DRILLDOWN_AUTHORITY_DB"
_PROJECT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class DrilldownConfigError(ValueError):
    """The drill-down is configured incompletely or inconsistently. The server does not start rather than guess."""


@dataclass
class DrilldownConfig:
    """Everything the mounted drill-down needs, injected. ``project_id`` is the permission scope of every mission in ``store`` (the log does
    not yet record a project: contract-gap row 8). ``grants`` is the permission plane's grant store; ``ceiling`` overrides how it is read.
    ``authenticator`` is the hosted Bearer path (default: ``control_auth`` tokens); it is not used behind the local capability gate."""
    store: Any
    grants: Any
    audit: AuditSink
    project_id: str
    ceiling: Optional[Ceiling] = None
    resolver: Optional[Callable[[str], Any]] = None
    authenticator: Optional[Authenticator] = None
    authority_store: Any = None
    principal_max_age_s: float = 300.0

    def __post_init__(self) -> None:
        if self.store is None or not all(hasattr(self.store, a) for a in ("for_mission", "mission_ids")):
            raise DrilldownConfigError("the drill-down needs an event store (for_mission, mission_ids)")
        if self.grants is None and self.ceiling is None:
            raise DrilldownConfigError("the drill-down needs a grant store (or a permission ceiling)")
        if self.audit is None or not callable(getattr(self.audit, "append", None)):
            raise DrilldownConfigError("the drill-down needs an audit sink: a read that cannot be recorded is not served")
        if not isinstance(self.project_id, str) or not _PROJECT.match(self.project_id):
            raise DrilldownConfigError("project_id must be a permission scope (lower-case letters, digits, - and _)")

    def permission_ceiling(self) -> Ceiling:
        if self.ceiling is not None:
            return self.ceiling
        from ..permissions import PermissionsPlane              # noqa: PLC0415
        return GrantsCeiling(PermissionsPlane(self.grants))


def drilldown_from_env(env: Optional[Dict[str, str]] = None) -> Optional[DrilldownConfig]:
    """``None`` when none of the keys is set (not configured: sample mode). All of the four required keys or an error."""
    e = dict(os.environ if env is None else env)
    keys = (ENV_EVENT_PATH, ENV_PROJECT, ENV_GRANTS_DIR, ENV_AUDIT_PATH)
    if not any((e.get(k) or "").strip() for k in (*keys, ENV_EVENT_BACKEND, ENV_AUTHORITY_DB)):
        return None
    missing = [k for k in keys if not (e.get(k) or "").strip()]
    if missing:
        raise DrilldownConfigError("the drill-down is partly configured; also set " + ", ".join(missing) + " (or unset the others)")
    backend = (e.get(ENV_EVENT_BACKEND) or "jsonl").strip().lower()
    if backend not in ("jsonl", "duckdb"):
        raise DrilldownConfigError(f"{ENV_EVENT_BACKEND} must be 'jsonl' or 'duckdb': a memory store holds nothing a restart keeps")
    project = e[ENV_PROJECT].strip()
    if not _PROJECT.match(project):
        raise DrilldownConfigError(f"{ENV_PROJECT} must be a permission scope (lower-case letters, digits, - and _)")
    path = e[ENV_EVENT_PATH].strip()
    if backend == "jsonl":
        from ..mission.store import EventStore               # noqa: PLC0415
        store = EventStore(path=path, strict=False)          # a reader: it never appends, and a log that fails its anchor is REPORTED, not refused
    else:
        from ..mission.event_backends import open_event_store  # noqa: PLC0415
        store = open_event_store("duckdb", path=path)
    from ..permissions import GrantStore                    # noqa: PLC0415
    authority = None
    if (e.get(ENV_AUTHORITY_DB) or "").strip():
        from ..mission.authority import SqliteEnvelopeStore  # noqa: PLC0415
        authority = SqliteEnvelopeStore(e[ENV_AUTHORITY_DB].strip())
    return DrilldownConfig(store=store, grants=GrantStore(e[ENV_GRANTS_DIR].strip()), audit=JsonlAuditSink(e[ENV_AUDIT_PATH].strip()),
                           project_id=project, authority_store=authority)


# ───────────────────────── who is calling ─────────────────────────
class LocalSessionBridge:
    """Turns "this request passed the local capability gate" into the ``local-owner`` principal, and nothing else into anything.

    The only input is the session object the gate placed in the ASGI scope for this request; it is re-checked against the capability
    that issued it (so a look-alike, an expired or a signed-out session yields no principal). The principal is issued per request by the
    process's authentication layer (``mission.principal``) and carries its proof; it is then judged by the permission plane like any other."""

    def __init__(self, capability: LocalCapability, issuer: Any = None) -> None:
        self.capability = capability
        self._issuer = issuer

    def principal_of(self, request) -> Optional[Any]:
        try:
            sess = request.scope.get(SESSION_SCOPE_KEY)
            if sess is None or not self.capability.session_is_live(sess):
                return None
            issuer = self._issuer
            if issuer is None:
                from ..mission.principal import default_issuer   # noqa: PLC0415
                issuer = default_issuer()
            return issuer.issue(Principal(LOCAL_OWNER_ID, "user", (), DEFAULT_TENANT), method=LOCAL_METHOD)
        except Exception:                                    # noqa: BLE001 - no issuer, a broken capability: nobody (fail closed)
            return None


class ControlTokenAuthenticator:
    """Hosted Bearer path: the credential is one of the ``control_auth`` tokens (admin, tenant, approver). The principal is
    ``admin`` / ``requester:<tenant>`` / ``approver:<tenant>`` with that tenant (``*`` for admin) and NO roles, so it needs an explicit
    ``evidence.<project>.inspect`` grant like everyone else: the permission plane's ``admin`` role bypass is not inherited. Nothing configured, a
    wrong token or an invalid token configuration authenticates nobody."""

    def authenticate(self, credential: Optional[str]) -> Optional[Any]:
        if not credential or not isinstance(credential, str):
            return None
        try:
            from ..mission.control_auth import resolve_caller    # noqa: PLC0415
            from ..mission.principal import default_issuer, principal_of_caller  # noqa: PLC0415
            caller = resolve_caller("Bearer " + credential)
            p = principal_of_caller(caller)
            return default_issuer().issue(Principal(p.id, "user", (), p.tenant), method="control-token")
        except Exception:                                    # noqa: BLE001 - HTTPException (401/503), ConfigError: nobody
            return None


class BearerPrincipals:
    """``principal_of(request)`` over an ``Authenticator``: ``Authorization: Bearer <credential>`` and nothing else."""

    def __init__(self, authenticator: Authenticator) -> None:
        self.authenticator = authenticator

    def principal_of(self, request) -> Optional[Any]:
        h = request.headers.get("authorization", "")
        if not h.lower().startswith("bearer "):
            return None
        try:
            return self.authenticator.authenticate(h[7:].strip())
        except Exception:                                    # noqa: BLE001 - a broken authenticator authenticates nobody
            return None


# ───────────────────────── the mounted pieces ─────────────────────────
def _service_of(cfg: DrilldownConfig):
    from .service import DrilldownService                    # noqa: PLC0415
    project = cfg.project_id
    store = cfg.store

    def scope_for(mission_id: str) -> Optional[str]:
        return project if mission_id in set(store.mission_ids()) else None
    return DrilldownService(store, cfg.permission_ceiling(), cfg.audit, scope_for=scope_for, resolver=cfg.resolver,
                            principal_max_age_s=cfg.principal_max_age_s)


class DrilldownMount:
    """The drill-down as installed in one app: the service, the principal source, and the ui-config report."""

    def __init__(self, cfg: DrilldownConfig, capability: Optional[LocalCapability]) -> None:
        self.cfg = cfg
        self.service = _service_of(cfg)
        if capability is not None:
            self.principals: Any = LocalSessionBridge(capability)              # behind the gate: the gate's session, never a bearer
        else:
            self.principals = BearerPrincipals(cfg.authenticator or ControlTokenAuthenticator())

    def install(self, app) -> None:
        from .api import build_router                                           # noqa: PLC0415
        app.include_router(build_router(self.service, None, principal_source=self.principals))

    # -- integrity facts, only what the store's own checks return --
    def _integrity(self) -> dict:
        store = self.cfg.store
        fn = getattr(store, "check_anchor", None) or getattr(store, "check_integrity", None)
        if not callable(fn):
            return {"status": "not_reported"}
        try:
            rep = fn()
        except Exception:                                    # noqa: BLE001
            return {"status": "not_reported"}
        from .models import ANCHOR_STATUSES                  # noqa: PLC0415
        status = rep.status if rep.status in ANCHOR_STATUSES else "not_reported"
        if status == "not_reported":
            return {"status": status}
        return {"status": status, "records": int(rep.count), "anchored_records": int(rep.anchored)}

    def report(self, request, provider: Any) -> dict:
        from ..projects_api import SampleProjectionProvider  # noqa: PLC0415
        known = self.principals.principal_of(request) is not None
        integ = self._integrity() if known else {"status": "not_reported"}
        stores = []
        if known:
            from .fold import store_descriptor               # noqa: PLC0415
            d = store_descriptor(self.cfg.store)
            stores.append({"role": "event_log", "kind": d["backend"], "durable": d["durability"] == "durable", "integrity": integ})
        authority: dict = {"configured": False}
        if self.cfg.authority_store is not None:
            authority = {"configured": True}
            if known:
                try:
                    self.cfg.authority_store.verify()
                    authority["verified"] = True
                except Exception:                            # noqa: BLE001 - a failed check names nothing else
                    authority["verified"] = False
        return {"schema": UI_CONFIG_SCHEMA_ID, "data_source": "runtime",
                "projections": {"basis": "sample" if isinstance(provider, SampleProjectionProvider) else "unreported"},
                "drilldown": {"mounted": True, "integrity": integ}, "stores": stores, "authority": authority}


def sample_report(provider: Any) -> dict:
    from ..projects_api import SampleProjectionProvider      # noqa: PLC0415
    return {"schema": UI_CONFIG_SCHEMA_ID, "data_source": "sample",
            "projections": {"basis": "sample" if isinstance(provider, SampleProjectionProvider) else "unreported"},
            "drilldown": {"mounted": False, "integrity": {"status": "not_reported"}}, "stores": [], "authority": {"configured": False}}

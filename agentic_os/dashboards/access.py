"""Who may see which projects and which sources on the dashboards.

The dashboards add no new authority and no new exposure:

* the PRINCIPAL is the one Projects already resolves for a human decision (``agentic_os.products.principal``): the
  desktop session, the operator's Basic credential, a control-plane bearer (admin / approver:<tenant> /
  requester:<tenant>) or the single owner the operator declared;
* a tenant bearer sees only what its tenant was granted in ``AGENTIC_OS_TENANT_LIMITS`` (``dashboards:<source>`` or
  ``dashboards:*`` for sources, ``project:<id>`` or ``project:*`` for projects) — the same grant list every other
  tenant permission comes from;
* an Enterprise overlay may replace the whole decision by giving the provider a ``dashboard_access(principal)`` method
  (its permission plane decides; this module then only applies the answer);
* the operator's ``access`` section in the dashboards file names what an anonymous caller and each named principal may
  see. With an ``access`` section present, anything not granted is refused.
* with NO ``access`` section and no tenant grant, the dashboards show what the deployment's other read routes already
  show to the same caller (missions, attention, registered products, runtime), and the business sources only if the
  operator enabled them — the boundary is unchanged, not widened.

An aggregate is computed ONLY over records the caller may read. A metric that would need a source or a project the
caller may not see is reported as not known, with the reason, never as a partial number that looks complete.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Tuple

from .config import ALL, NONE, DashboardsConfig, Grant


@dataclass(frozen=True)
class Access:
    principal: str                    # "" = anonymous
    grant: Grant
    basis: str                        # "overlay" | "tenant" | "config" | "deployment"

    def source(self, sid: str) -> bool:
        return self.grant.sources is None or sid in self.grant.sources

    def project(self, pid: str) -> bool:
        if self.grant.projects is None:
            return True
        return bool(pid) and pid in self.grant.projects      # a record attributed to no project is not shown to a scoped caller

    @property
    def scoped(self) -> bool:
        return self.grant.projects is not None or self.grant.sources is not None

    @property
    def owner(self) -> str:
        """The key saved views are stored under. Anonymous callers share nothing: they cannot save."""
        return self.principal


def _tenant_grant(tenant: str) -> Optional[Grant]:
    try:
        from agentic_os.mission.control_auth import ConfigError, tenant_limits  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        refs, _ = tenant_limits(tenant)
    except ConfigError:
        return NONE
    refs = [str(r) for r in (refs or [])]
    src = [r.split(":", 1)[1] for r in refs if r.startswith("dashboards:")]
    prj = [r.split(":", 1)[1] for r in refs if r.startswith("project:")]
    if not src:
        return NONE
    return Grant(projects=None if "*" in prj else tuple(prj), sources=None if "*" in src else tuple(src))


def resolve(config: DashboardsConfig, provider: Any, principal: Optional[str]) -> Access:
    p = principal or ""
    overlay = getattr(provider, "dashboard_access", None)
    if overlay is not None:
        try:
            g = overlay(p)
        except Exception:  # noqa: BLE001  (a failing permission plane grants nothing)
            g = None
        if isinstance(g, Grant):
            return Access(p, g, "overlay")
        if isinstance(g, dict):
            def one(v: Any) -> Optional[Tuple[str, ...]]:
                return None if v == "*" else tuple(str(x) for x in (v or ()))
            return Access(p, Grant(projects=one(g.get("projects")), sources=one(g.get("sources"))), "overlay")
        return Access(p, NONE, "overlay")
    if ":" in p and p.split(":", 1)[0] in ("approver", "requester"):
        tg = _tenant_grant(p.split(":", 1)[1])
        if p in config.principals:
            return Access(p, config.principals[p], "config")
        if tg is not None and (tg is not NONE or not config.access_configured):
            return Access(p, tg, "tenant")
    if config.access_configured:
        if p and p in config.principals:
            return Access(p, config.principals[p], "config")
        if p in ("admin", "operator", "local-owner"):
            return Access(p, ALL, "config")
        return Access(p, config.anonymous if not p else NONE, "config")
    return Access(p, ALL, "deployment")


def visible_projects(access: Access, project_ids: Iterable[str]) -> list:
    return [pid for pid in project_ids if access.project(pid)]


__all__ = ["Access", "resolve", "visible_projects"]

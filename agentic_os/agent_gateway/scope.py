"""What an external agent's tenant may read on a deployment that has tenants.

A deployment declares tenants in ``AGENTIC_OS_TENANT_LIMITS``. Without it there is one owner and nothing to separate, so
reads are not scoped. With it, an agent reads only what its token's tenant is granted (``project:<id>`` / ``project:*``
and ``dashboards:<source>`` / ``dashboards:*``), the same grant list that scopes that tenant's own sign-in. Everything
fails closed: a tenant with no entry, an expired one, or limits that cannot be read, sees nothing.
"""
from __future__ import annotations

import os
import time
from typing import Callable, Optional

from .contracts import GatewayPrincipal

ProjectVisible = Callable[[str], bool]


def _nothing(_project_id: str) -> bool:
    return False


def tenant_is_current(tenant: str, *, clock: Callable[[], float] = time.time) -> bool:
    """False for an unnamed tenant, an expired one, or one whose expiry cannot be read."""
    from agentic_os.mission.control_auth import ConfigError, tenant_expiry  # noqa: PLC0415
    if not tenant:
        return False
    try:
        end = tenant_expiry(tenant)
    except ConfigError:
        return False
    return end is None or end > clock()


def tenant_project_scope(principal: GatewayPrincipal) -> Optional[ProjectVisible]:
    """``None`` when the deployment has no tenants (nothing to scope); otherwise whether this caller may see a project."""
    from agentic_os.mission.control_auth import ENV_TENANT_LIMITS, ConfigError, tenant_limits  # noqa: PLC0415
    if not os.environ.get(ENV_TENANT_LIMITS, "").strip():
        return None
    tenant = principal.tenant
    if not tenant_is_current(tenant):
        return _nothing
    try:
        refs, _ = tenant_limits(tenant)
    except ConfigError:
        return _nothing
    granted = {r.split(":", 1)[1] for r in refs if r.startswith("project:")}
    return lambda project_id: "*" in granted or str(project_id) in granted

"""May this agent have this operation? One answer, with a reason a person can act on: PERMITTED, APPROVAL_REQUIRED or DENIED.

Asked when a lookup is made or a change is proposed, again when a person is about to approve it, and again just before it
is performed. **An approval never turns DENIED into anything else**: it only satisfies APPROVAL_REQUIRED.

In order, the first that refuses wins:

1. the operation is classified (``tool_actions.TOOL_ACTIONS["governed-writes"]``), else denied;
2. **tenant restriction**: on a deployment with tenants, the tenant is granted the application (``app:<name>``) and has not expired;
3. **the deployment's permissions plane**, when permissions are requested: BASIC permissions authorise the app instance,
   so the local profile needs the grant on ``governed-writes.<tool>``; ENTERPRISE permissions are asked about the agent's own
   identity (user ``agent:<id>``, role ``external-agent``, its tenant). No gate installed means denied;
4. a change needs a person's approval, and **someone must be able to give it**: otherwise it is denied rather than left waiting
   for nobody;
5. for a change that is requested through ``GovernedWrites``: **the requesting agent's own stored authority covers it**
   (``GovernedWrites.authority_refusal``): scoped to the grants a person gave it, expiring, revocable. Approval alone is
   not authority, and revoking an agent stops its waiting requests.

The gateway's named grants (``erp.propose`` ...) are checked before any of this, by the gateway.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from agentic_os import app_gate
from agentic_os.tool_actions import TOOL_ACTIONS

#: the name this plane is gated and classified under
APP = "governed-writes"


class Verdict(str, Enum):
    PERMITTED = "permitted"                  # may be done now: lookups and following one's own requests
    APPROVAL_REQUIRED = "approval_required"  # may be proposed; done only after a person approves
    DENIED = "denied"                        # may not be done, and no approval changes that


@dataclass(frozen=True)
class Authorization:
    verdict: Verdict
    code: str = ""                           # tenant_not_granted | permission_denied | no_approver | unclassified | no_authority
    reason: str = ""

    @property
    def denied(self) -> bool:
        return self.verdict is Verdict.DENIED

    @property
    def error(self) -> str:
        return f"not_authorized ({self.code}): {self.reason}"


def _denied(code: str, reason: str) -> Authorization:
    return Authorization(Verdict.DENIED, code, reason[:400])


def tenant_may_use_app(tenant: str, app: str) -> bool:
    """On a deployment with tenants: whether this tenant is granted this application and is current. Fail closed."""
    from agentic_os.agent_gateway.scope import tenant_is_current  # noqa: PLC0415
    from agentic_os.mission.control_auth import ENV_TENANT_LIMITS, ConfigError, tenant_limits  # noqa: PLC0415
    if not os.environ.get(ENV_TENANT_LIMITS, "").strip():
        return True
    if not tenant_is_current(tenant):
        return False
    try:
        refs, _ = tenant_limits(tenant)
    except ConfigError:
        return False
    return "app:*" in refs or f"app:{app}" in refs


def tenant_holds_financial(tenant: str, source: str) -> bool:
    """Whether this TENANT may receive this application's financial data (agentic_os/financial_access.py). On a deployment
    without tenants there is one owner and no ceiling. Fail closed otherwise. (The demonstration grant covers dashboards
    over declared synthetic sources, never record lookups or proposals.)"""
    from agentic_os import financial_access  # noqa: PLC0415
    from agentic_os.agent_gateway.scope import tenant_is_current  # noqa: PLC0415
    from agentic_os.mission.control_auth import ENV_TENANT_LIMITS, ConfigError, tenant_limits  # noqa: PLC0415
    if not os.environ.get(ENV_TENANT_LIMITS, "").strip():
        return True
    if not tenant_is_current(tenant):
        return False
    try:
        refs, _ = tenant_limits(tenant)
    except ConfigError:
        return False
    return financial_access.covers(financial_access.covered(refs), source)


def approver_exists(tenant: str, *, also: Optional[Callable[[], bool]] = None) -> bool:
    """Whether some configured person could decide a change of this tenant: the operator's sign-in, the admin credential,
    the tenant's own approver with ``writes:decide`` among the tenant's policy_refs (what the decision route requires), or
    what the mount adds (a declared single owner). Requesters never decide changes."""
    from agentic_os.app_kit.route_authz import operator_credential  # noqa: PLC0415
    from agentic_os.mission.control_auth import ConfigError, approver_tokens, configured_token, tenant_limits  # noqa: PLC0415
    user, password = operator_credential()
    if (user and password) or configured_token():
        return True
    try:
        if tenant in approver_tokens() and "writes:decide" in tenant_limits(tenant)[0]:
            return True
    except ConfigError:
        return False
    return bool(also and also())


def authorize(tool: str, *, app: str, requester: str, tenant: str, someone_else_can_approve: Optional[Callable[[], bool]] = None,
              needs_approval: bool = True) -> Authorization:
    action = TOOL_ACTIONS.get(APP, {}).get(tool)
    if action is None:
        return _denied("unclassified", f"{tool} has no reviewed classification")
    if not tenant_may_use_app(tenant, app):
        return _denied("tenant_not_granted", f"the tenant {tenant} is not granted the application {app} here "
                                             f"(add app:{app} to its policy_refs in AGENTIC_OS_TENANT_LIMITS)")
    why = app_gate.requester_denial(APP, tool, requester=requester, tenant=tenant)
    if why:
        return _denied("permission_denied", f"this deployment's permissions do not allow {tool}: {why}")
    if action == "read" or not needs_approval:       # a write that only makes new local files waits for nobody; every
        return Authorization(Verdict.PERMITTED)      # other check above applies to it as to any write
    if not approver_exists(tenant, also=someone_else_can_approve):
        return _denied("no_approver", f"nobody on this deployment can approve a change of tenant {tenant}, so nothing was requested: "
                                      "configure the operator sign-in, or an approver for the tenant "
                                      "(AGENTIC_OS_APPROVER_TOKENS, with writes:decide among its policy_refs)")
    return Authorization(Verdict.APPROVAL_REQUIRED)


__all__ = ["APP", "Verdict", "Authorization", "authorize", "approver_exists", "tenant_may_use_app"]

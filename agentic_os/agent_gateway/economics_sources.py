"""E-mail and internal-messenger evidence for the economics tools: the deployment's own configuration of
``agentic_os.economics.mail``'s read-only collectors.

A mailbox or a set of channels belongs to ONE tenant. On a deployment with tenants the configuration must name it
(``..._TENANT``); without a name nothing is read, because the application grant alone (``app:*`` includes everything) must
not hand one organisation's mail to another. The access scope of each message is its own mailbox folder or channel, and a
grant covers exactly the folders and channels configured here, nothing by pattern.
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from agentic_os.economics import evidence, mail
from agentic_os.economics.contracts import Extraction

_log = logging.getLogger("agentic_os.agent_gateway.economics_sources")
IMAP, SLACK = "AGENTIC_OS_ECONOMICS_IMAP_", "AGENTIC_OS_ECONOMICS_SLACK_"
DEFAULT_SINCE_DAYS, MAX_SINCE_DAYS = 180, 3660
MAX_FOLDERS, MAX_CHANNELS = 10, 20


def _list(value: str) -> Tuple[str, ...]:
    return tuple(dict.fromkeys(x.strip() for x in value.split(",") if x.strip()))


def _private(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return host == "localhost" or host.endswith(".svc.cluster.local")
    return ip.is_private or ip.is_loopback


@dataclass
class _Source:
    tenant: str                                       # "" = a deployment without tenants

    def serves(self, tenant: str) -> bool:
        from agentic_os.mission.control_auth import ENV_TENANT_LIMITS  # noqa: PLC0415
        if self.tenant:
            return tenant == self.tenant
        return not os.environ.get(ENV_TENANT_LIMITS, "").strip()


@dataclass
class MailSource(_Source):
    host: str = ""
    user: str = ""
    password: str = field(default="", repr=False)
    folders: Tuple[str, ...] = ("INBOX",)
    own_domains: Tuple[str, ...] = ()
    port: int = 993
    ssl: bool = True
    since_days: int = DEFAULT_SINCE_DAYS
    imap_factory: Optional[Callable[..., Any]] = field(default=None, repr=False)

    def scopes(self) -> List[str]:
        return [f"mailbox:{self.user}/{folder}" for folder in self.folders]

    def collect(self, ex: Extraction, *, tenant: str, identity: evidence.IdentityMap, now_ms: int) -> List[evidence.Communication]:
        since = (_dt.datetime.fromtimestamp(now_ms / 1000, _dt.timezone.utc).date() - _dt.timedelta(days=self.since_days)).isoformat()
        out: List[evidence.Communication] = []
        for folder in self.folders:
            out += mail.collect_imap(self.host, self.user, self.password, ex, tenant=tenant, identity=identity, own_domains=self.own_domains,
                                     folder=folder, since=since, port=self.port, ssl=self.ssl, now_ms=now_ms, imap_factory=self.imap_factory)
        # said, so that an older message's absence is never read as "nobody wrote"
        ex.gap("mail:history", f"only messages since {since} were read", "an older message would not be here")
        return mail.dedupe(out)[0]


@dataclass
class MessengerSource(_Source):
    provider: Any = field(default=None, repr=False)
    channels: Tuple[str, ...] = ()
    shared_channels: Tuple[str, ...] = ()
    workspace: str = ""
    own_domains: Tuple[str, ...] = ()

    def scopes(self) -> List[str]:
        return [f"messenger:{self.provider.provider}/{self.workspace or 'workspace'}/channel:{channel}" for channel in self.channels]

    def collect(self, ex: Extraction, *, tenant: str, identity: evidence.IdentityMap, now_ms: int) -> List[evidence.Communication]:
        return mail.collect_messenger(self.provider, self.channels, ex, tenant=tenant, identity=identity, own_domains=self.own_domains,
                                      shared_channels=self.shared_channels, workspace=self.workspace, now_ms=now_ms)


def mail_from_env(env: Optional[Mapping[str, str]] = None) -> Optional[MailSource]:
    """The configured mailbox, or None. An incomplete or unsafe configuration is refused as a whole, with a reason."""
    e = os.environ if env is None else env
    get = lambda k: (e.get(IMAP + k) or "").strip()  # noqa: E731
    if not get("HOST"):
        return None
    folders, domains = _list(get("FOLDERS") or "INBOX"), _list(get("OWN_DOMAINS"))
    encrypted = get("SSL") != "0"
    try:
        port, days = int(get("PORT") or (993 if encrypted else 143)), int(get("SINCE_DAYS") or DEFAULT_SINCE_DAYS)
    except ValueError:
        port, days = 0, 0
    why = ("a user and a password are required" if not (get("USER") and e.get(IMAP + "PASSWORD")) else
           f"{IMAP}OWN_DOMAINS is required: it is how a customer's message is told from your own" if not domains else
           "an unencrypted connection is allowed only to a private or local address" if not encrypted and not _private(get("HOST")) else
           f"name 1 to {MAX_FOLDERS} folders" if not 1 <= len(folders) <= MAX_FOLDERS else
           f"{IMAP}PORT and {IMAP}SINCE_DAYS (1 to {MAX_SINCE_DAYS}) must be whole numbers" if not (0 < port < 65536 and 1 <= days <= MAX_SINCE_DAYS)
           else "")
    if why:
        _log.error("the mailbox is not used as evidence: %s", why)
        return None
    return MailSource(get("TENANT"), host=get("HOST"), user=get("USER"), password=e[IMAP + "PASSWORD"], folders=folders,
                      own_domains=domains, port=port, ssl=encrypted, since_days=days)


def messenger_from_env(env: Optional[Mapping[str, str]] = None) -> Optional[MessengerSource]:
    e = os.environ if env is None else env
    get = lambda k: (e.get(SLACK + k) or "").strip()  # noqa: E731
    if not get("BOT_TOKEN"):
        return None
    channels = _list(get("CHANNELS"))
    shared = _list(get("SHARED_CHANNELS"))
    why = (f"name 1 to {MAX_CHANNELS} channels in {SLACK}CHANNELS" if not 1 <= len(channels) <= MAX_CHANNELS else
           f"{SLACK}WORKSPACE is required: it is part of each message's access scope" if not get("WORKSPACE") else
           f"every shared channel must also be listed in {SLACK}CHANNELS" if not set(shared) <= set(channels) else "")
    if why:
        _log.error("the messenger is not used as evidence: %s", why)
        return None
    from agentic_os.collaboration.slack import SlackCollaborationProvider  # noqa: PLC0415
    return MessengerSource(get("TENANT"), provider=SlackCollaborationProvider(get("BOT_TOKEN")), channels=channels, shared_channels=shared,
                           workspace=get("WORKSPACE"), own_domains=_list(get("OWN_DOMAINS")))


def from_env() -> Dict[str, Any]:
    found = {"mail": mail_from_env(), "messenger": messenger_from_env()}
    return {name: source for name, source in found.items() if source is not None}


__all__ = ["MailSource", "MessengerSource", "mail_from_env", "messenger_from_env", "from_env", "IMAP", "SLACK"]

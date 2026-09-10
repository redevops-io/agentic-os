"""Hosted OAuth bootstrap — click-only Connect for a *served* deployment.

The three layers, made concrete (see the credentials guide §0.5):

1. **Provider registration** — a ReDevOps deployment registers ONE OAuth app per provider,
   once, out of band, and stores its ``client_id`` + ``client_secret`` in deployment secrets
   (here: env vars like ``SLACK_CLIENT_ID`` / ``SLACK_CLIENT_SECRET``). Not a user credential.
2. **Account authorization** — the user clicks *Connect* and approves on the provider's own
   consent screen; this module drives the two-step **hosted-callback** flow (the browser is not
   on the box, so no loopback):

       POST /api/apps/{provider}/connect/start   -> { authorize_url }   (browser goes there)
       GET  /api/apps/connect/callback?code&state -> exchange -> store -> verify -> CONNECTED

3. **Account configuration** — Sidekick/Missions. The fetched user token lands in a shared
   :class:`CredentialBroker`; :meth:`HostedConnect.resolver` exposes it as the ``<provider>:token``
   refs ``go_live`` already resolves, so a Connect immediately makes the capability usable.

The orchestration (session store keyed by CSRF ``state``, dispatch, the broker→``go_live``
resolver bridge) is pure and unit-tested with a fake session factory; the real connector wiring
is the lazy :meth:`HostedConnect.from_env` path (needs the ``[connectors]`` plugin + a registered
OAuth app). Never hardcodes "provider = human" — a provider that supports API activation would be
driven by the same start/callback with no code change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

from .connect import ConnectOutcome
from .productivity import GOOGLE_DOCS_EDITOR, GOOGLE_SHEETS_EDITOR


def _dedup(scopes: Tuple[str, ...]) -> Tuple[str, ...]:
    """Union of scope URLs, first-seen order preserved."""
    seen: Dict[str, None] = {}
    for s in scopes:
        seen.setdefault(s, None)
    return tuple(seen)


#: The docs+sheets+drive.file union, taken verbatim from the W0 GOOGLE_* scope profiles so the
#: hosted Connect and the plane's scope profiles never drift apart.
_GOOGLE_DOCS_SHEETS_SCOPES: Tuple[str, ...] = _dedup(
    GOOGLE_DOCS_EDITOR.scopes + GOOGLE_SHEETS_EDITOR.scopes)

#: Microsoft Graph **delegated** scopes for the W2 workbook/drive App adapter. ``Files.ReadWrite``
#: covers the Excel workbook range + OneDrive file surfaces; ``User.Read`` lets the verify step
#: read the connected identity. NOTE the MS-vs-Google refresh-token distinction: Google receives a
#: refresh token via *authorize params* (``access_type=offline`` + ``prompt=consent``), whereas
#: Microsoft receives one from the ``offline_access`` **scope** — so it lives in ``scopes`` below,
#: not in ``authorize_params``.
_MICROSOFT_GRAPH_SCOPES: Tuple[str, ...] = ("Files.ReadWrite", "offline_access", "User.Read")


@dataclass(frozen=True)
class ProviderOAuthApp:
    """A ReDevOps-owned OAuth app for one provider — deployment config, never a user secret.

    ``authorize_params`` are extra query params a provider needs on the *authorize* URL (e.g.
    Google's ``access_type=offline`` + ``prompt=consent`` to receive a refresh token). They are
    carried here as deployment intent; whether the live consent link actually includes them
    depends on the connector's ``OAuthFlow`` supporting extra authorize params — see the note on
    :data:`KNOWN_OAUTH`.
    """

    provider: str
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    scopes: Tuple[str, ...]
    redirect_uri: str
    authorize_params: Dict[str, str] = field(default_factory=dict)


#: OAuth endpoints + the client-cred env var names for the providers ReDevOps can host. Scopes
#: are the bot/read scopes the test Missions need; extend per provider as coverage grows.
#:
#: ``google`` scopes are a *tuple of individual scope URLs* — ``OAuthFlow.authorize_url``
#: space-joins them, which is exactly the OAuth2 scope-delimiter Google wants (unlike Slack v2's
#: single comma-joined element). The default union covers docs + sheets + ``drive.file`` (the
#: least-privilege editor scope), matching the W0 ``GOOGLE_DOCS_EDITOR`` / ``GOOGLE_SHEETS_EDITOR``
#: profiles.
#:
#: ``authorize_params`` for Google (``access_type=offline`` + ``prompt=consent``) is what returns
#: a *refresh* token. These are threaded onto the live consent link via
#: ``OAuth2Config.extra_authorize_params`` (redevops-connectors #11): ``from_env`` passes
#: ``app.authorize_params`` through, so the hosted Google flow returns a durable refresh token.
KNOWN_OAUTH: Dict[str, Dict[str, Any]] = {
    "slack": {
        "authorize_url": "https://slack.com/oauth/v2/authorize",
        "token_url": "https://slack.com/api/oauth.v2.access",
        # one comma-joined element so OAuthFlow's space-join keeps Slack-v2 commas
        "scopes": ("chat:write,channels:history,users:read",),
        "env_id": "SLACK_CLIENT_ID", "env_secret": "SLACK_CLIENT_SECRET",
    },
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        # individual scope URLs — OAuthFlow space-joins them (the OAuth2 default). A sensible
        # docs+sheets+drive.file union (imported from the W0 GOOGLE_* scope profiles).
        "scopes": _GOOGLE_DOCS_SHEETS_SCOPES,
        "env_id": "GOOGLE_CLIENT_ID", "env_secret": "GOOGLE_CLIENT_SECRET",
        # needed for Google to return a refresh token (see the module note above)
        "authorize_params": {"access_type": "offline", "prompt": "consent"},
    },
    "microsoft": {
        "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        # individual Graph delegated scopes — OAuthFlow space-joins them (the OAuth2 default).
        "scopes": _MICROSOFT_GRAPH_SCOPES,
        "env_id": "MICROSOFT_CLIENT_ID", "env_secret": "MICROSOFT_CLIENT_SECRET",
        # MS-vs-Google refresh-token distinction: Google threads access_type=offline+prompt=consent
        # as authorize *params*; Microsoft returns a refresh token from the ``offline_access``
        # *scope* (already in scopes above), so authorize_params need only nudge account choice.
        "authorize_params": {"prompt": "select_account"},
    },
}


def oauth_apps_from_env(base_url: str, env: Optional[Dict[str, str]] = None) -> Dict[str, ProviderOAuthApp]:
    """Build the OAuth apps for every known provider whose client_id/secret are in the env. The
    redirect URI is this deployment's own hosted callback (``{base_url}/api/apps/connect/callback``)."""
    import os
    env = env if env is not None else dict(os.environ)
    redirect = base_url.rstrip("/") + "/api/apps/connect/callback"
    apps: Dict[str, ProviderOAuthApp] = {}
    for provider, spec in KNOWN_OAUTH.items():
        cid, secret = env.get(spec["env_id"], ""), env.get(spec["env_secret"], "")
        if cid and secret:
            apps[provider] = ProviderOAuthApp(
                provider=provider, client_id=cid, client_secret=secret,
                authorize_url=spec["authorize_url"], token_url=spec["token_url"],
                scopes=tuple(spec["scopes"]), redirect_uri=redirect,
                authorize_params=dict(spec.get("authorize_params", {})))
    return apps


# A session is any object with ``start() -> {authorize_url, state}`` and
# ``complete(code, state) -> ConnectOutcome`` (redevops_connectors-shaped HostedConnectSession).
SessionFactory = Callable[[str], Any]


@dataclass
class HostedConnect:
    """Runs the served-app Connect flow across registered OAuth apps.

    ``session_factory(provider)`` builds a fresh hosted session; inject a fake in tests, or use
    :meth:`from_env` for the real connector-backed one. ``connected`` maps a connected provider
    to the ``CredentialRef`` its fetched token was stored under.
    """

    session_factory: SessionFactory
    providers: Tuple[str, ...] = ()
    _sessions: Dict[str, Tuple[str, Any]] = field(default_factory=dict)   # state -> (provider, session)
    _connected: Dict[str, str] = field(default_factory=dict)              # provider -> credential_ref
    broker: Any = None                                                    # shared token store (for resolver)

    def available(self, provider: str) -> bool:
        return provider in self.providers

    def start(self, provider: str) -> Dict[str, str]:
        if not self.available(provider):
            raise KeyError(f"no hosted OAuth app registered for {provider!r} "
                           f"(set its client_id/secret in deployment secrets)")
        session = self.session_factory(provider)
        started = session.start()
        self._sessions[started["state"]] = (provider, session)
        return started

    def callback(self, code: str, state: str) -> ConnectOutcome:
        entry = self._sessions.pop(state, None)
        if entry is None:
            return ConnectOutcome("", "NOT_CONNECTED", False, detail="unknown or expired state")
        provider, session = entry
        outcome = session.complete(code, state)
        if outcome.connected and outcome.credential_ref:
            self._connected[provider] = outcome.credential_ref
        return outcome

    def connected_providers(self) -> Tuple[str, ...]:
        return tuple(sorted(self._connected))

    def resolver(self) -> "_BrokerResolver":
        """A SecretResolver that maps ``<provider>:token`` → the token this flow fetched, so
        ``go_live`` (which resolves those refs) uses hosted-connected credentials directly."""
        return _BrokerResolver(self.broker, dict(self._connected))

    @classmethod
    def from_env(cls, base_url: str, *, broker: Optional[Any] = None,
                 env: Optional[Dict[str, str]] = None) -> "HostedConnect":
        """The real path: lazily wire redevops-connectors (OAuthFlow + verify_setup + the adapter
        factory) into a session factory, over a shared in-memory broker."""
        try:
            from redevops_connectors import OAuth2Config, OAuthFlow, UrllibTransport, verify_setup
            from redevops_connectors.connect_flow import InMemoryCredentialBroker
        except ImportError as e:  # pragma: no cover — only without the extra
            raise ImportError("hosted OAuth needs the connector plugin: "
                              "pip install 'agentic-os[connectors]'") from e
        from .connect import HostedConnectSession
        from .live import default_adapter_factory

        apps = oauth_apps_from_env(base_url, env=env)
        shared = broker or InMemoryCredentialBroker()

        def factory(provider: str) -> HostedConnectSession:
            app = apps[provider]
            oauth = OAuth2Config(provider=provider, authorize_url=app.authorize_url,
                                 token_url=app.token_url, client_id=app.client_id,
                                 client_secret_ref=f"{provider}:client", scopes=app.scopes,
                                 redirect_uri=app.redirect_uri,
                                 extra_authorize_params=dict(app.authorize_params))
            csr = _StaticResolver({f"{provider}:client": {"client_secret": app.client_secret}})
            flow = OAuthFlow(config=oauth, resolver=csr, transport=UrllibTransport())

            def build_adapter(ref: str, _resolver: Any) -> Any:
                # Resolve the provider's live adapter against the freshly-stored token.
                mapped = _StaticResolver({f"{provider}:token": shared.resolver.resolve(ref)})
                return default_adapter_factory(mapped)[provider]()

            return HostedConnectSession(provider=provider, flow=flow, broker=shared,
                                        build_adapter=build_adapter, verify=verify_setup)

        return cls(session_factory=factory, providers=tuple(apps), broker=shared)


@dataclass
class _StaticResolver:
    material: Dict[str, Dict[str, str]]

    def resolve(self, ref: str) -> Dict[str, str]:
        if ref not in self.material:
            raise KeyError(f"no material for {ref!r}")
        return self.material[ref]


@dataclass
class _BrokerResolver:
    """Resolves ``<provider>:token`` to the broker material stored for that provider's connect."""

    broker: Any
    connected: Dict[str, str]

    def resolve(self, ref: str) -> Dict[str, str]:
        provider = ref.split(":", 1)[0]
        cref = self.connected.get(provider)
        if not cref or self.broker is None:
            raise KeyError(f"{provider} not connected via hosted OAuth")
        return dict(self.broker.resolver.resolve(cref))

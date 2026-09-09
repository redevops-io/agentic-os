"""The one-click **Connect** acceptance path — the frozen contract behind Projects' and
Sidekick's "Connect <provider>" button.

    Projects → Connect Google → provider consent → callback → CredentialRef
             → verify_setup() → CONNECTED → (read smoke) VERIFIED_READ

The user performs consent; ReDevOps performs the integration — no secret is pasted, the
token is *fetched*. This module is the reference orchestration and the acceptance test.

Like :mod:`agentic_os.integrations.execution`, agentic-os stays **decoupled** from
``redevops-connectors``: :func:`connect_provider` is pure orchestration over structural
ports (a runner that yields a credential-bearing result, an adapter builder, and a verify
callable), so it is unit-tested with fakes and never imports the connector package. The
convenience wrapper :func:`local_connect` lazily wires the real
``redevops_connectors.LoopbackConnect`` + ``verify_setup`` for the **local** deployment
profile (the agent runs on the user's machine; the browser opens locally and the redirect
lands on a 127.0.0.1 loopback). Hosted/brokered profiles swap the runner for a
hosted-callback one but reach the same :class:`ConnectOutcome`.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Protocol, Tuple


@dataclass(frozen=True)
class ConnectOutcome:
    """The result of a Connect attempt, in SetupState terms (doc's acceptance path)."""

    provider: str
    state: str            # NOT_CONNECTED | CONNECTED | VERIFIED_READ | VERIFIED_WRITE | MISSION_READY
    connected: bool
    account_ref: str = ""
    scopes: Tuple[str, ...] = ()
    credential_ref: str = ""
    detail: str = ""


# ── structural ports (duck-typed; any redevops-connectors object satisfies them) ──
class _ConnectResultPort(Protocol):
    credential_ref: str
    account_ref: str
    scopes: Tuple[str, ...]


class ConnectRunner(Protocol):
    """Performs the authorization and returns a credential-bearing result. In the local
    profile this is ``redevops_connectors.LoopbackConnect``."""

    def run(self) -> _ConnectResultPort: ...


class _SetupResultPort(Protocol):
    state: Any
    connected: bool


VerifyFn = Callable[[Any], _SetupResultPort]
AdapterBuilder = Callable[[str], Any]  # credential_ref -> adapter bound to the broker's resolver


def connect_provider(provider: str, *, runner: ConnectRunner, build_adapter: AdapterBuilder,
                     verify: VerifyFn) -> ConnectOutcome:
    """The frozen acceptance path, as pure orchestration:

    1. ``runner.run()`` — user consents; a token is fetched and stored behind a fresh
       ``CredentialRef`` (nothing pasted).
    2. build the provider adapter bound to that new ref.
    3. ``verify`` runs a read-only smoke and grades the SetupState.

    Injected end to end, so it is tested with no browser and no live provider.
    """
    result = runner.run()
    ref = getattr(result, "credential_ref", "") or ""
    if not ref:
        return ConnectOutcome(provider=provider, state="NOT_CONNECTED", connected=False,
                              detail="authorization returned no credential")
    adapter = build_adapter(ref)
    setup = verify(adapter)
    state = getattr(getattr(setup, "state", None), "value", None) or str(getattr(setup, "state", "CONNECTED"))
    return ConnectOutcome(
        provider=provider, state=state, connected=bool(getattr(setup, "connected", True)),
        account_ref=getattr(result, "account_ref", "") or "",
        scopes=tuple(getattr(result, "scopes", ()) or ()),
        credential_ref=ref, detail=getattr(setup, "detail", "") or "",
    )


def local_connect(*, provider: str, oauth_config: Any, client_secret_resolver: Any,
                  adapter_builder: Callable[[str, Any], Any], broker: Optional[Any] = None,
                  open_browser: Optional[Callable[[str], None]] = None,
                  transport: Optional[Any] = None, timeout: float = 180.0) -> ConnectOutcome:
    """LOCAL profile convenience: lazily wire ``redevops-connectors`` LoopbackConnect +
    verify_setup and run the acceptance path.

    - ``oauth_config``: a ``redevops_connectors.OAuth2Config`` for the provider (endpoints +
      client_id + the ``client_secret_ref`` the deployment resolves).
    - ``client_secret_resolver``: resolves that client-secret ref (a deployment secret;
      NOT a user credential).
    - ``broker``: stores the fetched user token; its resolver backs the adapter. Defaults to
      an in-memory broker.
    - ``adapter_builder(credential_ref, resolver)``: builds the provider adapter bound to the
      broker's resolver and a live transport.
    """
    try:
        from redevops_connectors import OAuthFlow, UrllibTransport, verify_setup
        from redevops_connectors.connect_flow import InMemoryCredentialBroker, LoopbackConnect
    except ImportError as e:  # pragma: no cover — only without the extra
        raise ImportError(
            "Connect requires the connector plugin: pip install 'agentic-os[connectors]'") from e

    import webbrowser

    broker = broker or InMemoryCredentialBroker()
    tp = transport or UrllibTransport()
    flow = OAuthFlow(config=oauth_config, resolver=client_secret_resolver, transport=tp)
    runner = LoopbackConnect(flow=flow, broker=broker, open_browser=open_browser or webbrowser.open,
                             timeout=timeout)
    resolver = getattr(broker, "resolver", None)
    return connect_provider(
        provider, runner=runner,
        build_adapter=lambda ref: adapter_builder(ref, resolver),
        verify=verify_setup,
    )


@dataclass
class _StaticRunner:
    """A ConnectRunner whose authorization already happened (hosted callback) — it just
    carries the stored credential ref forward into connect_provider's grading."""

    credential_ref: str
    account_ref: str = ""
    scopes: Tuple[str, ...] = ()

    def run(self) -> "_StaticRunner":
        return self


@dataclass
class HostedConnectSession:
    """The **served-app** Connect: two HTTP steps instead of a loopback, for the
    ``HOSTED_REDEVOPS`` / ``REDEVOPS_BROKERED`` profiles where the browser is not on the box
    running the agent.

        start()  → the provider consent URL (the user's browser goes there)
        …provider redirects to the hosted callback with ?code=&state=…
        complete(code, state) → verify_setup → ConnectOutcome

    Same ``ConnectOutcome`` as :class:`LoopbackConnect`; only the redirect differs. State is
    generated in ``start`` and checked in ``complete`` (CSRF). Everything is injected (an
    OAuthFlow-like ``flow``, a broker, an adapter builder, a verify callable), so it is tested
    with no browser and no live provider.
    """

    provider: str
    flow: Any                                   # OAuthFlow-like: authorize_url(state=), exchange_code(code)
    broker: Any                                 # CredentialBroker-like: store(provider, grant) -> ref; .resolver
    build_adapter: Callable[[str, Any], Any]    # (credential_ref, resolver) -> adapter
    verify: VerifyFn
    state_factory: Callable[[], str] = lambda: secrets.token_urlsafe(24)
    _state: str = field(default="", init=False)

    def start(self) -> Dict[str, str]:
        self._state = self.state_factory()
        return {"authorize_url": self.flow.authorize_url(state=self._state), "state": self._state}

    def complete(self, code: str, state: str) -> ConnectOutcome:
        if not code:
            return ConnectOutcome(self.provider, "NOT_CONNECTED", False, detail="no authorization code")
        if not self._state or state != self._state:
            return ConnectOutcome(self.provider, "NOT_CONNECTED", False, detail="state mismatch — possible CSRF")
        grant = self.flow.exchange_code(code)
        ref = self.broker.store(self.provider, grant)
        resolver = getattr(self.broker, "resolver", None)
        return connect_provider(
            self.provider,
            runner=_StaticRunner(ref, getattr(grant, "account_ref", "") or "",
                                 tuple(getattr(grant, "scopes", ()) or ())),
            build_adapter=lambda r: self.build_adapter(r, resolver),
            verify=self.verify,
        )

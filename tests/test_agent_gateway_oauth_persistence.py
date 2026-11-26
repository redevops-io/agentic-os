"""What a person approved survives a restart, ends when it should, and cannot be replayed: on both stores."""
import sqlite3
import threading

import pytest

from agentic_os.agent_gateway import AuthorizationServer, OAuthError, pkce_challenge
from agentic_os.agent_gateway.oauth_store import MemoryOAuthStore, SqliteOAuthStore
from agentic_os.overlays import Principal

VERIFIER = "verifier-" + "v" * 50
REDIRECT = "https://app.example/cb"


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture(params=["memory", "sqlite"])
def make(request, tmp_path):
    """A server over a store; calling it again with the sqlite store is a restart (or a second worker)."""
    memory, clock, events = MemoryOAuthStore(), Clock(), []

    def server(**kw):
        store = memory if request.param == "memory" else SqliteOAuthStore(str(tmp_path / "oauth.sqlite"))
        return AuthorizationServer(store=store, clock=clock, audit=events.append, **kw)

    server.clock, server.events, server.kind, server.path = clock, events, request.param, tmp_path / "oauth.sqlite"
    return server


def connect(srv, tenant="acme", scope=("projects.read",), connection_id="", client=None):
    client = client or srv.register_client([REDIRECT])
    code = srv.authorize(client_id=client.client_id, redirect_uri=REDIRECT, code_challenge=pkce_challenge(VERIFIER),
                         principal=Principal(f"agent:{connection_id or 'x'}", "service", (), tenant), scope=scope,
                         connection_id=connection_id, approved_by="approver:acme")
    return client, srv.exchange(code=code, code_verifier=VERIFIER, client_id=client.client_id, redirect_uri=REDIRECT)


def test_a_connection_survives_a_restart_with_the_same_identity(make):
    client, tokens = connect(make(), connection_id="conn-abc")
    again = make()                                                          # restart
    who = again.verify(tokens.access_token)
    assert who.principal.id == "agent:conn-abc" and who.tenant == "acme" and who.scopes == ("projects.read",)
    renewed = make().refresh(refresh_token=tokens.refresh_token, client_id=client.client_id)
    assert make().verify(renewed.access_token).principal.id == "agent:conn-abc"          # stable across refresh
    record = make().connections()[0]
    assert record["approved_by"] == "approver:acme" and record["tenant"] == "acme" and not record["revoked"]


def test_a_refresh_token_used_twice_ends_the_whole_connection(make):
    srv = make()
    client, first = connect(srv)
    second = srv.refresh(refresh_token=first.refresh_token, client_id=client.client_id)
    with pytest.raises(OAuthError):
        make().refresh(refresh_token=first.refresh_token, client_id=client.client_id)      # the stolen copy
    assert make().verify(second.access_token) is None                                      # the rightful one dies too
    with pytest.raises(OAuthError):
        make().refresh(refresh_token=second.refresh_token, client_id=client.client_id)
    assert "refresh_reuse_detected" in [e["event"] for e in make.events]


def test_another_client_cannot_spend_a_refresh_token_or_cause_the_revocation(make):
    srv = make()
    client, tokens = connect(srv)
    other = srv.register_client([REDIRECT])
    with pytest.raises(OAuthError):
        srv.refresh(refresh_token=tokens.refresh_token, client_id=other.client_id)
    assert srv.refresh(refresh_token=tokens.refresh_token, client_id=client.client_id)


def test_revoking_a_connection_ends_every_token_and_survives_a_restart(make):
    srv = make()
    client, tokens = connect(srv, connection_id="conn-one")
    _, other = connect(srv, connection_id="conn-two")
    assert srv.revoke_connection("conn-one", by="owner") and not srv.revoke_connection("conn-none")
    assert make().verify(tokens.access_token) is None and make().verify(other.access_token) is not None
    with pytest.raises(OAuthError):
        make().refresh(refresh_token=tokens.refresh_token, client_id=client.client_id)


def test_refreshing_never_extends_a_connection_past_its_end(make):
    srv = make(connection_ttl=3600)
    client, tokens = connect(srv)
    for _ in range(3):
        make.clock.t += 800
        tokens = srv.refresh(refresh_token=tokens.refresh_token, client_id=client.client_id)
    make.clock.t += 800
    tokens = srv.refresh(refresh_token=tokens.refresh_token, client_id=client.client_id)
    assert tokens.expires_in <= 3600 - 3200
    make.clock.t += 500                                                     # past the end
    assert srv.verify(tokens.access_token) is None
    with pytest.raises(OAuthError):
        srv.refresh(refresh_token=tokens.refresh_token, client_id=client.client_id)


def test_a_code_cannot_be_used_after_the_connection_is_revoked_and_ids_are_not_reused(make):
    srv = make()
    client = srv.register_client([REDIRECT])
    args = dict(client_id=client.client_id, redirect_uri=REDIRECT, code_challenge=pkce_challenge(VERIFIER),
                principal=Principal("agent:conn-r", "service", (), "acme"), connection_id="conn-r")
    code = srv.authorize(**args)
    with pytest.raises(OAuthError):
        srv.authorize(**args)
    srv.revoke_connection("conn-r")
    with pytest.raises(OAuthError):
        srv.exchange(code=code, code_verifier=VERIFIER, client_id=client.client_id, redirect_uri=REDIRECT)


def test_registration_is_bounded(make):
    srv = make(max_clients=2)
    srv.register_client([REDIRECT]), srv.register_client([REDIRECT])
    with pytest.raises(OAuthError):
        make(max_clients=2).register_client([REDIRECT])


def test_nothing_on_disk_can_be_presented_as_a_credential(tmp_path):
    path = tmp_path / "oauth.sqlite"
    srv = AuthorizationServer(store=SqliteOAuthStore(str(path)))
    client = srv.register_client([REDIRECT], public=False, client_secret="client-secret-value-123")
    code = srv.authorize(client_id=client.client_id, redirect_uri=REDIRECT, code_challenge=pkce_challenge(VERIFIER),
                         principal=Principal("agent:a", "service", (), "acme"))
    srv_codes = code
    tokens = srv.exchange(code=code, code_verifier=VERIFIER, client_id=client.client_id, redirect_uri=REDIRECT,
                          client_secret="client-secret-value-123")
    srv.store.close()
    blob = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith("oauth.sqlite"))
    for secret in (srv_codes, tokens.access_token, tokens.refresh_token, "client-secret-value-123", VERIFIER):
        assert secret.encode() not in blob
    assert path.stat().st_mode & 0o077 == 0


def test_two_workers_cannot_both_spend_one_refresh_token(tmp_path):
    path = str(tmp_path / "oauth.sqlite")
    client, tokens = connect(AuthorizationServer(store=SqliteOAuthStore(path)))
    workers = [AuthorizationServer(store=SqliteOAuthStore(path)) for _ in range(6)]
    won, lost, gate = [], [], threading.Barrier(len(workers))

    def spend(srv):
        gate.wait()
        try:
            won.append(srv.refresh(refresh_token=tokens.refresh_token, client_id=client.client_id))
        except OAuthError:
            lost.append(1)

    threads = [threading.Thread(target=spend, args=(w,)) for w in workers]
    [t.start() for t in threads], [t.join(30) for t in threads]
    assert len(won) <= 1 and len(won) + len(lost) == len(workers)
    assert workers[0].verify(won[0].access_token) is None if won else True      # the replays ended the connection


def test_a_store_that_cannot_be_read_admits_nobody(tmp_path):
    srv = AuthorizationServer(store=SqliteOAuthStore(str(tmp_path / "oauth.sqlite")))
    _, tokens = connect(srv)

    def broken(_digest):
        raise sqlite3.OperationalError("disk I/O error")

    srv.store.get_access = broken
    assert srv.verify(tokens.access_token) is None

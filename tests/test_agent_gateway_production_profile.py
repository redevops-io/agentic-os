"""The mounted agent gateway under AGENTIC_OS_PROFILE=production, the profile real deployments use.

It could not start there: the gateway was built without an authority store, which the profile refuses. Every other test
of the gateway ran in the development profile, so none saw it."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from agentic_os.agent_gateway import agent_tokens as AT
from agentic_os.mission.profile import ProfileError
from agentic_os.projects_api import SampleProjectionProvider, create_app

CLEAR = ("AGENT_GATEWAY_TOKEN", "AGENT_GATEWAY_AGENTS_FILE", "AGENT_GATEWAY_AUDIT_PATH", "AGENT_GATEWAY_MCP", "PROJECTS_OPERATOR_GATE",
         "OPERATOR_BASIC_AUTH", "OPERATOR_BASIC_AUTH_USER", "OPERATOR_BASIC_AUTH_PASS", "OUTREACH_BASIC_AUTH", "AGENTIC_OS_CONTROL_TOKEN",
         "AGENTIC_OS_TENANT_TOKENS", "AGENTIC_OS_APPROVER_TOKENS", "AGENTIC_OS_PRODUCTS", "AGENTIC_OS_DASHBOARDS", "PROJECTS_CORS_ORIGINS")


@pytest.fixture
def prod(monkeypatch, tmp_path):
    for k in CLEAR:
        monkeypatch.delenv(k, raising=False)
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<html></html>", encoding="utf-8")
    db, agents = str(tmp_path / "authority.sqlite"), str(tmp_path / "agents.json")
    for k, v in {"PROJECTS_UI_DIR": str(ui), "AGENTIC_OS_PROFILE": "production", "AGENTIC_OS_AUTHORITY_DB": db,
                 "AGENTIC_OS_APPROVAL_SECRET": "s" * 48, "AGENTIC_OS_REPLAY_DB": str(tmp_path / "replay.sqlite"),
                 "AGENT_GATEWAY_AGENTS_FILE": agents}.items():
        monkeypatch.setenv(k, v)
    return monkeypatch, agents, db


def client():
    return TestClient(create_app(SampleProjectionProvider()), raise_server_exceptions=False)


def call(c, token, name="projects.list", args=None):
    r = c.post(f"/api/agent-gateway/capabilities/{name}/invoke", json=args or {}, headers={"Authorization": f"Bearer {token}"})
    return r.status_code, (r.json().get("status") if r.headers.get("content-type", "").startswith("application/json") else None)


def issue(agents, db, agent="visitor", grants=("projects.read",), **kw):
    kw.setdefault("minutes", 5)
    return AT.issue(agents, agent, list(grants), authority_db=db, **kw)


def test_the_gateway_starts_and_an_agent_issued_with_authority_can_read(prod):
    _, agents, db = prod
    token = issue(agents, db)
    assert call(client(), token) == (200, "ok")


def test_a_token_without_an_authority_record_is_admitted_and_every_call_denied(prod):
    _, agents, db = prod
    token = AT.issue(agents, "no-authority", ["projects.read"], minutes=5)
    assert call(client(), token) == (200, "denied")


def test_the_record_covers_only_what_the_token_was_granted(prod):
    _, agents, db = prod
    token = issue(agents, db, grants=("projects.read",))
    doc = json.load(open(agents))
    doc["agents"][0]["grants"] = ["projects.read", "sources.read"]          # the file edited by hand to widen the agent
    json.dump(doc, open(agents, "w"))
    c = client()
    assert call(c, token) == (200, "ok")
    assert call(c, token, "sources.search", {"query": "x"}) == (200, "denied")


def test_revoking_the_authority_record_stops_a_token_the_file_still_allows(prod):
    from agentic_os.mission.authority import SqliteEnvelopeStore
    _, agents, db = prod
    token = issue(agents, db)
    c = client()
    assert call(c, token) == (200, "ok")
    SqliteEnvelopeStore(db).revoke(AT.envelope_key("visitor"))
    assert call(c, token) == (200, "denied")


def test_the_record_belongs_to_one_tenant(prod):
    _, agents, db = prod
    token = issue(agents, db, tenant="demo-tenant")
    doc = json.load(open(agents))
    doc["agents"][0]["tenant"] = "another-tenant"                          # the same token claiming another tenant
    json.dump(doc, open(agents, "w"))
    assert call(client(), token) == (200, "denied")


def test_the_record_ends_when_the_token_does_even_if_the_file_is_edited_to_outlive_it(prod):
    _, agents, db = prod
    token = issue(agents, db, minutes=1, now=time.time() - 120)            # issued two minutes ago for one minute
    doc = json.load(open(agents))
    doc["agents"][0]["expires_at"] = int(time.time()) + 3600
    json.dump(doc, open(agents, "w"))
    assert call(client(), token) == (200, "denied")


def test_the_shared_bearer_has_no_authority_in_production(prod):
    monkeypatch, agents, db = prod
    monkeypatch.setenv("AGENT_GATEWAY_TOKEN", "z" * 48)
    issue(agents, db, agent="external-agent")                              # even an agent named like the shared principal
    assert call(client(), "z" * 48) == (200, "denied")


def test_the_rate_limit_holds_in_production(prod):
    _, agents, db = prod
    token = issue(agents, db, rate_per_minute=2)
    c = client()
    assert [call(c, token)[1] for _ in range(3)] == ["ok", "ok", "denied"]


def test_production_without_an_authority_store_still_refuses_to_start(prod):
    monkeypatch, agents, db = prod
    monkeypatch.delenv("AGENTIC_OS_AUTHORITY_DB")
    with pytest.raises((ProfileError, RuntimeError)):
        create_app(SampleProjectionProvider())


def test_no_token_is_left_behind_when_its_authority_cannot_be_recorded(tmp_path):
    agents = str(tmp_path / "agents.json")
    with pytest.raises(Exception):
        AT.issue(agents, "visitor", ["projects.read"], minutes=5, authority_db=str(tmp_path / "missing" / "dir" / "a.sqlite"))
    store = AT.AgentTokenStore(agents)
    assert all(a.get("revoked") for a in json.load(open(agents))["agents"])
    assert store is not None


def test_the_command_refuses_to_issue_a_token_that_production_would_always_deny(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    monkeypatch.delenv("AGENTIC_OS_AUTHORITY_DB", raising=False)
    f = tmp_path / "agents.json"
    assert AT.main(["issue", str(f), "--id", "visitor", "--grant", "projects.read", "--minutes", "5"]) == 1
    assert not f.exists() and "--authority-db" in capsys.readouterr().err


def test_the_command_records_authority_and_revokes_both(prod, capsys):
    from agentic_os.mission.authority import SqliteEnvelopeStore
    _, agents, db = prod
    assert AT.main(["issue", agents, "--id", "visitor", "--grant", "projects.read", "--minutes", "5"]) == 0    # the store from the environment
    token = capsys.readouterr().out.strip().splitlines()[-1]
    assert call(client(), token) == (200, "ok")
    assert AT.main(["revoke", agents, "--id", "visitor"]) == 0
    assert SqliteEnvelopeStore(db).is_revoked(AT.envelope_key("visitor"))

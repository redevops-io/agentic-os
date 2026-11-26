"""An external agent gets, keeps and loses access over HTTP, with a person's consent: the mounted app, end to end."""
import base64
import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from agentic_os.agent_gateway import pkce_challenge
from agentic_os.projects_api import SampleProjectionProvider, create_app

ISSUER = "https://projects.example/projects"
VERIFIER = "verifier-" + "q" * 50
REDIRECT = "http://localhost:53682/callback"
OPERATOR = {"Authorization": "Basic " + base64.b64encode(b"operator:" + b"p" * 24).decode()}
ACME_APPROVER = "acme-approver-" + "a" * 32
GLOBEX_APPROVER = "globex-approver-" + "g" * 32
CLEAR = ("AGENT_GATEWAY_TOKEN", "AGENT_GATEWAY_AGENTS_FILE", "AGENT_GATEWAY_AUDIT_PATH", "AGENT_GATEWAY_MCP", "OPERATOR_BASIC_AUTH",
         "OUTREACH_BASIC_AUTH", "AGENTIC_OS_CONTROL_TOKEN", "AGENTIC_OS_TENANT_TOKENS", "AGENTIC_OS_APPROVER_TOKENS", "AGENTIC_OS_PRODUCTS",
         "AGENTIC_OS_DASHBOARDS", "PROJECTS_CORS_ORIGINS", "AGENTIC_OS_TENANT_LIMITS", "PROJECTS_TENANT", "AGENTIC_OS_PROFILE",
         "AGENTIC_OS_AUTHORITY_DB")


@pytest.fixture(params=["development", "production"])
def deployment(request, monkeypatch, tmp_path):
    """The signed-in deployment an agent connects to; ``make()`` again is a restart."""
    for k in CLEAR:
        monkeypatch.delenv(k, raising=False)
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<html>secret ui</html>", encoding="utf-8")
    env = {"PROJECTS_UI_DIR": str(ui), "PROJECTS_OPERATOR_GATE": "1", "OPERATOR_BASIC_AUTH": "operator:" + "p" * 24,
           "AGENTIC_OS_OAUTH_DB": str(tmp_path / "oauth.sqlite"), "AGENTIC_OS_OAUTH_ISSUER": ISSUER,
           "AGENTIC_OS_APPROVAL_SECRET": "s" * 48, "AGENTIC_OS_REPLAY_DB": str(tmp_path / "replay.sqlite")}
    if request.param == "production":
        env.update(AGENTIC_OS_PROFILE="production", AGENTIC_OS_AUTHORITY_DB=str(tmp_path / "authority.sqlite"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    def make():
        return TestClient(create_app(SampleProjectionProvider()), base_url="https://projects.example", root_path="/projects",
                          raise_server_exceptions=False, follow_redirects=False)

    make.monkeypatch = monkeypatch
    return make


def register(c, uris=(REDIRECT,), name="Test <agent>"):
    return c.post("/projects/oauth/register", json={"redirect_uris": list(uris), "client_name": name})


def consent_page(c, client_id, who=OPERATOR, scope="projects.read missions.read", redirect=REDIRECT, state="st-1"):
    return c.get("/projects/oauth/authorize", headers=who, params={
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect, "code_challenge": pkce_challenge(VERIFIER),
        "code_challenge_method": "S256", "scope": scope, "state": state})


def ticket_of(page):
    return re.search(r'name="ticket" value="([^"]+)"', page.text).group(1)


def answer(c, ticket, grants, who=OPERATOR, decision="approve", headers=None):
    return c.post("/projects/oauth/authorize", headers={**who, "Origin": "https://projects.example", **(headers or {})},
                  data={"ticket": ticket, "grant": list(grants), "decision": decision})


def exchange(c, client_id, code):
    return c.post("/projects/oauth/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": VERIFIER,
                                                  "client_id": client_id, "redirect_uri": REDIRECT})


def connect(c, who=OPERATOR, grants=("projects.read",), scope="projects.read missions.read"):
    client_id = register(c).json()["client_id"]
    back = answer(c, ticket_of(consent_page(c, client_id, who, scope)), grants, who)
    assert back.status_code == 303, back.text
    query = parse_qs(urlsplit(back.headers["location"]).query)
    assert query["state"] == ["st-1"]
    tokens = exchange(c, client_id, query["code"][0]).json()
    return client_id, tokens


def bearer(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def invoke(c, tokens, name, **arguments):
    return c.post(f"/projects/api/agent-gateway/capabilities/{name}/invoke", json=arguments, headers=bearer(tokens))


def listed(c, tokens):
    return sorted(x["name"] for x in c.get("/projects/api/agent-gateway/capabilities", headers=bearer(tokens)).json()["capabilities"])


def test_discovery_needs_no_sign_in_and_an_unauthenticated_agent_is_told_where_to_go(deployment):
    c = deployment()
    meta = c.get("/projects/.well-known/oauth-authorization-server").json()
    assert meta["issuer"] == ISSUER and meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["token_endpoint"] == ISSUER + "/oauth/token"
    assert c.get("/projects/.well-known/oauth-protected-resource").json()["authorization_servers"] == [ISSUER]
    assert c.get("/projects/.well-known/openid-configuration").json() == meta
    r = c.get("/projects/api/agent-gateway/capabilities")
    assert r.status_code == 401 and r.headers["www-authenticate"] == f'Bearer resource_metadata="{ISSUER}/.well-known/oauth-protected-resource"'
    # the rest of the app still asks a person to sign in
    assert c.get("/projects/api/dashboards").status_code == 401 and "secret ui" not in c.get("/projects/").text


def test_an_agent_connects_with_consent_reads_what_was_granted_and_survives_a_restart(deployment):
    c = deployment()
    client_id, tokens = connect(c, grants=("projects.read",))
    assert tokens["scope"] == "projects.read" and tokens["token_type"] == "Bearer"
    assert listed(c, tokens) == ["projects.get_status", "projects.list"]
    assert invoke(c, tokens, "projects.list").json()["status"] == "ok"
    assert invoke(c, tokens, "missions.list").json()["status"] == "denied"
    again = deployment()                                                                     # restart
    assert invoke(again, tokens, "projects.list").json()["status"] == "ok"
    renewed = again.post("/projects/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id}).json()
    assert invoke(again, renewed, "projects.list").json()["status"] == "ok"
    assert again.get("/projects/api/agent-gateway/capabilities", headers=bearer(tokens)).status_code == 401
    # the agent's credential opens the gateway and nothing else
    assert again.get("/projects/api/dashboards", headers=bearer(renewed)).status_code == 401
    assert again.get("/projects/oauth/connections", headers=bearer(renewed)).status_code == 401


def test_a_person_lists_and_ends_a_connection_and_it_stays_ended(deployment):
    c = deployment()
    client_id, tokens = connect(c)
    rows = c.get("/projects/oauth/connections", headers=OPERATOR).json()["connections"]
    assert len(rows) == 1 and rows[0]["approved_by"] == "operator" and rows[0]["state"] == "active" and "token" not in json.dumps(rows)
    cid = rows[0]["connection_id"]
    assert c.post(f"/projects/oauth/connections/{cid}/revoke", headers=OPERATOR, data={"x": "1"}).status_code == 415   # a form cannot
    assert c.post(f"/projects/oauth/connections/{cid}/revoke", headers=OPERATOR, json={}).status_code == 200
    again = deployment()
    assert again.get("/projects/api/agent-gateway/capabilities", headers=bearer(tokens)).status_code == 401
    r = again.post("/projects/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_a_client_revokes_only_its_own_token_and_an_ended_connection_stays_ended(deployment):
    c = deployment()
    client_id, tokens = connect(c)
    other = register(c).json()["client_id"]
    for token in (tokens["access_token"], tokens["refresh_token"]):
        assert c.post("/projects/oauth/revoke", data={"token": token, "client_id": other}).status_code == 200      # same answer
        assert c.post("/projects/oauth/revoke", data={"token": token}).status_code == 200
    assert invoke(c, tokens, "projects.list").status_code == 200
    assert c.post("/projects/oauth/revoke", data={"token": tokens["refresh_token"], "client_id": client_id}).status_code == 200
    assert c.get("/projects/api/agent-gateway/capabilities", headers=bearer(tokens)).status_code == 401
    assert c.get("/projects/oauth/connections", headers=OPERATOR).json()["connections"][0]["state"] == "revoked"


def test_dashboards_are_never_ticked_for_the_person(deployment):
    c = deployment()
    client_id = register(c).json()["client_id"]
    page = consent_page(c, client_id, scope="projects.read dashboards.read")
    assert re.search(r'value="projects.read" checked', page.text) and re.search(r'value="dashboards.read">', page.text)
    assert "Test &lt;agent&gt;" in page.text and "<agent>" not in page.text                  # the client's name is not markup
    assert page.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in page.headers["content-security-policy"]
    default = consent_page(c, client_id, scope="")
    assert "dashboards.read" not in default.text


def test_consent_cannot_be_forged_replayed_or_widened(deployment):
    c = deployment()
    client_id = register(c).json()["client_id"]
    ticket = ticket_of(consent_page(c, client_id))
    assert answer(c, ticket, ["projects.read"], headers={"Origin": "https://evil.example"}).status_code == 403
    assert answer(c, ticket[:-4] + "0000", ["projects.read"]).status_code == 403
    assert c.post("/projects/oauth/authorize", data={"ticket": ticket, "grant": "projects.read", "decision": "approve"}).status_code == 401
    back = answer(c, ticket_of(consent_page(c, client_id, scope="projects.read")), ["projects.read", "dashboards.read", "sources.read"])
    tokens = exchange(c, client_id, parse_qs(urlsplit(back.headers["location"]).query)["code"][0]).json()
    assert tokens["scope"] == "projects.read"                                                # only what was asked for AND ticked
    used = ticket_of(consent_page(c, client_id))
    assert answer(c, used, ["projects.read"]).status_code == 303 and answer(c, used, ["projects.read"]).status_code == 403


def test_refusing_or_ticking_nothing_grants_nothing(deployment):
    c = deployment()
    client_id = register(c).json()["client_id"]
    for decision, grants in (("deny", ["projects.read"]), ("approve", [])):
        back = answer(c, ticket_of(consent_page(c, client_id)), grants, decision=decision)
        assert "error=access_denied" in back.headers["location"] and "code=" not in back.headers["location"]
    assert c.get("/projects/oauth/connections", headers=OPERATOR).json()["connections"] == []


@pytest.mark.parametrize("uri", ["http://evil.example/cb", "javascript:alert(1)", "https://x.example/cb#f", "https://u:p@x.example/cb", "ftp://x/cb"])
def test_only_safe_redirect_addresses_are_registered(deployment, uri):
    assert register(deployment(), [uri]).status_code == 400


def test_nothing_redirects_to_an_address_the_client_did_not_register(deployment):
    c = deployment()
    client_id = register(c).json()["client_id"]
    r = consent_page(c, client_id, redirect="https://evil.example/cb")
    assert r.status_code == 400 and "location" not in r.headers


def test_each_approver_connects_agents_for_their_own_tenant_only(deployment):
    deployment.monkeypatch.setenv("AGENTIC_OS_APPROVER_TOKENS", f"acme={ACME_APPROVER},globex={GLOBEX_APPROVER}")
    deployment.monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({"acme": {"policy_refs": ["project:*"]}, "globex": {"policy_refs": []}}))
    c = deployment()
    acme = {"Authorization": f"Bearer {ACME_APPROVER}"}
    globex = {"Authorization": f"Bearer {GLOBEX_APPROVER}"}
    _, a = connect(c, who=acme)
    _, g = connect(c, who=globex)
    assert invoke(c, a, "projects.list").json()["output"] and invoke(c, g, "projects.list").json()["output"] == []
    rows_a = c.get("/projects/oauth/connections", headers=acme).json()["connections"]
    assert [r["tenant"] for r in rows_a] == ["acme"] and rows_a[0]["approved_by"] == "approver:acme"
    assert c.post(f"/projects/oauth/connections/{rows_a[0]['connection_id']}/revoke", headers=globex, json={}).status_code == 404
    assert invoke(c, a, "projects.list").status_code == 200
    assert len(c.get("/projects/oauth/connections", headers=OPERATOR).json()["connections"]) == 2
    # another approver cannot answer a consent page shown to this one
    client_id = register(c).json()["client_id"]
    assert answer(c, ticket_of(consent_page(c, client_id, acme)), ["projects.read"], globex).status_code == 403


def test_without_the_settings_nothing_of_it_exists(monkeypatch, tmp_path):
    for k in (*CLEAR, "AGENTIC_OS_OAUTH_DB", "AGENTIC_OS_OAUTH_ISSUER", "PROJECTS_OPERATOR_GATE"):
        monkeypatch.delenv(k, raising=False)
    c = TestClient(create_app(SampleProjectionProvider()), raise_server_exceptions=False)
    assert c.get("/.well-known/oauth-authorization-server").status_code == 404 and c.post("/oauth/register", json={}).status_code in (403, 404, 405)


@pytest.mark.parametrize("issuer", ["", "http://projects.example", "ftp://x", "https://x.example/?a=1"])
def test_an_issuer_that_is_not_https_stops_startup(deployment, issuer):
    deployment.monkeypatch.setenv("AGENTIC_OS_OAUTH_ISSUER", issuer)
    with pytest.raises(ValueError):
        deployment()

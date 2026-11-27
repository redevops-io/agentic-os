"""A person with only a browser connects an agent: follow the client's link, sign in once, read what is asked, allow it.

Through the mounted Projects app with the sign-in gate on and tenants (the approval-pages world), plus the OAuth server."""
import base64
import hashlib
import re
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

pytest.importorskip("fastapi")

from agentic_os.projects.approval_pages import safe_next  # noqa: E402
from tests import test_approval_pages as AP  # noqa: E402

CALLBACK = "http://localhost:9999/cb"
ISSUER = "http://localhost"


@pytest.fixture(params=["behind the sign-in gate", "without the gate"])
def world(monkeypatch, tmp_path, request):
    real = AP.create_app

    def create(provider):                      # the fixture below starts from a clean environment, so OAuth is set here
        monkeypatch.setenv("AGENTIC_OS_OAUTH_DB", str(tmp_path / "oauth.sqlite"))
        monkeypatch.setenv("AGENTIC_OS_OAUTH_ISSUER", ISSUER)
        if request.param == "without the gate":
            monkeypatch.delenv("PROJECTS_OPERATOR_GATE")
        return real(provider)

    monkeypatch.setattr(AP, "create_app", create)
    if request.param == "without the gate":
        # a deployment several people can reach: no declared single owner, who would be whoever sends a request
        registry = AP.write_registry
        monkeypatch.setattr(AP, "write_registry", lambda *a, **k: registry(*a, **{**k, "single_owner": ""}))
    yield from AP.world.__wrapped__(monkeypatch, tmp_path)


def client_link(c, scope="erp.read erp.propose"):
    """What an MCP client does before it sends the person's browser anywhere: register, and make a PKCE challenge."""
    made = c.post("/oauth/register", json={"redirect_uris": [CALLBACK], "client_name": "Desk assistant"})
    assert made.status_code == 201, made.text
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    query = urlencode({"client_id": made.json()["client_id"], "redirect_uri": CALLBACK, "response_type": "code", "code_challenge": challenge,
                       "code_challenge_method": "S256", "state": "st-1", "scope": scope})
    return "/oauth/authorize?" + query, made.json()["client_id"], verifier


def allow(c, page, *grants, origin=ISSUER, decision="approve"):
    ticket = re.search(r'name="ticket" value="([^"]+)"', page.text).group(1)
    data = [("ticket", ticket), ("decision", decision), *[("grant", g) for g in grants]]
    headers = {"Content-Type": "application/x-www-form-urlencoded", **({"Origin": origin} if origin else {})}
    return c.post("/oauth/authorize", content=urlencode(data), headers=headers)


def test_follow_the_clients_link_sign_in_allow_and_the_agent_works(world):
    link, client_id, verifier = client_link(world["browser"]())
    browser = world["browser"]()
    first = browser.get(link)
    assert first.status_code == 303 and first.headers["location"].startswith("/approvals/signin?next="), (first.status_code, first.text)
    form = browser.get(first.headers["location"])
    then = re.search(r'name="next" value="([^"]+)"', form.text).group(1).replace("&amp;", "&")
    assert then == link
    back = AP.sign_in(browser, AP.A_APPROVER, then)
    assert back.status_code == 303 and back.headers["location"] == link
    consent = browser.get(link)
    assert consent.status_code == 200 and "Desk assistant" in consent.text and "approver:acme" in consent.text and "acme" in consent.text, consent.text
    done = allow(browser, consent, "erp.read", "erp.propose")
    assert done.status_code == 303 and done.headers["location"].startswith(CALLBACK + "?"), done.text
    returned = parse_qs(urlsplit(done.headers["location"]).query)
    assert returned["state"] == ["st-1"]
    token = world["browser"]().post("/oauth/token", data={"grant_type": "authorization_code", "code": returned["code"][0], "code_verifier": verifier,
                                                          "client_id": client_id, "redirect_uri": CALLBACK})
    assert token.status_code == 200, token.text
    who = {"Authorization": "Bearer " + token.json()["access_token"]}
    asked = AP.invoke(world["browser"](), who, "erp.request_comment", **AP.INVOICE, text="from a connected agent", idempotency_key="comment-oauth-1")
    assert asked["status"] == "ok" and asked["output"]["state"] == "pending", asked
    # and the same browser session decides what the connected agent proposed
    card = browser.get(asked["output"]["approval_url"])
    assert card.status_code == 200 and "from a connected agent" in card.text
    assert AP.press(browser, asked["output"]["approval_url"], card, "approve").status_code == 200 and len(world["apps"].comments) == 1


def test_an_answer_resting_on_the_session_must_come_from_this_site(world):
    link, *_ = client_link(world["browser"]())
    browser = world["browser"]()
    AP.sign_in(browser, AP.A_APPROVER)
    for origin in ("https://evil.example", "", "http://localhost.evil.example"):
        page = browser.get(link)
        refused = allow(browser, page, "erp.read", origin=origin)
        assert refused.status_code == 403 and "location" not in refused.headers, (origin, refused.text)


def test_without_a_session_or_credential_nothing_is_granted(world):
    link, *_ = client_link(world["browser"]())
    stranger = world["browser"]()
    assert stranger.get(link).status_code == 303                                 # to sign-in, with nothing of the page
    assert "ticket" not in stranger.get(link).text
    signed = world["browser"]()
    AP.sign_in(signed, AP.A_APPROVER)
    page = signed.get(link)
    stolen = allow(stranger, page, "erp.read")                                    # someone else's ticket, no session
    assert stolen.status_code in (401, 403) and "location" not in stolen.headers
    other = world["browser"]()
    AP.sign_in(other, AP.B_APPROVER)                                              # another tenant's approver, same ticket
    assert allow(other, page, "erp.read").status_code == 403


def test_a_requester_whose_tenant_has_an_approver_cannot_consent_by_session(world):
    link, *_ = client_link(world["browser"]())
    requester = world["browser"]()
    AP.sign_in(requester, AP.A_REQUESTER)
    out = requester.get(link)
    assert out.status_code == 401 and "ticket" not in out.text, (out.status_code, out.text)


def test_a_request_with_its_own_credential_is_judged_by_it_not_by_the_cookie(world):
    link, *_ = client_link(world["browser"]())
    browser = world["browser"]()
    AP.sign_in(browser, AP.A_APPROVER)
    out = browser.get(link, headers={"Authorization": "Bearer " + "z" * 40})
    assert out.status_code == 401 and "ticket" not in out.text


@pytest.mark.parametrize("value", ["https://evil.example/oauth/authorize?x=1", "//evil.example/oauth/authorize?x=1", "/oauth/token?x=1",
                                   "/oauth/authorize", "/oauth/authorize?x=1\r\nSet-Cookie: a=b", "/oauth/authorize?x=<script>",
                                   "/oauth/authorizeevil?x=1", "/oauth/authorize?" + "a" * 2001, "/oauth/authorize/../x?y=1"])
def test_sign_in_leads_only_to_these_pages_or_the_consent_page(value):
    assert safe_next(value) == "/approvals", value

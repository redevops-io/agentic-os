"""A person signs in, follows the link an agent gave them, sees exactly what is proposed and decides it. Through the
mounted Projects app, production profile, sign-in gate on, with tenants; governed writes and both integrated products."""
import base64
import html
import json
import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agentic_os.agent_gateway import agent_tokens as AT  # noqa: E402
from agentic_os.governed_writes import apps as apps_mod  # noqa: E402
from agentic_os.projects.approval_pages import safe_next  # noqa: E402
from agentic_os.projects.approver_session import COOKIE, IDLE_S, TTL_S, Sessions  # noqa: E402
from agentic_os.projects_api import SampleProjectionProvider, create_app  # noqa: E402
from tests._products_fakes import FakeQa, FakeSentinel, write_registry  # noqa: E402
from tests._writes_fakes import FakeApps  # noqa: E402
from tests.test_governed_writes import AMBIENT  # noqa: E402

A_APPROVER, B_APPROVER, A_REQUESTER = "A" * 40, "B" * 40, "R" * 40
OPERATOR = {"Authorization": "Basic " + base64.b64encode(b"operator:" + b"p" * 24).decode()}
LIMITS = {"acme": {"policy_refs": ["app:erpnext", "app:chatwoot", "writes:decide", "projects:write", "products:edge-sentinel", "products:redevops-qa"]},
          "globex": {"policy_refs": ["writes:decide", "projects:write"]}}
INVOICE = {"doctype": "Sales Invoice", "name": "SINV-1"}
HOSTILE = "</td><script>alert(1)</script> please check"


@pytest.fixture
def world(monkeypatch, tmp_path):
    for k in (*AMBIENT, "AGENT_GATEWAY_TOKEN", "AGENT_GATEWAY_MCP", "AGENTIC_OS_OAUTH_DB", "AGENTIC_OS_TENANT_TOKENS", "PROJECTS_PUBLIC_URL",
              "PROJECTS_CORS_ORIGINS", "AGENTIC_OS_APPROVER_SIGNIN_OVER_HTTP", "ERPNEXT_PUBLIC_URL", "CHATWOOT_PUBLIC_URL"):
        monkeypatch.delenv(k, raising=False)
    fake = FakeApps()
    monkeypatch.setattr(apps_mod, "writers_from_env", lambda: fake.writers())
    sentinel = FakeSentinel(dry_run=False, linked={"projects:approver:acme": "alex", "projects:operator": "alex"})
    qa = FakeQa(plans=True)
    sentinel_url, qa_url = sentinel.serve(), qa.serve()
    extra = f"""
  - id: redevops-qa
    display_name: redevops-qa
    kind: quality
    api_base: {qa_url}
    ui_url: {qa_url}/ui
    health: {{path: /readyz, interval_s: 1, timeout_s: 2}}
    contract: {{api: /v1, info: /v1/info, min_version: "0.1"}}
    auth:
      service_token_file: {tmp_path / 'secrets' / 'service'}
      decision_token_file: {tmp_path / 'secrets' / 'decision'}
      approval_secret_file: {tmp_path / 'secrets' / 'secret'}
      scheme: bearer
"""
    registry = write_registry(tmp_path, sentinel_url, extra_products=extra)
    agents, db = str(tmp_path / "agents.json"), str(tmp_path / "authority.sqlite")
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<html>secret ui</html>")
    for k, v in {"AGENTIC_OS_PRODUCTS": registry, "AGENT_GATEWAY_AGENTS_FILE": agents, "AGENTIC_OS_PROFILE": "production",
                 "AGENTIC_OS_AUTHORITY_DB": db, "AGENTIC_OS_APPROVAL_SECRET": "s" * 48, "AGENTIC_OS_REPLAY_DB": str(tmp_path / "replay.sqlite"),
                 "AGENTIC_OS_TENANT_LIMITS": json.dumps(LIMITS), "AGENTIC_OS_WRITES_DIR": str(tmp_path / "writes"),
                 "AGENTIC_OS_TENANT_TOKENS": f"acme={A_REQUESTER}", "AGENTIC_OS_APPROVER_TOKENS": f"acme={A_APPROVER},globex={B_APPROVER}",
                 "OPERATOR_BASIC_AUTH": "operator:" + "p" * 24, "PROJECTS_UI_DIR": str(ui), "PROJECTS_OPERATOR_GATE": "1",
                 "ERPNEXT_PUBLIC_URL": "https://erp.example"}.items():
        monkeypatch.setenv(k, v)
    app = create_app(SampleProjectionProvider())

    def browser(base="http://localhost"):
        return TestClient(app, base_url=base, follow_redirects=False, raise_server_exceptions=False)

    def agent(grants, tenant="acme", label="Back office agent"):
        n = len(json.loads(open(agents).read())["agents"]) if __import__("os").path.exists(agents) else 0
        return {"Authorization": "Bearer " + AT.issue(agents, f"agent-{n + 1}", grants, minutes=10, tenant=tenant, authority_db=db, label=label)}

    yield {"browser": browser, "agent": agent, "apps": fake, "sentinel": sentinel, "qa": qa, "env": monkeypatch, "app": app}
    sentinel.close(), qa.close()


def invoke(c, who, tool, /, **args):
    return c.post(f"/api/agent-gateway/capabilities/{tool}/invoke", headers=who, json=args).json()


def sign_in(c, token=None, next_path="/approvals", **form):
    data = {"next": next_path, **({"token": token} if token else {}), **form}
    return c.post("/approvals/signin", data=data, headers={"Origin": "http://localhost"})


def field(page, name):
    return re.search(rf'name="{name}" value="([^"]+)"', page.text).group(1)


def press(c, path, page, decision, **override):
    data = {"csrf": field(page, "csrf"), "digest": field(page, "digest"), "decision": decision, **override}
    return c.post(path + "/decision", data=data, headers={"Origin": "http://localhost"})


def propose(w, c, text="please check", **kw):
    out = invoke(c, kw.pop("who", None) or w["agent"](["erp.read", "erp.propose"]), "erp.request_comment", **INVOICE, text=text,
                 idempotency_key=kw.pop("key", "comment-0001"))
    assert out["status"] == "ok", out
    return out["output"]


def test_follow_the_agents_link_sign_in_review_and_approve(world):
    c = world["browser"]()
    asked = propose(world, c, HOSTILE)
    path = asked["approval_url"]
    assert path == f"/approvals/writes/{asked['request_id']}"                    # no public address configured: the path, and no credential in it
    first = c.get(path)
    assert first.status_code == 303 and first.headers["location"] == f"/approvals/signin?next={path}"
    page = c.get(first.headers["location"])
    assert page.status_code == 200 and f'value="{path}"' in page.text and page.headers["x-frame-options"] == "DENY"
    assert "default-src 'none'" in page.headers["content-security-policy"] and "<script" not in page.text
    back = sign_in(c, A_APPROVER, path)
    cookie = back.headers["set-cookie"].lower()
    assert back.status_code == 303 and back.headers["location"] == path and "httponly" in cookie and "samesite=lax" in cookie
    assert A_APPROVER.lower() not in cookie
    card = c.get(path)
    text = html.unescape(card.text)
    for needle in ("Waiting for a decision", "ERPNext", "Tenant</th><td>acme", "agent:agent-1", "Back office agent", "Approval expires",
                   "Sales Invoice", "SINV-1", "Affected records", "Add a comment to Sales Invoice SINV-1", "no approval step"):
        assert needle in card.text or needle in text, needle
    assert HOSTILE in text and "<script>alert" not in card.text                   # shown exactly, never as markup
    assert 'href="https://erp.example/app/sales-invoice/SINV-1"' in card.text    # the record in the application's own UI
    assert re.search(r'value="approve">Approve', card.text) and 'value="reject">Reject' in card.text and not world["apps"].posts
    done = press(c, path, card, "approve")
    assert done.status_code == 200 and "Done and verified" in done.text and "approver:acme" in done.text and "Outcome" in done.text, done.text
    assert "please check" in html.unescape(done.text) and "Decided by" in done.text and 'name="decision"' not in done.text
    assert len(world["apps"].comments) == 1
    again = press(c, path, card, "approve")
    assert again.status_code == 409 and len(world["apps"].comments) == 1
    assert "Done and verified" in c.get("/approvals").text


def test_a_session_opens_the_approval_pages_and_nothing_else(world):
    c = world["browser"]()
    sign_in(c, A_APPROVER)
    assert c.get("/approvals").status_code == 200
    for path in ("/api/dashboards", "/api/projects", "/api/writes", "/", "/api/agent-gateway/capabilities", "/approvals/../api/dashboards"):
        r = c.get(path)
        assert r.status_code in (401, 404) and "secret ui" not in r.text, (path, r.status_code, r.text[:100])


def test_a_decision_needs_the_session_its_csrf_token_and_the_same_origin(world):
    c = world["browser"]()
    who = world["agent"](["erp.propose"])
    path = propose(world, c, who=who)["approval_url"]
    sign_in(c, A_APPROVER)
    card = c.get(path)
    assert press(c, path, card, "approve", csrf="forged").status_code == 403
    cross = c.post(path + "/decision", data={"csrf": field(card, "csrf"), "digest": field(card, "digest"), "decision": "approve"},
                   headers={"Origin": "https://evil.example"})
    assert cross.status_code == 403
    stranger = world["browser"]()
    anonymous = stranger.post(path + "/decision", data={"csrf": field(card, "csrf"), "digest": field(card, "digest"), "decision": "approve"})
    as_agent = stranger.post(path + "/decision", headers=who, data={"csrf": "x", "digest": field(card, "digest"), "decision": "approve"})
    assert anonymous.status_code in (401, 403) and as_agent.status_code in (401, 403)
    assert press(c, path, card, "approve", digest="0" * 64).status_code == 409
    assert press(c, path, card, "execute").status_code == 403
    assert not world["apps"].posts
    assert press(c, path, card, "reject").status_code == 200 and not world["apps"].posts


def test_another_tenant_sees_nothing_and_a_requester_sees_but_cannot_decide(world):
    c = world["browser"]()
    path = propose(world, c)["approval_url"]
    globex = world["browser"]()
    sign_in(globex, B_APPROVER)
    assert globex.get(path).status_code == 404 and "SINV-1" not in globex.get("/approvals").text
    requester = world["browser"]()
    sign_in(requester, A_REQUESTER)                                               # acme has an approver: separation of duties
    card = requester.get(path)
    assert card.status_code == 200 and "SINV-1" in card.text and 'name="decision"' not in card.text and "separation of duties" in card.text
    assert "please check" not in card.text and "sign in as an approver" in card.text           # as the API: no exact values
    forced = requester.post(path + "/decision", data={"csrf": field(card, "csrf"), "digest": "0" * 64, "decision": "approve"},
                            headers={"Origin": "http://localhost"})
    assert forced.status_code == 403 and not world["apps"].posts


def test_what_the_api_requires_of_an_approver_the_page_requires_too(world):
    c = world["browser"]()
    path = propose(world, c)["approval_url"]
    sign_in(c, A_APPROVER)
    card = c.get(path)
    world["env"].setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({"acme": {"policy_refs": ["app:erpnext"]}}))
    page = c.get(path)
    assert 'name="decision"' not in page.text and "not granted writes:decide" in page.text
    assert press(c, path, card, "approve").status_code == 403 and not world["apps"].posts


def test_a_denied_operation_can_only_be_rejected(world):
    c = world["browser"]()
    path = propose(world, c)["approval_url"]
    sign_in(c, A_APPROVER)
    card = c.get(path)
    world["env"].setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({"acme": {"policy_refs": ["writes:decide"]}}))
    page = c.get(path)
    assert "It cannot be approved" in page.text and "tenant_not_granted" not in page.text.split("It cannot be approved")[0]
    assert 'value="approve"' not in page.text and 'value="reject"' in page.text
    assert press(c, path, card, "approve").status_code == 403 and not world["apps"].posts
    assert press(c, path, c.get(path), "reject", digest=field(card, "digest")).status_code == 200


def test_a_removed_or_replaced_credential_ends_its_sessions_at_once(world):
    c = world["browser"]()
    sign_in(c, A_APPROVER)
    assert c.get("/approvals").status_code == 200
    world["env"].setenv("AGENTIC_OS_APPROVER_TOKENS", "acme=" + "N" * 40)
    r = c.get("/approvals")
    assert r.status_code == 303 and "signin" in r.headers["location"]


def test_sign_in_refuses_wrong_cross_site_cleartext_and_repeated_attempts(world):
    c = world["browser"]()
    assert sign_in(c, "x" * 40).status_code == 401 and COOKIE not in c.cookies
    assert c.post("/approvals/signin", data={"token": A_APPROVER}, headers={"Origin": "https://evil.example"}).status_code == 403
    lan = world["browser"]("http://192.0.2.10")
    refused = lan.post("/approvals/signin", data={"token": A_APPROVER}, headers={"Origin": "http://192.0.2.10"})
    assert refused.status_code == 403 and "HTTPS" in refused.text and "set-cookie" not in refused.headers
    # a public HTTPS address does not make a plain-HTTP LAN address acceptable, only its own hostname behind the proxy
    world["env"].setenv("PROJECTS_PUBLIC_URL", "https://projects.example")
    from agentic_os.projects.approval_pages import ApprovalPages
    pages = ApprovalPages(None, public_base="https://projects.example")

    class _At:
        def __init__(self, scheme, host):
            self.url = type("U", (), {"scheme": scheme, "hostname": host})()
    assert pages.may_receive_a_credential(_At("http", "projects.example")) and pages.may_receive_a_credential(_At("https", "other.example"))
    assert not pages.may_receive_a_credential(_At("http", "192.0.2.10")) and pages.may_receive_a_credential(_At("http", "localhost"))
    secure = world["browser"]("https://projects.example")
    ok = secure.post("/approvals/signin", data={"token": A_APPROVER}, headers={"Origin": "https://projects.example"})
    assert ok.status_code == 303 and "secure" in ok.headers["set-cookie"].lower()
    operator = world["browser"]()
    assert sign_in(operator, username="operator", password="p" * 24).status_code == 303
    for _ in range(9):
        last = sign_in(c, "y" * 40)
    assert last.status_code == 429 and sign_in(c, A_APPROVER).status_code == 429       # locked out, even with the right one


def test_other_peoples_failures_never_lock_an_approver_out(world):
    from agentic_os.projects.approver_session import FAILURES_IN_ALL, FLOOD_DELAY_S
    now = [1000.0]
    s = Sessions(clock=lambda: now[0])
    assert s.failure_delay() == 0.0
    for n in range(FAILURES_IN_ALL * 10):                                       # a flood from many (or forged) addresses
        s.sign_in(f"attacker-{n}", token="z" * 40)
    assert s.failure_delay() == FLOOD_DELAY_S and len(s._all_failures) <= FAILURES_IN_ALL * 4
    assert not s.blocked("the-approver") and s.sign_in("the-approver", token=A_APPROVER)
    now[0] += 301
    assert s.failure_delay() == 0.0


def test_signing_in_never_redirects_elsewhere(world):
    for bad in ("https://evil.example/x", "//evil.example", "/approvals/../../etc", "/api/dashboards", "/approvals/x\r\nSet-Cookie: a=1", ""):
        assert safe_next(bad) == "/approvals", bad
    assert safe_next("/approvals/writes/mi_ab-1") == "/approvals/writes/mi_ab-1"
    c = world["browser"]()
    assert sign_in(c, A_APPROVER, "https://evil.example/").headers["location"] == "/approvals"


def test_sessions_lapse(world):
    now = [1000.0]
    s = Sessions(clock=lambda: now[0])
    value = s.sign_in("client", token=A_APPROVER)
    assert s.get(value).principal == "approver:acme"
    now[0] += IDLE_S + 1
    assert s.get(value) is None
    value = s.sign_in("client", token=A_APPROVER)
    for _ in range(int(TTL_S // 1000) + 1):
        now[0] += 1000
        s.get(value)
    assert s.get(value) is None


def test_sentinel_approve_then_execute_on_the_same_page_with_the_agent_named(world):
    c = world["browser"]()
    who = world["agent"](["security.read", "security.propose"], label="SOC agent")
    out = invoke(c, who, "security.sentinel_propose_ban", ip="198.51.100.7", reason="scanning", duration_hours=4, idempotency_key="ban-000000001")
    assert out["status"] == "ok", out
    path = out["output"]["approval_url"]
    assert path == f"/approvals/edge-sentinel/{out['output']['request_id']}"
    sign_in(c, A_APPROVER)
    card = c.get(path)
    assert "198.51.100.7" in card.text and "agent:agent-1" in card.text and "SOC agent" in card.text and "Tenant</th><td>acme" in card.text
    assert "Open in " in card.text and 'value="approve"' in card.text and "sets no expiry" in card.text and world["sentinel"].executions == 0
    approved = press(c, path, card, "approve")
    assert approved.status_code == 200 and 'value="execute"' in approved.text and world["sentinel"].executions == 0, approved.text
    done = press(c, path, approved, "execute")
    assert done.status_code == 200 and world["sentinel"].executions == 1 and "Outcome" in done.text and "Read-back" in done.text, done.text
    assert world["sentinel"].actions[out["output"]["request_id"]]["decision"]["actor"] == "user:alex"


def test_a_qa_run_waiting_for_approval_is_decided_here_too(world):
    c = world["browser"]()
    who = world["agent"](["quality.read", "quality.run"], label="QA agent")
    from tests._products_fakes import EST, TGT
    out = invoke(c, who, "quality.qa_start_run", target_id=TGT, profile="edge-sentinel-fixture-readonly", estimate_digest=EST,
                 idempotency_key="run-000000001")
    assert out["status"] == "ok" and out["output"].get("approval_url"), out
    sign_in(c, A_APPROVER)
    card = c.get(out["output"]["approval_url"])
    assert card.status_code == 200 and "QA agent" in card.text and 'value="approve"' in card.text and "Test target" in card.text, card.text
    done = press(c, out["output"]["approval_url"], card, "reject")
    run = world["qa"].runs[out["output"]["run_id"]]
    assert done.status_code == 200 and run["state"] == "rejected" and "Rejected" in done.text, (done.status_code, run, done.text)


def test_only_the_agent_that_created_a_record_is_named_as_its_requester(tmp_path):
    from agentic_os.agent_gateway import product_capabilities as pc
    from agentic_os.products.schemas import TOOL_CLASS
    assert pc.CREATING_TOOLS <= {t for t, c in TOOL_CLASS.items() if c in ("stage", "bounded_write")}
    assert "quality.qa_cancel_run" not in pc.CREATING_TOOLS
    from agentic_os.projects.approval_center import Proposals
    p = Proposals(tmp_path / "p.sqlite")
    p.record("qa", "run-1", "agent:first", "acme")
    p.record("qa", "run-1", "agent:second", "globex")                                        # the first proposer stands
    assert p.get("qa", "run-1") == ("agent:first", "acme") and p.get("qa", "run-2") is None


def test_products_are_shown_and_decided_only_for_tenants_granted_them(world):
    c = world["browser"]()
    who = world["agent"](["security.read", "security.propose"])
    out = invoke(c, who, "security.sentinel_propose_ban", ip="198.51.100.7", reason="scanning", idempotency_key="ban-000000002")["output"]
    globex = world["browser"]()
    sign_in(globex, B_APPROVER)
    assert globex.get(out["approval_url"]).status_code == 404 and "198.51.100.7" not in globex.get("/approvals").text
    # nor through the product's decision route, although globex's approver holds projects:write
    digest = world["sentinel"].actions[out["request_id"]]["digest"]
    r = c.post(f"/api/products/edge-sentinel/actions/{out['request_id']}/decision", headers={"Authorization": f"Bearer {B_APPROVER}"},
               json={"approved": True, "digest": digest})
    assert r.status_code == 403 and "tenant_not_granted" in r.text, r.text
    assert world["sentinel"].actions[out["request_id"]]["state"] == "AWAITING_APPROVAL"

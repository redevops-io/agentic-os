"""Adding an approver must not take anything from a requester except deciding for its own tenant."""
import base64

import pytest

import fastapi  # noqa: F401,PLC0415 - required on this line: its absence is a failure, not a skip

from agentic_os.mission.control_auth import separation_applies  # noqa: E402
from agentic_os.products.principal import PrincipalMiddleware, current_principal, may_decide  # noqa: E402

REQ_A, REQ_B, APPR_A = "r" * 40, "s" * 40, "a" * 40


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("AGENTIC_OS_CONTROL_TOKEN", "OPERATOR_BASIC_AUTH", "AGENTIC_OS_APPROVER_TOKENS", "AGENTIC_OS_TENANT_LIMITS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", f"acme={REQ_A},globex={REQ_B}")


def who(token, single_owner=""):
    """The principal of a request carrying this bearer, as a route behind the middleware sees it."""
    import asyncio
    seen = {}

    async def app(scope, receive, send):
        seen["who"] = current_principal(single_owner)
    asyncio.run(PrincipalMiddleware(app)({"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]}, None, None))
    return seen["who"]


def test_without_approvers_requesters_are_identified_and_decide():
    assert who(REQ_A) == "requester:acme" and may_decide(who(REQ_A))
    assert who(REQ_B) == "requester:globex" and may_decide(who(REQ_B))


def test_adding_an_approver_keeps_every_requester_identified_and_changes_only_its_own_tenants_decisions(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_APPROVER_TOKENS", f"acme={APPR_A}")
    assert who(REQ_A) == "requester:acme" and who(REQ_B) == "requester:globex" and who(APPR_A) == "approver:acme"
    assert not may_decide("requester:acme") and may_decide("requester:globex") and may_decide("approver:acme")
    assert separation_applies("acme") and not separation_applies("globex")


def test_a_requester_never_falls_back_to_the_single_owner(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_APPROVER_TOKENS", f"acme={APPR_A}")
    assert who(REQ_A, single_owner="owner") == "requester:acme"
    assert who("not-a-token", single_owner="owner") == "owner" or who("not-a-token", single_owner="owner") is None


def test_an_expired_approver_still_separates_and_an_unreadable_table_fails_closed(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_APPROVER_TOKENS", f"acme={APPR_A}")
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", '{"acme": {"expires_at": 1}}')
    assert separation_applies("acme")
    monkeypatch.setenv("AGENTIC_OS_APPROVER_TOKENS", f"acme={APPR_A},globex={APPR_A}")      # colliding tokens
    assert separation_applies("globex") and not may_decide("requester:globex")
    assert not may_decide(None) and not may_decide("")


def test_through_projects_a_requester_keeps_reading_its_dashboards_and_cannot_decide(monkeypatch, tmp_path):
    import json

    from fastapi.testclient import TestClient

    from agentic_os.projects_api import SampleProjectionProvider, create_app
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<html></html>")
    for k, v in {"PROJECTS_UI_DIR": str(ui), "PROJECTS_OPERATOR_GATE": "1", "AGENTIC_OS_PROFILE": "production",
                 "AGENTIC_OS_APPROVAL_SECRET": "x" * 48, "AGENTIC_OS_REPLAY_DB": str(tmp_path / "replay.db"),
                 "AGENTIC_OS_AUTHORITY_DB": str(tmp_path / "auth.db"),
                 "AGENTIC_OS_TENANT_LIMITS": json.dumps({"acme": {"policy_refs": ["dashboards:*", "project:*", "projects:write"]},
                                                          "globex": {"policy_refs": ["dashboards:*", "project:*", "projects:write"]}})}.items():
        monkeypatch.setenv(k, v)
    client = TestClient(create_app(SampleProjectionProvider()), raise_server_exceptions=False)
    H = lambda t: {"Authorization": f"Bearer {t}"}  # noqa: E731
    before = client.get("/api/dashboards", headers=H(REQ_A))
    assert before.status_code == 200
    monkeypatch.setenv("AGENTIC_OS_APPROVER_TOKENS", f"acme={APPR_A}")
    after = client.get("/api/dashboards", headers=H(REQ_A))
    assert after.status_code == 200
    ids = lambda r: sorted(d["id"] for d in r.json()["dashboards"])  # noqa: E731
    assert ids(after) == ids(before)                                   # the same access as before the approver existed
    # deciding is what separation of duties takes away, from this tenant only
    pid = client.get("/api/projects", headers=H(REQ_A)).json()
    pid = (pid.get("projects") if isinstance(pid, dict) else pid)[0]["id"]
    approve = lambda t: client.post(f"/api/projects/{pid}/today/item-1/approve", headers=H(t), json={"payload_digest": "0" * 64})  # noqa: E731
    denied = approve(REQ_A)
    assert denied.status_code == 403 and "separation of duties" in denied.text, (denied.status_code, denied.text)
    assert "separation of duties" not in approve(REQ_B).text
    # what is not a decision stays: reversible batch actions
    batch = client.post(f"/api/projects/{pid}/today/batch", headers=H(REQ_A), json={"action": "dismiss", "item_ids": ["item-1"]})
    assert "separation of duties" not in batch.text, batch.text

"""Live: the approval-gated enrichment handoff against a real Twenty. Opt-in — set TWENTY_LIVE_TEST_URL and
TWENTY_LIVE_TEST_KEY to a disposable workspace (never a production CRM: this test creates records)."""
from __future__ import annotations

import os
import uuid

import httpx
import pytest

from agentic_os.integrations.twenty_operator import build_twenty_operator
from agentic_os.integrations.twenty_write import TwentyWriteClient

URL, KEY = os.environ.get("TWENTY_LIVE_TEST_URL", "").rstrip("/"), os.environ.get("TWENTY_LIVE_TEST_KEY", "")
pytestmark = pytest.mark.skipif(not (URL and KEY), reason="TWENTY_LIVE_TEST_URL/KEY not set (opt-in live test)")
H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
PROV = [{"provider": "hunter", "operation": "find_email", "retrieved_at": "2026-11-23T10:00:00Z",
         "request_digest": "a" * 64, "response_digest": "b" * 64, "ledger_entry": 1}]


def _companies(host):
    r = httpx.get(f"{URL}/rest/companies", params={"filter": f"domainName.primaryLinkUrl[ilike]:%{host}%"},
                  headers=H, timeout=20)
    assert r.status_code == 200, r.text
    return r.json()["data"]["companies"]


def _people(email):
    r = httpx.get(f"{URL}/rest/people", params={"filter": f"emails.primaryEmail[eq]:{email}"}, headers=H, timeout=20)
    assert r.status_code == 200, r.text
    return r.json()["data"]["people"]


def _input(host):
    return {"company": {"name": f"Live {host}", "domain": f"https://www.{host}/"},
            "contact": {"name": "Dana Rivera", "email": f"dana@{host}", "verified": True}, "provenance": PROV}


def test_unapproved_and_shadow_write_nothing_then_approval_writes_once():
    host = f"live-{uuid.uuid4().hex[:10]}.example.test"
    live = build_twenty_operator(client=TwentyWriteClient(base_url=URL, api_key=KEY, shadow=False))
    shadow = build_twenty_operator(client=TwentyWriteClient(base_url=URL, api_key=KEY, shadow=True))

    staged = live.invoke("crm.enrichment.handoff", _input(host))
    assert staged["status"] == "pending_approval" and staged["executed"] is False
    assert _companies(host) == [] and _people(f"dana@{host}") == []

    sh = shadow.invoke("crm.enrichment.handoff", {**_input(host), "_approval": {"approved": True}})
    assert sh["status"] == "done" and sh["shadow"] is True
    assert _companies(host) == [] and _people(f"dana@{host}") == []

    done = live.invoke("crm.enrichment.handoff", {**_input(host), "_approval": {"approved": True}})
    assert done["status"] == "done" and done["shadow"] is False, done
    companies, people = _companies(host), _people(f"dana@{host}")
    assert len(companies) == 1 and len(people) == 1
    assert people[0]["companyId"] == companies[0]["id"] == done["company_id"]

    again = live.invoke("crm.enrichment.handoff", {**_input(host), "_approval": {"approved": True}})
    assert again["status"] == "done" and again["company"]["created"] is False and again["person"]["created"] is False
    assert len(_companies(host)) == 1 and len(_people(f"dana@{host}")) == 1


@pytest.mark.parametrize("stored", ["https://www.{h}/", "http://{h}", "{h}"])
def test_company_entered_another_way_in_the_real_crm_is_matched(stored):
    host = f"dup-{uuid.uuid4().hex[:10]}.example.test"
    r = httpx.post(f"{URL}/rest/companies", headers=H, timeout=20,
                   json={"name": "Entered by hand", "domainName": {"primaryLinkUrl": stored.format(h=host)}})
    assert r.status_code < 300, r.text
    client = TwentyWriteClient(base_url=URL, api_key=KEY, shadow=False)
    res = client.upsert_company(name="From enrichment", domain=host.upper())
    assert res.get("created") is False and res["company"]["name"] == "Entered by hand", res
    assert len(_companies(host)) == 1


def test_refused_handoff_writes_nothing_in_the_real_crm():
    host = f"ref-{uuid.uuid4().hex[:10]}.example.test"
    live = build_twenty_operator(client=TwentyWriteClient(base_url=URL, api_key=KEY, shadow=False))
    inp = {**_input(host), "provenance": [], "_approval": {"approved": True}}
    res = live.invoke("crm.enrichment.handoff", inp)
    assert res["status"] == "error" and "no provenance" in res["error"]
    assert _companies(host) == []

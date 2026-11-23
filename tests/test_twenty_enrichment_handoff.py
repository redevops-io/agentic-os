"""The approval-gated enrichment -> Twenty handoff, and domain-normalised company matching."""
from __future__ import annotations

import pytest

from agentic_os.integrations import twenty_write as tw
from agentic_os.integrations.twenty_operator import build_twenty_operator, core_enrichment_handoff
from agentic_os.integrations.twenty_write import TwentyWriteClient, normalize_domain

PROV = [{"provider": "hunter", "operation": "find_email", "retrieved_at": "2026-11-23T10:00:00Z",
         "request_digest": "a" * 64, "response_digest": "b" * 64, "ledger_entry": 1}]
OK = {"company": {"name": "Acme GmbH", "domain": "https://www.acme.com/"},
      "contact": {"name": "Dana Rivera", "email": "Dana@acme.com", "verified": True}, "provenance": PROV}


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeTwenty:
    """Minimal in-memory stand-in for Twenty's REST API, driven through httpx.get/post."""

    def __init__(self, companies=(), lookup_status=200):
        self.companies, self.people, self.posts, self.lookup_status = list(companies), [], [], lookup_status

    def get(self, url, params=None, headers=None, timeout=None):
        if url.endswith("/rest/companies"):
            if self.lookup_status >= 400:
                return Resp(self.lookup_status, {})
            needle = params["filter"].split(":%", 1)[1].rstrip("%")
            return Resp(200, {"data": {"companies": [c for c in self.companies
                                                     if needle in c["domainName"]["primaryLinkUrl"].lower()]}})
        email = params["filter"].split(":", 1)[1]
        return Resp(200, {"data": {"people": [p for p in self.people if p["emails"]["primaryEmail"] == email]}})

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append((url.rsplit("/", 1)[1], json))
        rec = {"id": f"id-{len(self.posts)}", **json}
        if url.endswith("/rest/companies"):
            self.companies.append(rec)
            return Resp(201, {"data": {"createCompany": rec}})
        self.people.append(rec)
        return Resp(201, {"data": {"createPerson": rec}})


@pytest.fixture
def twenty(monkeypatch):
    def _make(**kw):
        fake = FakeTwenty(**kw)
        monkeypatch.setattr(tw.httpx, "get", fake.get)
        monkeypatch.setattr(tw.httpx, "post", fake.post)
        return fake, TwentyWriteClient(base_url="http://twenty.example", api_key="k", shadow=False)
    return _make


@pytest.mark.parametrize("raw,want", [("https://www.Acme.com/about", "acme.com"), ("acme.com", "acme.com"),
                                      ("http://acme.com:8080", "acme.com"), ("not a domain", ""), ("", "")])
def test_normalize_domain(raw, want):
    assert normalize_domain(raw) == want


def test_handoff_without_approval_stages_and_writes_nothing(twenty):
    fake, client = twenty()
    res = build_twenty_operator(client=client).invoke("crm.enrichment.handoff", dict(OK))
    assert res["status"] == "pending_approval" and res["executed"] is False and res["enrichment_written"] is False
    assert fake.posts == []


def test_approved_handoff_writes_company_then_linked_person(twenty):
    fake, client = twenty()
    res = build_twenty_operator(client=client).invoke("crm.enrichment.handoff", {**OK, "_approval": {"approved": True}})
    assert res["status"] == "done" and res["shadow"] is False and res["provenance"] == PROV
    assert [p[0] for p in fake.posts] == ["companies", "people"]
    assert fake.posts[0][1]["domainName"]["primaryLinkUrl"] == "acme.com"
    assert fake.posts[1][1]["companyId"] == res["company_id"] == "id-1"
    assert fake.posts[1][1]["emails"]["primaryEmail"] == "dana@acme.com"


def test_approved_handoff_in_shadow_mode_calls_nothing(twenty):
    fake, _ = twenty()
    client = TwentyWriteClient(base_url="http://twenty.example", api_key="k", shadow=True)
    res = core_enrichment_handoff(dict(OK), client=client)
    assert res["status"] == "done" and res["shadow"] is True and fake.posts == []


@pytest.mark.parametrize("patch,reason", [
    ({"provenance": []}, "no provenance"),
    ({"provenance": [{"provider": "hunter"}]}, "no provenance"),
    ({"contact": {"name": "Dana Rivera", "email": "dana@acme.com", "verified": False}}, "not verified"),
    ({"contact": {"name": "Dana Rivera", "email": "dana@acme.com", "verified": None}}, "not verified"),
    ({"contact": {"name": "Dana Rivera", "email": "dana@gmail.com", "verified": True}}, "not at the company domain"),
    ({"company": {"name": "Acme", "domain": "nope"}}, "company name/domain"),
])
def test_handoff_refuses_before_any_write(twenty, patch, reason):
    fake, client = twenty()
    res = core_enrichment_handoff({**OK, **patch}, client=client)
    assert res["status"] == "error" and reason in res["error"] and fake.posts == []


def test_domain_mismatch_needs_the_approvers_explicit_flag(twenty):
    fake, client = twenty()
    inp = {**OK, "contact": {"name": "Dana Rivera", "email": "dana@acme-holding.com", "verified": True},
           "allow_domain_mismatch": True}
    assert core_enrichment_handoff(inp, client=client)["status"] == "done"


@pytest.mark.parametrize("stored", ["acme.com", "https://acme.com", "https://www.acme.com/", "HTTP://ACME.COM"])
def test_company_entered_another_way_is_matched_not_duplicated(twenty, stored):
    fake, client = twenty(companies=[{"id": "c-1", "name": "Acme", "domainName": {"primaryLinkUrl": stored}}])
    res = client.upsert_company(name="Acme GmbH", domain="www.acme.com")
    assert res["created"] is False and res["company"]["id"] == "c-1" and fake.posts == []


def test_a_longer_domain_containing_the_host_is_not_a_match(twenty):
    fake, client = twenty(companies=[{"id": "c-1", "name": "Other", "domainName": {"primaryLinkUrl": "notacme.com"}},
                                     {"id": "c-2", "name": "Sub", "domainName": {"primaryLinkUrl": "acme.com.evil.io"}}])
    res = client.upsert_company(name="Acme", domain="acme.com")
    assert res["created"] is True and len(fake.posts) == 1


def test_failed_company_lookup_does_not_create(twenty):
    fake, client = twenty(lookup_status=500)
    res = client.upsert_company(name="Acme", domain="acme.com")
    assert res["created"] is False and "lookup failed" in res["error"] and fake.posts == []


def test_manifest_gates_every_write():
    specs = {c.name: c for c in build_twenty_operator().manifest.capabilities}
    assert set(specs) == {"crm.contact.upsert", "crm.company.upsert", "crm.enrichment.handoff"}
    assert all(s.approval_required and s.side_effecting and "crm:write" in s.permissions for s in specs.values())

"""outreach-engine x Hunter(/Apollo/Clearbit/PDL) enrichment wiring.

`core.enrich_contact` / `core.enrich` resolve a verified business email for a ranked lead's point of
contact via `agentic_os.world.enrichment`. These tests exercise the wiring with a fake provider (never
real HTTP) — the provider adapters themselves are already covered by tests/test_world_enrichment.py.

The package dir has a HYPHEN (`outreach-engine`), so it's loaded via importlib, matching the other
tests in this app.
"""
from __future__ import annotations

import importlib

import pytest

core = importlib.import_module("outreach-engine.core")


class _FakeProvider:
    name = "hunter"

    def __init__(self, email="jordan@acme.com", verified=True, confidence=0.9):
        self._email, self._verified, self._confidence = email, verified, confidence

    def find_email(self, *, domain, first_name, last_name):
        assert domain == "acme.com" and first_name == "Jordan" and last_name == "Lee"
        return {"email": self._email, "confidence": self._confidence, "verified": None}

    def verify_email(self, email):
        assert email == self._email
        return {"status": "valid" if self._verified else "invalid", "score": 0.95, "verified": self._verified}


@pytest.fixture(autouse=True)
def _reset_state():
    core._STATE.update(accounts=[], approved=set(), candidates=[])
    yield


def test_enrich_contact_resolves_a_verified_email_via_the_provider(monkeypatch):
    monkeypatch.setattr(core, "_gh_profile", lambda login: {"name": "Someone Else", "blog": "https://www.acme.com"})
    monkeypatch.setattr(core, "_gh_top_contributor", lambda full: {"name": "Jordan Lee"})
    row = {"account": "acme-oss", "source": "github", "full_name": "acme-oss/acme"}
    result = core.enrich_contact(row, provider=_FakeProvider())
    assert result["email"] == "jordan@acme.com" and result["verified"] is True and result["provider"] == "hunter"


def test_enrich_contact_falls_back_to_the_owner_profile_name_with_no_top_contributor(monkeypatch):
    monkeypatch.setattr(core, "_gh_profile", lambda login: {"name": "Jordan Lee", "blog": "acme.com"})
    monkeypatch.setattr(core, "_gh_top_contributor", lambda full: {})
    row = {"account": "acme-oss", "source": "github", "full_name": "acme-oss/acme"}
    result = core.enrich_contact(row, provider=_FakeProvider())
    assert result["email"] == "jordan@acme.com"


def test_enrich_contact_degrades_without_a_domain_never_fabricates():
    # the offline DEMO leads are "manual" source: no GitHub profile, so no domain is ever available.
    row = dict(core.DEMO[0])
    result = core.enrich_contact(row, provider=_FakeProvider())
    assert result == {"email": None, "verified": False, "provider": None,
                       "reason": "no company domain available for this lead"}


def test_enrich_action_attaches_the_result_onto_the_pipeline_row(monkeypatch):
    monkeypatch.setattr(core, "_gh_profile", lambda login: {"name": "Jordan Lee", "blog": "acme.com"})
    monkeypatch.setattr(core, "_gh_top_contributor", lambda full: {})
    monkeypatch.setattr(core, "enrich_contact",
                        lambda row, provider=None: {"email": "jordan@acme.com", "verified": True,
                                                    "provider": "hunter", "confidence": 0.95})
    core._STATE["accounts"] = [{"account": "acme-oss", "source": "github"}]
    res = core.enrich({"account": "acme-oss"})
    assert res["ok"] is True and res["action"] == "enrich" and res["email"] == "jordan@acme.com"
    row = core._STATE["accounts"][0]
    assert row["contact_email"] == "jordan@acme.com" and row["email_verified"] is True
    assert row["email_provider"] == "hunter"


def test_enrich_unknown_account_is_an_honest_miss_not_a_crash():
    core._STATE["accounts"] = []
    res = core.enrich({"account": "does-not-exist"})
    assert res["ok"] is False and "refresh the pipeline" in res["reason"]


def test_enrich_with_no_provider_configured_never_fabricates(monkeypatch):
    monkeypatch.setattr(core, "_gh_profile", lambda login: {"name": "Jordan Lee", "blog": "acme.com"})
    monkeypatch.setattr(core, "_gh_top_contributor", lambda full: {})
    for v in ("HUNTER_API_KEY", "APOLLO_API_KEY", "CLEARBIT_API_KEY", "PDL_API_KEY", "REDEVOPS_ENRICHMENT_PROVIDER"):
        monkeypatch.delenv(v, raising=False)
    row = {"account": "acme-oss", "source": "github", "full_name": "acme-oss/acme"}
    result = core.enrich_contact(row)
    assert result["email"] is None and "no enrichment provider" in result["reason"]

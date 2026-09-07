"""NHTSA recall crawler → KnowledgeDocs (offline, injectable fetch)."""
from __future__ import annotations

from agentic_os.automotive import CONSUMER_MAKES, KnowledgeDoc, nhtsa_recall_docs


def _fake_fetch(url: str):
    if "products/vehicle/models" in url and "HONDA" in url:
        return {"results": [{"model": "ACCORD"}, {"model": "CIVIC"}]}
    if "products/vehicle/models" in url:
        return {"results": []}
    if "recallsByVehicle" in url and "ACCORD" in url:
        return {"results": [{"NHTSACampaignNumber": "20V314000", "Component": "FUEL PUMP",
                             "Summary": "fuel pump may fail", "Consequence": "engine may stall",
                             "Remedy": "replace the fuel pump", "Make": "HONDA", "Model": "ACCORD",
                             "ModelYear": "2019"}]}
    if "recallsByVehicle" in url and "CIVIC" in url:
        return {"results": [{"NHTSACampaignNumber": "20V314000", "Component": "FUEL PUMP",  # dup campaign
                             "Summary": "fuel pump may fail", "Make": "HONDA", "Model": "CIVIC",
                             "ModelYear": "2019"},
                            {"NHTSACampaignNumber": "21V999000", "Component": "BRAKES",
                             "Summary": "brake line corrosion", "Consequence": "reduced braking",
                             "Remedy": "inspect and replace", "Make": "HONDA", "Model": "CIVIC",
                             "ModelYear": "2019"}]}
    return {"results": []}


def test_crawl_dedups_by_campaign_and_maps_docs():
    docs = nhtsa_recall_docs(fetch=_fake_fetch, years=[2019], makes=["HONDA"])
    ids = {d.doc_id for d in docs}
    assert ids == {"nhtsa-recall:20V314000", "nhtsa-recall:21V999000"}   # dup campaign collapsed
    fuel = next(d for d in docs if d.doc_id == "nhtsa-recall:20V314000")
    assert fuel.source == "nhtsa-recall" and fuel.make == "Honda" and fuel.title == "FUEL PUMP"
    assert "engine may stall" in fuel.body and "Remedy: replace the fuel pump" in fuel.body
    assert fuel.dtc == ""                                  # recalls aren't DTC-keyed
    assert "FUEL PUMP" in fuel.embed_text()


def test_max_docs_caps_crawl():
    docs = nhtsa_recall_docs(fetch=_fake_fetch, years=[2019], makes=["HONDA"], max_docs=1)
    assert len(docs) == 1


def test_consumer_makes_are_passenger_brands():
    assert "TOYOTA" in CONSUMER_MAKES and "HONDA" in CONSUMER_MAKES and "TESLA" in CONSUMER_MAKES
    assert len(CONSUMER_MAKES) >= 20


def test_missing_campaign_skipped():
    def fetch(url):
        if "models" in url: return {"results": [{"model": "X"}]}
        if "recallsByVehicle" in url: return {"results": [{"Component": "no campaign id"}]}
        return {"results": []}
    assert nhtsa_recall_docs(fetch=fetch, years=[2020], makes=["FORD"]) == []

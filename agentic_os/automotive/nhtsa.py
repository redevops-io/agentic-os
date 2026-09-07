"""NHTSA recalls → evidence-lake KnowledgeDocs (real defect → consequence → remedy grounding).

NHTSA's keyless recalls API gives, per vehicle, the component, a summary, the safety consequence, and
the manufacturer's remedy — exactly the "known failure → fix" grounding a diagnosis benefits from,
complementing the DTC-definition corpus. This crawls makes → models → recalls for a set of consumer
makes/years, dedups by campaign number, and maps each to a ``KnowledgeDoc`` (source ``nhtsa-recall``).

HTTP is injectable (``fetch(url) -> dict``) so the crawler is unit-tested offline; the live crawler
uses the public api.nhtsa.gov endpoints (no key).
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Sequence

from .knowledge import KnowledgeDoc

API = "https://api.nhtsa.gov"
Fetch = Callable[[str], Dict]

# high-volume US consumer makes — the vehicles the product actually targets (not trailers/fire trucks)
CONSUMER_MAKES = [
    "TOYOTA", "HONDA", "FORD", "CHEVROLET", "NISSAN", "JEEP", "HYUNDAI", "KIA", "SUBARU", "GMC",
    "RAM", "DODGE", "VOLKSWAGEN", "BMW", "MERCEDES-BENZ", "AUDI", "MAZDA", "LEXUS", "TESLA",
    "CHRYSLER", "BUICK", "CADILLAC", "ACURA", "VOLVO", "MITSUBISHI",
]


def _urllib_fetch(url: str) -> Dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def _models(fetch: Fetch, make: str, year: int) -> List[str]:
    url = f"{API}/products/vehicle/models?modelYear={year}&make={urllib.parse.quote(make)}&issueType=r"
    try:
        res = fetch(url).get("results") or []
    except Exception:  # noqa: BLE001
        return []
    return sorted({(r.get("model") or "").strip() for r in res if r.get("model")})


def _recalls(fetch: Fetch, make: str, model: str, year: int) -> List[Dict]:
    url = (f"{API}/recalls/recallsByVehicle?make={urllib.parse.quote(make)}"
           f"&model={urllib.parse.quote(model)}&modelYear={year}")
    try:
        return fetch(url).get("results") or []
    except Exception:  # noqa: BLE001
        return []


def _to_doc(rec: Dict) -> Optional[KnowledgeDoc]:
    campaign = (rec.get("NHTSACampaignNumber") or "").strip()
    if not campaign:
        return None
    comp = (rec.get("Component") or "").strip()
    body = " ".join(p for p in (
        comp,
        (rec.get("Summary") or "").strip(),
        ("Consequence: " + rec["Consequence"].strip()) if rec.get("Consequence") else "",
        ("Remedy: " + rec["Remedy"].strip()) if rec.get("Remedy") else "",
    ) if p)
    return KnowledgeDoc(
        doc_id=f"nhtsa-recall:{campaign}", source="nhtsa-recall", dtc="",
        make=(rec.get("Make") or "").title(), model=(rec.get("Model") or "").title(),
        year=str(rec.get("ModelYear") or ""), title=(comp or campaign), body=body)


def nhtsa_recall_docs(*, fetch: Optional[Fetch] = None, years: Sequence[int],
                      makes: Sequence[str] = CONSUMER_MAKES, sleep: float = 0.0,
                      max_docs: int = 0, on_progress: Callable[[int, int], None] = None) -> List[KnowledgeDoc]:
    """Crawl makes→models→recalls for the given years, dedup by campaign → KnowledgeDocs."""
    fetch = fetch or _urllib_fetch
    seen: Dict[str, KnowledgeDoc] = {}
    calls = 0
    for year in years:
        for make in makes:
            for model in _models(fetch, make, year):
                for rec in _recalls(fetch, make, model, year):
                    doc = _to_doc(rec)
                    if doc and doc.doc_id not in seen:
                        seen[doc.doc_id] = doc
                        if on_progress:
                            on_progress(len(seen), calls)
                        if max_docs and len(seen) >= max_docs:
                            return list(seen.values())
                calls += 1
                if sleep:
                    time.sleep(sleep)
    return list(seen.values())

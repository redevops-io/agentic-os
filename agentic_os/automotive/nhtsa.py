"""NHTSA recalls + TSBs → evidence-lake KnowledgeDocs (real defect → consequence → remedy grounding).

NHTSA's keyless recalls API gives, per vehicle, the component, a summary, the safety consequence, and
the manufacturer's remedy — exactly the "known failure → fix" grounding a diagnosis benefits from,
complementing the DTC-definition corpus. This crawls makes → models → recalls for a set of consumer
makes/years, dedups by campaign number, and maps each to a ``KnowledgeDoc`` (source ``nhtsa-recall``).

NHTSA's Technical Service Bulletins / Manufacturer Communications add the *diagnostic* layer recalls
lack: a manufacturer's own "if you see this symptom on this model, here's the fix" bulletins. There is
no keyless per-vehicle TSB API, but the ODI publishes them as 5-year CSV chunks (small-column format:
``TSB/Document ID, Make, Model, Model Year, Concise Summary`` — one record per bulletin per product,
intended for keyword search of the summary). ``tsb_docs_from_rows`` maps those to ``KnowledgeDoc``
(source ``nhtsa-tsb``), filtered to consumer makes and capped per make so a few high-volume filers
don't swamp the corpus. Row parsing is pure (fed dict rows) so it is unit-tested offline; the CLI
downloads + unzips the chunks. Customer *complaints* are intentionally skipped (low diagnostic value).

HTTP is injectable (``fetch(url) -> dict``) so the recalls crawler is unit-tested offline; the live
crawler uses the public api.nhtsa.gov endpoints (no key).
"""
from __future__ import annotations

import csv
import hashlib
import json
import time
import urllib.parse
import urllib.request
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence

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


# --- Technical Service Bulletins / Manufacturer Communications (5-year CSV chunks) ---

TSB_BASE = "https://static.nhtsa.gov/odi/ffdd/tsbs"
# small-column CSV chunks (Make/Model/Year + Concise Summary); MFR_COMMS_* is the post-2024 naming
TSB_CHUNKS = ["2020-2024", "2025-2026"]


def _tsb_title(summary: str) -> str:
    """A short, human label for the bulletin — its leading topic phrase, else a capped prefix."""
    s = " ".join(summary.split())
    for sep in (": ", " - "):
        if sep in s[:150]:
            head = s.split(sep, 1)[0].strip()
            if 4 <= len(head) <= 120:
                return head
    return (s[:100].rsplit(" ", 1)[0] + "…") if len(s) > 100 else s


def _tsb_doc(row: Dict[str, str], seen: set) -> Optional[KnowledgeDoc]:
    make = (row.get("Make") or "").strip()
    summary = " ".join((row.get("Concise Summary") or "").split())
    if not make or not summary:
        return None
    tsb_id = (row.get("TSB/Document ID") or "").strip()
    model = (row.get("Model") or "").strip()
    year = (row.get("Model Year") or "").strip()
    key = hashlib.blake2b(f"{tsb_id}|{make}|{model}|{year}".encode(), digest_size=8).hexdigest()
    doc_id = f"nhtsa-tsb:{key}"
    if doc_id in seen:
        return None
    seen.add(doc_id)
    return KnowledgeDoc(doc_id=doc_id, source="nhtsa-tsb", dtc="", make=make.title(),
                        model=model.title(), year=year, title=_tsb_title(summary), body=summary)


def tsb_docs_from_rows(rows: Iterable[Dict[str, str]], *, makes: Sequence[str] = CONSUMER_MAKES,
                       per_make_cap: int = 0, max_docs: int = 0,
                       on_progress: Callable[[int], None] = None) -> List[KnowledgeDoc]:
    """Map TSB CSV rows → KnowledgeDocs, filtered to ``makes`` and capped per make.

    ``per_make_cap`` keeps a few high-volume filers (Porsche/Mercedes/Audi file tens of thousands)
    from swamping the corpus; ``max_docs`` bounds the total. Dedups by (TSB id, make, model, year).
    """
    allow = {m.strip().upper() for m in makes} if makes else None
    seen: set = set()
    per_make: Dict[str, int] = {}
    out: List[KnowledgeDoc] = []
    for row in rows:
        mk = (row.get("Make") or "").strip().upper()
        if allow is not None and mk not in allow:
            continue
        if per_make_cap and per_make.get(mk, 0) >= per_make_cap:
            continue
        doc = _tsb_doc(row, seen)
        if not doc:
            continue
        per_make[mk] = per_make.get(mk, 0) + 1
        out.append(doc)
        if on_progress and len(out) % 500 == 0:
            on_progress(len(out))
        if max_docs and len(out) >= max_docs:
            break
    return out


def iter_tsb_csv_rows(paths: Sequence[str]) -> Iterator[Dict[str, str]]:
    """Yield dict rows from local TSB CSV files (small-column format)."""
    for p in paths:
        with open(p, newline="", encoding="utf-8", errors="replace") as fh:
            yield from csv.DictReader(fh)

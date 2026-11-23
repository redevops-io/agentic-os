"""Authorized product-identity feeds.

A feed is used under the terms its publisher states, recorded on the ``CatalogSource``: the page that states
them, the words, and the uses they permit. A feed gives product identity (what a barcode is). It carries no
price for this buyer, so its rows class as ``unconfirmed`` and a comparison answers "request a quote".

Open Food Facts is the first one: a public product database published under the Open Database License,
which permits use with attribution.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable, List, Tuple

from agentic_os.intelligence.adapters._http import http_get_json

from .catalog import AUTHORIZED_FEED, INTERNAL_SOURCING, CatalogSource, today

OFF_TERMS_URL = "https://world.openfoodfacts.org/terms-of-use"
OFF_TERMS_QUOTE = "The Open Food Facts database is available under the Open Database License."
OFF_ATTRIBUTION = "Product data: Open Food Facts (openfoodfacts.org), Open Database License"
_UA = {"User-Agent": "redevops-procurement-catalog/0.1 (catalog identity lookup)"}


def open_food_facts_source(tenant: str, *, recorded_by: str, source_id: str = "open-food-facts") -> CatalogSource:
    """The source record for Open Food Facts. ``recorded_by`` is the person who read the terms for this tenant."""
    return CatalogSource(
        source_id=source_id, tenant=tenant, kind=AUTHORIZED_FEED, name="Open Food Facts", permitted_uses=(INTERNAL_SOURCING,),
        authorized_by=recorded_by, authorization_ref=OFF_TERMS_URL, authorization_quote=OFF_TERMS_QUOTE,
        authorized_at=today(), licence="ODbL 1.0", attribution=OFF_ATTRIBUTION)


def open_food_facts_rows(gtins: Iterable[str], *, supplier_ref: str = "unassigned",
                         fetch: Callable[..., Tuple[int, Any]] = http_get_json) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """(rows ready for ``Catalog.ingest``, misses). One request per barcode; a barcode the database does not
    know, or a failed request, is a miss with its reason, never a made-up row."""
    rows, misses = [], []
    for raw in gtins:
        gtin = re.sub(r"\D", "", str(raw))
        if not 8 <= len(gtin) <= 14:
            misses.append({"gtin": str(raw), "reason": "not an 8 to 14 digit barcode"})
            continue
        url = f"https://world.openfoodfacts.org/api/v2/product/{gtin}.json?fields=code,product_name,brands,quantity,categories_tags"
        try:
            status, body = fetch(url, _UA)
        except Exception as e:  # noqa: BLE001
            misses.append({"gtin": gtin, "reason": f"request failed: {type(e).__name__}"})
            continue
        product = (body or {}).get("product") if status == 200 and (body or {}).get("status") == 1 else None
        if not product or not str(product.get("product_name") or "").strip():
            misses.append({"gtin": gtin, "reason": f"not in the database (http {status})"})
            continue
        cats = [c.split(":", 1)[-1].replace("-", " ") for c in product.get("categories_tags") or []]
        title = " ".join(p for p in (str(product.get("product_name")).strip(), str(product.get("quantity") or "").strip()) if p)
        rows.append({"supplier_ref": supplier_ref, "sku": f"gtin-{gtin}", "title": title, "gtin": gtin,
                     "brand": str(product.get("brands") or "").split(",")[0].strip(), "category": cats[-1] if cats else "",
                     "document_ref": url})
    return rows, misses

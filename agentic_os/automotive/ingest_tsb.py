"""CLI: download NHTSA Technical Service Bulletins (Manufacturer Communications) and ingest them
into the Doris evidence lake.

    python -m agentic_os.automotive.ingest_tsb \
        --chunks 2020-2024 2025-2026 --per-make-cap 2000 [--max-docs 40000]

Downloads the 5-year CSV chunks (small-column format), filters to consumer makes, caps per make so a
few high-volume filers don't swamp the corpus, embeds with fastembed and ingests (source ``nhtsa-tsb``).
Pure grounding data — no train/test split (the DTC corpus holds the evaluation split). Needs
CAR_DORIS_HOST/PORT and fastembed. Complaints are intentionally not ingested.
"""
from __future__ import annotations

import argparse
import io
import os
import time
import urllib.request
import zipfile

from .knowledge import fastembed_embedder, ingest_knowledge
from .nhtsa import (
    CONSUMER_MAKES,
    TSB_BASE,
    TSB_CHUNKS,
    iter_tsb_csv_rows,
    tsb_docs_from_rows,
)
from .store_doris import DorisCaseStore


def _download_chunk(chunk: str, cache_dir: str) -> str:  # pragma: no cover - live data job
    """Download + unzip one MFR_COMMS_RECEIVED_<chunk>.zip, return the extracted CSV path (cached)."""
    os.makedirs(cache_dir, exist_ok=True)
    csv_path = os.path.join(cache_dir, f"MFR_COMMS_RECEIVED_{chunk}.csv")
    if os.path.exists(csv_path) and os.path.getsize(csv_path) > 0:
        return csv_path
    url = f"{TSB_BASE}/MFR_COMMS_RECEIVED_{chunk}.zip"
    print(f"  downloading {url}", flush=True)
    with urllib.request.urlopen(url, timeout=180) as r:
        blob = r.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        with zf.open(name) as src, open(csv_path, "wb") as dst:
            dst.write(src.read())
    return csv_path


def main() -> None:  # pragma: no cover - live data job
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", nargs="+", default=TSB_CHUNKS)
    ap.add_argument("--per-make-cap", type=int, default=2000)
    ap.add_argument("--max-docs", type=int, default=0)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--cache-dir", default="/tmp/nhtsa_tsb")
    args = ap.parse_args()

    t0 = time.time()
    print(f"downloading {len(args.chunks)} TSB chunk(s): {args.chunks}", flush=True)
    paths = [_download_chunk(c, args.cache_dir) for c in args.chunks]

    docs = tsb_docs_from_rows(
        iter_tsb_csv_rows(paths), makes=CONSUMER_MAKES, per_make_cap=args.per_make_cap,
        max_docs=args.max_docs,
        on_progress=lambda n: print(f"  parsed {n} TSBs ({time.time()-t0:.0f}s)", flush=True))
    print(f"parsed {len(docs)} TSBs (consumer makes, cap {args.per_make_cap}/make) "
          f"in {time.time()-t0:.0f}s", flush=True)

    embed = fastembed_embedder()
    store = DorisCaseStore(host=os.environ.get("CAR_DORIS_HOST", ""),
                           port=int(os.environ.get("CAR_DORIS_PORT", "9030")))
    te = time.time()
    n = ingest_knowledge(store, docs, embed, batch=args.batch,
                         progress=lambda a, b: print(f"  ingested {a}/{b} ({time.time()-te:.0f}s)",
                                                     flush=True) if a % (args.batch * 4) == 0 or a == b else None)
    total = len(store._execute("SELECT doc_id FROM car_diagnosis.knowledge", []) or [])
    print(f"TSB ingest done: {n} bulletins in {time.time()-te:.0f}s; knowledge rows now: {total}",
          flush=True)


if __name__ == "__main__":  # pragma: no cover
    main()

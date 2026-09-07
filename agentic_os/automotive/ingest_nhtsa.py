"""CLI: crawl NHTSA recalls and ingest them into the Doris evidence lake.

    python -m agentic_os.automotive.ingest_nhtsa --years 2020 2021 2022 2023 2024 [--max-docs 6000]

Needs CAR_DORIS_HOST/PORT and fastembed. Pure grounding data (no train/test split — the DTC corpus
holds the evaluation split); dedup by NHTSA campaign number.
"""
from __future__ import annotations

import argparse
import os
import time

from .knowledge import fastembed_embedder, ingest_knowledge
from .nhtsa import CONSUMER_MAKES, nhtsa_recall_docs
from .store_doris import DorisCaseStore


def main() -> None:  # pragma: no cover - live data job
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, nargs="+", default=[2020, 2021, 2022, 2023, 2024])
    ap.add_argument("--max-docs", type=int, default=0)
    ap.add_argument("--batch", type=int, default=256)
    args = ap.parse_args()

    t0 = time.time()
    print(f"crawling NHTSA recalls: years={args.years} makes={len(CONSUMER_MAKES)}", flush=True)
    docs = nhtsa_recall_docs(years=args.years, max_docs=args.max_docs,
                             on_progress=lambda n, c: print(f"  found {n} unique recalls "
                                                            f"({c} calls, {time.time()-t0:.0f}s)",
                                                            flush=True) if n % 250 == 0 else None)
    print(f"crawled {len(docs)} unique recalls in {time.time()-t0:.0f}s", flush=True)

    embed = fastembed_embedder()
    store = DorisCaseStore(host=os.environ.get("CAR_DORIS_HOST", ""),
                           port=int(os.environ.get("CAR_DORIS_PORT", "9030")))
    te = time.time()
    n = ingest_knowledge(store, docs, embed, batch=args.batch,
                         progress=lambda a, b: print(f"  ingested {a}/{b} ({time.time()-te:.0f}s)",
                                                     flush=True) if a % (args.batch * 4) == 0 or a == b else None)
    total = len(store._execute("SELECT doc_id FROM car_diagnosis.knowledge", []) or [])
    print(f"NHTSA ingest done: {n} recalls in {time.time()-te:.0f}s; knowledge rows now: {total}", flush=True)


if __name__ == "__main__":  # pragma: no cover
    main()

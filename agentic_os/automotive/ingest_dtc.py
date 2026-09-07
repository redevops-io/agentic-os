"""CLI: split a DTC dataset randomly, ingest the train part into the Doris evidence lake, hold out the
rest for tests, and report retrieval recall on the holdout.

    python -m agentic_os.automotive.ingest_dtc /path/dtc_codes.db \
        --test-frac 0.2 --seed 42 --out-test /path/dtc_test.jsonl

Needs CAR_DORIS_HOST/PORT set and fastembed installed (real 1024-dim embeddings).
"""
from __future__ import annotations

import argparse
import json
import os
import time

from .knowledge import (
    fastembed_embedder,
    ingest_knowledge,
    load_dtc_sqlite,
    retrieval_eval,
    split_dataset,
)
from .store_doris import DorisCaseStore


def main() -> None:  # pragma: no cover - live data job
    ap = argparse.ArgumentParser()
    ap.add_argument("sqlite")
    ap.add_argument("--test-frac", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out-test", default="/tmp/dtc_test.jsonl")
    ap.add_argument("--eval-sample", type=int, default=300)
    ap.add_argument("--batch", type=int, default=256)
    args = ap.parse_args()

    docs = load_dtc_sqlite(args.sqlite)
    train, test = split_dataset(docs, test_frac=args.test_frac, seed=args.seed)
    print(f"loaded {len(docs)} docs → ingest {len(train)}, holdout {len(test)} (seed {args.seed})", flush=True)

    with open(args.out_test, "w") as fh:
        for d in test:
            fh.write(json.dumps(d.__dict__) + "\n")
    print(f"wrote holdout → {args.out_test}", flush=True)

    embed = fastembed_embedder()
    store = DorisCaseStore(host=os.environ.get("CAR_DORIS_HOST", ""),
                           port=int(os.environ.get("CAR_DORIS_PORT", "9030")))
    t0 = time.time()

    def prog(n, total):
        if n % (args.batch * 8) == 0 or n == total:
            print(f"  ingested {n}/{total} ({(time.time()-t0):.0f}s)", flush=True)

    ingest_knowledge(store, train, embed, batch=args.batch, progress=prog)
    print(f"ingest done in {(time.time()-t0):.0f}s", flush=True)

    ev = retrieval_eval(store, test, embed, k=5, limit=args.eval_sample)
    print(f"RETRIEVAL EVAL on {ev['n']} held-out DTCs: recall@1={ev['recall@1']:.3f} "
          f"recall@5={ev['recall@5']:.3f}", flush=True)


if __name__ == "__main__":  # pragma: no cover
    main()

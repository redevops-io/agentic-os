"""Resource limits for one evidence query (per worker process)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    timeout_s: float = 10.0          # wall clock; the worker's process group is killed on expiry
    memory_mb: int = 256             # DuckDB memory_limit AND an RSS watchdog in the worker (RLIMIT_AS on Linux)
    max_rows: int = 500              # rows returned (more is reported as truncated)
    max_result_bytes: int = 2_000_000
    max_sql_chars: int = 8_000
    max_input_rows: int = 200_000    # rows a scope may load into one worker
    max_input_bytes: int = 64_000_000
    threads: int = 1

"""Small in-process token bucket (per key and global) for the public synthetic profile."""
from __future__ import annotations

import threading
import time
from typing import Dict, Tuple


class RateLimiter:
    def __init__(self, per_key_per_min: int = 20, global_per_min: int = 120, max_keys: int = 4096):
        self.per_key, self.global_, self.max_keys = per_key_per_min, global_per_min, max_keys
        self._lock = threading.Lock()
        self._hits: Dict[str, Tuple[float, int]] = {}
        self._g: Tuple[float, int] = (time.monotonic(), 0)

    @staticmethod
    def _bump(state: Tuple[float, int], limit: int, now: float) -> Tuple[Tuple[float, int], bool]:
        start, n = state
        if now - start >= 60.0:
            start, n = now, 0
        n += 1
        return (start, n), n <= limit

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            self._g, ok_g = self._bump(self._g, self.global_, now)
            if len(self._hits) >= self.max_keys and key not in self._hits:
                self._hits.clear()
            self._hits[key], ok_k = self._bump(self._hits.get(key, (now, 0)), self.per_key, now)
            return ok_g and ok_k

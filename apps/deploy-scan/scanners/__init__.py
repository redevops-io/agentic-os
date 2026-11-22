"""Each scanner module exposes one function, ``run(target: str, **opts) -> list[Finding]``, and does
its own subprocess/HTTP work. No scanner imports another; `jobs.py` is the only thing that knows the
profile -> scanner mapping, so adding a profile never touches an existing one."""
from __future__ import annotations

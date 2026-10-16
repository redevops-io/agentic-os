#!/usr/bin/env python3
"""Generate the shared Python<->Go parity fixtures under go/mission/testdata/.

The Python implementation is the source of truth: every fixture is computed by calling the real Python code. Go
tests (go/mission/*_parity_test.go) replay the same inputs and must produce the same outputs, and
tests/test_go_parity_fixtures.py regenerates everything here and compares it to the checked-in files, so a change
to Python behaviour fails CI until the fixtures (and the Go port) are updated.

    python scripts/gen_parity_fixtures.py --write     # (re)write go/mission/testdata/*.json
    python scripts/gen_parity_fixtures.py --check     # exit 1 if a checked-in fixture is stale
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
OUT = ROOT / "go" / "mission" / "testdata"
ENV = "AGENTIC_OS_DEFAULT_FAILURE_POLICY"



@contextlib.contextmanager
def _env(**kv):
    old = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


TEMPLATES = [None, "onboarding", "deploy_app", "sky_deploy", "invoice_recovery", "cost_audit", "no_such_template"]
ENVS = [None, "", "COMPENSATE_DECLARED", "halt_and_report", " preserve_only ", "CONTINUE_SIBLINGS", "PRESERVE_ONLY",
        "HALT_AND_REPORT", "bogus"]


def policy_resolution() -> dict:
    from agentic_os.mission.compensation_policy import (effective_policy, is_legacy_frozen,
                                                        resolve_default_detailed)
    from agentic_os.mission.runtime import MissionRuntime
    from agentic_os.mission.executor import Executor, InMemoryOperatorClient
    from agentic_os.mission.registry import CapabilityRegistry

    resolve = []
    for t in TEMPLATES:
        for e in ENVS:
            with _env(**{ENV: e}):
                pol, src, warn = resolve_default_detailed(t)
            resolve.append({"template": t, "env": e, "policy": pol.value, "source": src, "warning": warn})

    effective = []
    for t in TEMPLATES:
        for own in ("", "COMPENSATE_DECLARED", "preserve_only", "weird"):
            for frozen in ("", "COMPENSATE_DECLARED", "PRESERVE_ONLY", "HALT_AND_REPORT"):
                for forked in (False, True):
                    for e in (None, "COMPENSATE_DECLARED"):
                        m = SimpleNamespace(template=t, failure_policy=own, default_failure_policy=frozen)
                        with _env(**{ENV: e}):
                            pol = effective_policy(m, forked=forked)
                        effective.append({"template": t, "failure_policy": own, "default_failure_policy": frozen,
                                          "forked": forked, "env": e, "policy": pol.value})

    legacy = []
    for t in TEMPLATES:
        for own in ("", "PRESERVE_ONLY"):
            for src in ("", "template"):
                for frozen in ("", "COMPENSATE_DECLARED", "PRESERVE_ONLY"):
                    m = SimpleNamespace(template=t, failure_policy=own, default_policy_source=src,
                                        default_failure_policy=frozen)
                    legacy.append({"template": t, "failure_policy": own, "default_policy_source": src,
                                   "default_failure_policy": frozen, "legacy": bool(is_legacy_frozen(m))})

    created = []
    for t in TEMPLATES:
        for own in ("", "COMPENSATE_DECLARED"):
            for e in (None, "COMPENSATE_DECLARED", "bogus"):
                with _env(**{ENV: e}):
                    rt = MissionRuntime(CapabilityRegistry(), Executor(InMemoryOperatorClient({})))
                    m = rt.create_mission("g", policy_refs=["*"], template=t, failure_policy=own)
                    p = next(ev.payload for ev in rt.store.for_mission(m.id) if ev.type == "MissionCreated")
                created.append({"template": t, "failure_policy": own, "env": e,
                                "recorded_failure_policy": p.get("failure_policy", ""),
                                "default_failure_policy": p["default_failure_policy"],
                                "default_policy_source": p["default_policy_source"],
                                "default_policy_warning": p.get("default_policy_warning", ""),
                                "effective": effective_policy(m).value})
    return {"resolve": resolve, "effective": effective, "legacy": legacy, "created": created}


FIXTURES = {"policy_resolution.json": policy_resolution}


def render(obj) -> str:
    return json.dumps(obj, indent=1, sort_keys=True) + "\n"


def build_all() -> dict[str, str]:
    # resolve_default_detailed warns ONCE per process (module-level set): do not leave it mutated for other tests
    from agentic_os.mission import compensation_policy as cp
    saved = set(cp._WARNED)
    log = logging.getLogger("agentic_os.mission.compensation_policy")
    level = log.level
    log.setLevel(logging.CRITICAL)
    try:
        return {name: render(fn()) for name, fn in FIXTURES.items()}
    finally:
        log.setLevel(level)
        cp._WARNED.clear()
        cp._WARNED.update(saved)


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "--check"
    stale = []
    for name, text in build_all().items():
        path = OUT / name
        if mode == "--write":
            OUT.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        elif not path.exists() or path.read_text() != text:
            stale.append(name)
    if stale:
        print("stale parity fixtures (run scripts/gen_parity_fixtures.py --write):", ", ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

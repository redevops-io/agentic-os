"""The acceptance scenarios of the permissions work, run through the reproducible sweep harness (``tests/sweeps/sweep.py``), and the harness's own mutation checks.

The full sweep (~85,000 calls: ``python tests/sweeps/sweep.py --fuzz 130``) is a command, not a unit test; this runs it at a small fuzz so every CI run still covers all 17 apps x six
modes x routes / ``Operator.invoke`` / HTTP ``/invoke`` / policy, plus the three start-up refusals. The harness is only evidence if it can fail, so a deliberately broken gate
must produce violations.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tests" / "sweeps"))
import sweep  # noqa: E402

from agentic_os import app_gate  # noqa: E402
from agentic_os.tool_actions import TOOL_ACTIONS  # noqa: E402


def _kinds(out):
    return {(v["surface"], v["what"]) for v in out["violations"]}


def test_the_sweep_finds_no_violation_over_all_17_apps_and_every_mode():
    out = sweep.run(fuzz=3, seed=11)
    assert out["apps"] == 17
    assert out["violations"] == [], out["violations"][:5]
    assert out["calls"] > 8000
    modes = set(out["by_mode"])
    assert set(sweep.MODES) | set(sweep.START_MODES) <= modes
    for mode in sweep.MODES:
        assert {"route", "operator", "http", "policy"} <= set(out["by_mode"][mode]), mode
    for mode in sweep.START_MODES:
        assert out["by_mode"][mode]["startup"] == 17


def test_every_write_classified_tool_is_denied_to_a_read_only_principal_and_to_an_unprovisioned_one(monkeypatch, tmp_path):
    from agentic_os.permissions_setup import init_local_profile
    writes = {(a, t) for a, tb in TOOL_ACTIONS.items() for t, act in tb.items() if act != "read"}
    assert len(writes) >= 60                                              # the sweep is not vacuous
    for mode, expect_denied in (("read-only", writes), ("unprovisioned", {(a, t) for a, tb in TOOL_ACTIONS.items() for t in tb})):
        seen = set()
        for app in sweep.APPS:
            mp = sweep.MonkeyPatch()
            try:
                sweep.configure(mp, mode, str(tmp_path / f"{mode}-{app}"))
                sweep.start_app(mp, app)
                for t in TOOL_ACTIONS[app]:
                    if app_gate.tool_denial(app, t) is not None:
                        seen.add((app, t))
            finally:
                mp.undo()
        assert expect_denied <= seen, sorted(expect_denied - seen)[:5]
        if mode == "read-only":
            reads = {(a, t) for a, tb in TOOL_ACTIONS.items() for t, act in tb.items() if act == "read"}
            assert not (reads & seen), sorted(reads & seen)[:5]            # and the read tools run


# ── the harness must be able to fail ──

def test_the_harness_detects_a_gate_that_lets_one_write_tool_through(monkeypatch):
    real = app_gate.tool_denial
    monkeypatch.setattr(app_gate, "tool_denial", lambda app, tool, *a, **k: None if (app, tool) == ("books", "books.close") else real(app, tool, *a, **k))
    out = sweep.run(fuzz=1, seed=1, apps=("books",), modes=("read-only",))
    assert ("policy", "tool not denied") in _kinds(out)


def test_the_harness_detects_a_gate_that_accepts_a_near_miss_name(monkeypatch):
    real = app_gate.tool_denial
    monkeypatch.setattr(app_gate, "tool_denial", lambda app, tool, *a, **k: real(app, tool.strip().lower(), *a, **k))
    out = sweep.run(fuzz=12, seed=1, apps=("books",), modes=("write",))
    assert ("policy", "a name that is not in the trusted table was allowed") in _kinds(out)


def test_the_harness_detects_an_operator_that_skips_the_gate(monkeypatch):
    monkeypatch.setattr(app_gate, "operator_denial", lambda *a, **k: None)
    out = sweep.run(fuzz=1, seed=1, apps=("growth-assistant",), modes=("unprovisioned",))
    assert any(v["surface"] in ("operator", "http") for v in out["violations"]), out["violations"][:3]


def test_the_harness_detects_a_denial_that_already_touched_the_network(monkeypatch):
    import socket
    original = app_gate.deny_response

    def leaky(*a, **k):
        try:
            socket.getaddrinfo("example.invalid", 80)                      # the sweep's sentinel refuses and records the lookup
        except OSError:
            pass
        return original(*a, **k)
    monkeypatch.setattr(app_gate, "deny_response", leaky)
    out = sweep.run(fuzz=0, seed=1, apps=("growth-assistant",), modes=("unprovisioned",))
    assert {v["what"] for v in out["violations"]} == {"a denied/refused call made an outbound attempt"}


def test_a_startup_that_does_not_refuse_is_reported(monkeypatch):
    monkeypatch.setattr(app_gate, "_check_switches", lambda *a, **k: None)         # the strict switch parsing is bypassed: "maybe" must then be reported
    out = sweep.run(fuzz=0, seed=1, apps=("books",), modes=())
    assert any(v["surface"] == "startup" and v["mode"] == "invalid-switch" for v in out["violations"]), out["violations"]

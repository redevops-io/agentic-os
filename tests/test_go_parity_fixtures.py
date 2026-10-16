"""Python <-> Go parity: the checked-in fixtures under go/mission/testdata are exactly what the Python implementation
produces today (scripts/gen_parity_fixtures.py), and the Go tests replay them. Python is the source of truth: a
behaviour change here fails this test until the fixtures are regenerated and the Go port follows."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _gen():
    spec = importlib.util.spec_from_file_location("gen_parity_fixtures", ROOT / "scripts" / "gen_parity_fixtures.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_checked_in_parity_fixtures_are_current():
    gen = _gen()
    built = gen.build_all()
    assert built, "no fixtures generated"
    for name, text in built.items():
        path = ROOT / "go" / "mission" / "testdata" / name
        assert path.exists(), f"{name} missing: run python scripts/gen_parity_fixtures.py --write"
        assert path.read_text() == text, f"{name} is stale: run python scripts/gen_parity_fixtures.py --write"

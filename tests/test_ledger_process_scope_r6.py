"""SR-04 round 6 (LGI-1, LGI-2): documented process-scope limits, pinned so the documentation stays true."""
from __future__ import annotations

import pathlib
import pickle
import subprocess
import sys

import pytest

from agentic_os.market import ingestion as ING
from agentic_os.market.ingestion import IngestionRefused, register_source_adapter, repo_adapter
from agentic_os.market.outcomes_join import FirstPartyLedger

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _py(code, mp_start=None):
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT),
                          env={**__import__("os").environ, "PYTHONPATH": str(ROOT)})


def test_lgi1_a_metric_minted_in_one_process_is_refused_in_a_spawned_one():
    blob = subprocess.run([sys.executable, "-c",
                           "import pickle, sys\nfrom agentic_os.market.ingestion import repo_adapter\n"
                           "sys.stdout.buffer.write(pickle.dumps(repo_adapter('umami').make_metric(metric='m', value=1.0, segment='c')))"],
                          capture_output=True, cwd=str(ROOT), env={**__import__("os").environ, "PYTHONPATH": str(ROOT)}).stdout
    m = pickle.loads(blob)                                             # a plain dataclass: it unpickles ...
    with pytest.raises(IngestionRefused):
        FirstPartyLedger().record(m)                                   # ... but this process has other keys


def test_lgi1_reloading_the_ingestion_module_rebuilds_vault_and_registry_so_older_ledgers_become_unreadable():
    r = _py("import importlib\n"
            "from agentic_os.market import ingestion as I\n"
            "from agentic_os.market.outcomes_join import FirstPartyLedger\n"
            "I.register_source_adapter('analytics', 'reload-probe')\n"
            "led = FirstPartyLedger(); led.record(I.repo_adapter('umami').make_metric(metric='m', value=1.0, segment='c'))\n"
            "assert led.value('m', 'c') == 1.0 and len(I.registered_adapters()) == 8\n"
            "importlib.reload(I)\n"
            "assert len(I.registered_adapters()) == 7                  # registry (and the 64 cap) started over\n"
            "try:\n    led.value('m', 'c')\nexcept ValueError as e:\n    print('unreadable')")
    assert r.returncode == 0 and r.stdout.strip() == "unreadable", r.stderr


def test_lgi2_the_registration_cap_can_be_exhausted_by_any_caller_of_register_source_adapter():
    r = _py("from agentic_os.market import ingestion as I\n"
            "for i in range(I.MAX_REGISTRATIONS):\n    I.register_source_adapter('crm', f'squat-{i:03d}')\n"
            "try:\n    I.register_source_adapter('crm', 'legit-late')\nexcept I.IngestionRefused as e:\n    print('blocked', 'limit' in str(e))")
    assert r.returncode == 0 and r.stdout.strip() == "blocked True", r.stderr


def test_the_limits_are_documented():
    text = " ".join((ROOT / "docs" / "ledger_source_inventory.md").read_text().split()).lower()
    for needle in ("spawn", "importlib.reload", "64 caller slots", "20 microseconds", "2 s per 100,000"):
        assert needle in text, needle
    assert "closure key recovery" in (ROOT / "docs" / "SEARCH_RELEVANCE.md").read_text().lower()

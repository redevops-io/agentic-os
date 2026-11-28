"""SR-04 round 5 (LG-1): the synthetic-research package is isolated from the ledger and the adapters.

(a) importing ANY synthetic_research module loads neither `market.ingestion` nor `market.outcomes_join` (subprocess);
(b) minting, registering and recording are refused when ANY frame of the synthetic-research package is on the call
stack (also through an unrelated helper module it calls); the signing key is not reachable through the adapter
(closure), and adapters refuse to be pickled or copied;
(c) the docs say the guard is bypassable by code in the same process and use no 'unforgeable' / 'hard guarantee'
wording. Evasions that need arbitrary code in the same process (gc / importlib / sys.modules from an unrelated
module) are a DOCUMENTED residual: these checks stop accidental and casual misuse."""
from __future__ import annotations

import copy
import pathlib
import pickle
import subprocess
import sys
import types

import pytest

from agentic_os.market import ingestion as ING
from agentic_os.market import outcomes_join as OJ
from agentic_os.market.ingestion import IngestionRefused, register_source_adapter, repo_adapter
from agentic_os.market.outcomes_join import FirstPartyLedger

ROOT = pathlib.Path(__file__).resolve().parent.parent
STUDY = "agentic_os.market.synthetic_research.fake_study_module"


def _fresh(code):
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT),
                          env={**__import__("os").environ, "PYTHONPATH": str(ROOT)})


# ── (a) the import chain ─────────────────────────────────────────────────────────────────────────────────
def test_importing_any_synthetic_research_module_does_not_load_the_ledger_or_the_adapters():
    r = _fresh(
        "import importlib, pkgutil, sys\n"
        "import agentic_os.market.synthetic_research as pkg\n"
        "mods = [m.name for m in pkgutil.iter_modules(pkg.__path__)]\n"
        "for m in mods:\n"
        "    importlib.import_module('agentic_os.market.synthetic_research.' + m)\n"
        "bad = [k for k in sys.modules if k.endswith('market.ingestion') or k.endswith('market.outcomes_join')]\n"
        "print(len(mods), bad)")
    assert r.returncode == 0, r.stderr
    n, bad = r.stdout.strip().split(" ", 1)
    assert int(n) >= 5 and bad == "[]"


def test_importing_each_module_alone_in_a_fresh_process_is_clean_too():
    names = [m.name for m in __import__("pkgutil").iter_modules(__import__(
        "agentic_os.market.synthetic_research", fromlist=["x"]).__path__)]
    for name in names:
        r = _fresh(f"import sys, agentic_os.market.synthetic_research.{name}\n"
                   "print([k for k in sys.modules if k.endswith('market.ingestion') or k.endswith('market.outcomes_join')])")
        assert r.returncode == 0 and r.stdout.strip() == "[]", (name, r.stdout, r.stderr[-300:])


def test_the_ledger_names_are_still_importable_from_the_market_package_lazily():
    r = _fresh("import sys, agentic_os.market as m\n"
               "assert 'agentic_os.market.outcomes_join' not in sys.modules\n"
               "from agentic_os.market import FirstPartyLedger, FirstPartyMetric, MeasuredOutcome, measure_experiment, conversion_delta\n"
               "assert 'agentic_os.market.outcomes_join' in sys.modules\n"
               "assert FirstPartyLedger.__module__ == 'agentic_os.market.outcomes_join'\n"
               "assert 'FirstPartyLedger' in dir(m) and m.FirstPartyLedger is FirstPartyLedger\n"
               "ns = {}\nexec('from agentic_os.market import *', ns)\nassert 'FirstPartyMetric' in ns\n"
               "try:\n    m.not_a_name\nexcept AttributeError:\n    print('ok')")
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr


# ── (b) caller checks ────────────────────────────────────────────────────────────────────────────────────
def _as_study(code, **extra):
    """Run `code` with a stack frame whose module name is inside the synthetic-research package."""
    ns = {"__name__": STUDY, **extra}
    exec(compile(code, "<study>", "exec"), ns)
    return ns


def test_study_code_cannot_mint_register_record_or_look_up_adapters():
    adapter = repo_adapter("umami")
    metric = adapter.make_metric(metric="signup", value=0.5, segment="treatment")
    for code in ("repo_adapter('umami')", "register_source_adapter('analytics', 'study-try-1')",
                 "adapter.make_metric(metric='x', value=1.0)", "adapter.attest(metric)",
                 "FirstPartyLedger().record(metric)"):
        with pytest.raises(IngestionRefused, match="synthetic-research"):
            _as_study(code, repo_adapter=repo_adapter, register_source_adapter=register_source_adapter,
                      adapter=adapter, metric=metric, FirstPartyLedger=FirstPartyLedger)
    assert FirstPartyLedger().record(metric) is metric                         # the same calls elsewhere work


def test_a_study_frame_anywhere_on_the_stack_is_refused_even_through_a_throwaway_helper_module():
    helper = types.ModuleType("throwaway_helper")
    exec("def mint(adapter):\n    return adapter.make_metric(metric='x', value=1.0)\n"
         "def rec(ledger, m):\n    return ledger.record(m)\n", helper.__dict__)
    adapter = repo_adapter("gsc")
    m = adapter.make_metric(metric="x", value=1.0)
    with pytest.raises(IngestionRefused):
        _as_study("helper.mint(adapter)", helper=helper, adapter=adapter)
    with pytest.raises(IngestionRefused):
        _as_study("helper.rec(ledger, m)", helper=helper, ledger=FirstPartyLedger(), m=m)
    assert helper.mint(adapter).source == "gsc"                                  # from a normal module: fine


def test_a_non_study_module_with_a_similar_name_is_not_blocked():
    for name in ("agentic_os.market.synthetic_research_notes", "agentic_os.market.not_synthetic_research.x",
                 "other.agentic_os.market.synthetic_research.x"):
        ns = {"__name__": name, "adapter": repo_adapter("crm")}
        exec(compile("m = adapter.make_metric(metric='x', value=1.0)", "<x>", "exec"), ns)
        assert ns["m"].source == "crm", name


def test_the_ledger_read_path_is_not_blocked_for_study_frames_only_writes_and_minting_are():
    led = FirstPartyLedger()
    led.record(repo_adapter("billing").make_metric(metric="refund", value=0.1, segment="control"))
    ns = _as_study("v = led.value('refund', 'control')", led=led)
    assert ns["v"] == 0.1                                                        # reading is not a first-party write


# ── key hiding, no pickle / copy ─────────────────────────────────────────────────────────────────────────
def test_adapters_expose_no_key_and_refuse_pickle_copy_and_state_access():
    a = repo_adapter("erp")
    with pytest.raises(TypeError):
        vars(a)
    assert not hasattr(a, "__dict__")
    for attr in dir(a):
        if not attr.startswith("__"):
            assert not isinstance(getattr(a, attr), (bytes, bytearray)), attr
    for fn in (pickle.dumps, copy.copy, copy.deepcopy, lambda o: o.__reduce__(), lambda o: o.__getstate__(),
               lambda o: o.__reduce_ex__(4)):
        with pytest.raises(TypeError):
            fn(a)
    for protocol in range(0, pickle.HIGHEST_PROTOCOL + 1):
        with pytest.raises(TypeError):
            pickle.dumps(a, protocol)
    with pytest.raises(AttributeError):
        a.source = "gsc"                                                         # type: ignore[misc]
    with pytest.raises(AttributeError):
        del a.adapter_id
    assert "key" not in repr(a).lower() and not hasattr(ING, "_KEYS")


def test_a_metric_still_pickles_and_copies_and_stays_valid_but_a_forged_copy_does_not():
    m = repo_adapter("umami").make_metric(metric="m", value=0.5, segment="treatment")
    for c in (copy.copy(m), copy.deepcopy(m), pickle.loads(pickle.dumps(m))):
        assert FirstPartyLedger().record(c) is c
    forged = pickle.loads(pickle.dumps(m))
    object.__setattr__(forged, "value", 9.0)
    with pytest.raises(IngestionRefused):
        FirstPartyLedger().record(forged)


def test_the_attestation_is_bound_to_the_source_and_the_adapter_id_inside_the_mac():
    # the same digest signed for another (source, adapter) pair gives a different mac (defence in depth, L6)
    a, b = repo_adapter("umami"), repo_adapter("gsc")
    assert a._sign("d" * 64) != b._sign("d" * 64) and a._sign("d" * 64) == a._sign("d" * 64)


# ── (c) wording ──────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", ["docs/SEARCH_RELEVANCE.md", "docs/ledger_source_inventory.md",
                                  "agentic_os/market/ingestion.py", "agentic_os/market/outcomes_join.py",
                                  "tests/test_first_party_ledger_guard.py"])
def test_no_overclaiming_wording_and_the_limit_is_stated(path):
    text = (ROOT / path).read_text().lower()
    assert "unforgeable" not in text and "hard guarantee" not in text and "cannot be forged" not in text


def test_the_docs_state_that_the_guard_is_bypassable_in_process():
    for path in ("docs/SEARCH_RELEVANCE.md", "docs/ledger_source_inventory.md"):
        text = (ROOT / path).read_text().lower()
        assert "bypassable by code that runs in the same process" in text
        assert "accidental and casual misuse" in text
    assert "bypassable" in (ROOT / "agentic_os/market/ingestion.py").read_text().lower()


# ── round 6 (LZ-1): the submodule attribute resolves lazily like on base ─────────────────────────────────
def test_lz1_a_bare_import_of_the_market_package_still_exposes_outcomes_join_as_an_attribute_lazily():
    r = _fresh("import sys, agentic_os.market as m\n"
               "assert 'agentic_os.market.outcomes_join' not in sys.modules and 'agentic_os.market.ingestion' not in sys.modules\n"
               "assert 'outcomes_join' in dir(m) and 'FirstPartyLedger' in dir(m)\n"
               "mod = m.outcomes_join\n"
               "assert mod.__name__ == 'agentic_os.market.outcomes_join' and 'agentic_os.market.outcomes_join' in sys.modules\n"
               "assert m.outcomes_join is mod and mod.FirstPartyLedger is m.FirstPartyLedger\n"
               "import agentic_os.market.outcomes_join as again\nassert again is mod\n"
               "try:\n    m.ingestion_typo\nexcept AttributeError:\n    print('ok')")
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr


def test_lz1_importing_the_synthetic_research_package_still_loads_neither_module():
    r = _fresh("import sys, agentic_os.market.synthetic_research.evidence_ref as e, agentic_os.market as m\n"
               "assert 'outcomes_join' in dir(m)\n"
               "print([k for k in sys.modules if k.endswith('market.ingestion') or k.endswith('market.outcomes_join')])")
    assert r.returncode == 0 and r.stdout.strip() == "[]", r.stderr

"""Compatibility impact of the first-party ledger guard on out-of-repo callers (SR-04 round 4).

The table below is DERIVED, not written by hand: `tests/ledger_compat_probe.py` was run against the base commit
d9210fb (before the guard existed) and against this tree, producing `tests/golden/ledger_compat_base.json` and
`tests/golden/ledger_compat_now.json`. This file pins both, recomputes the current outcome table live, and asserts
exactly which classes of input changed from accepted to refused. The guard is the ONLY market-plane behaviour change
of SR-04 and it is unconditional (no flag; same result with AGENTIC_OS_SYNTHETIC_RESEARCH unset)."""
from __future__ import annotations

import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ledger_compat_probe as P  # noqa: E402

GOLD = pathlib.Path(__file__).resolve().parent / "golden"
BASE = json.loads((GOLD / "ledger_compat_base.json").read_text())
NOW = json.loads((GOLD / "ledger_compat_now.json").read_text())


def test_the_current_outcome_table_equals_the_pinned_one():
    assert P.run() == NOW


def test_the_base_commit_accepted_every_hand_built_metric():
    hand = [k for k in BASE if k.startswith("hand|source=")]
    assert len(hand) == len(P.SOURCES) * len(P.PROVIDERS) and {BASE[k] for k in hand} == {"ok"}


def test_exactly_what_changed_for_out_of_repo_callers():
    changed = {k: (BASE[k], NOW[k]) for k in BASE if BASE[k] != NOW[k]}
    kinds = collections.Counter(NOW[k] for k in changed)
    # hand-built metrics: every one is now refused (a source label alone is no longer evidence of provenance)
    hand = [k for k in BASE if k.startswith("hand|source=")]
    assert all(NOW[k] in ("ValueError", "IngestionRefused") for k in hand)
    # ... with the unregistered / missing sources refused by name (ValueError) and the seven registered sources
    # refused for lack of an adapter attestation (IngestionRefused, a ValueError subclass)
    for k in hand:
        source = k.split("|")[1][len("source="):]
        expected_cls = "IngestionRefused" if eval(source) in P.LEGIT else "ValueError"
        assert NOW[k] == expected_cls, k
    # metrics minted by a registered adapter keep working for every registered source
    assert all(NOW[f"minted|source={s}"] == "ok" for s in P.LEGIT)
    # tampering, copying, subclasses, duck types, study refs: accepted by base, refused now
    for k in ("minted|umami|value mutated after minting", "minted|umami|copied onto a different metric"):
        assert (BASE[k], NOW[k]) == ("ok", "IngestionRefused")
    for k in ("duck|SimpleNamespace metric", "subclass|FirstPartyMetric subclass"):
        assert (BASE[k], NOW[k]) == ("ok", "TypeError")
    for k in ("minted|umami|study ref in evidence_refs", "minted|umami|study ref with whitespace",
              "hand|umami|study ref in evidence_refs", "duck|synthetic=True attribute"):
        assert (BASE[k], NOW[k]) == ("ok", "ValueError")
    # non-metrics failed on base with AttributeError (record read .metric); now a clean TypeError
    for k in ("type|dict", "type|None", "type|object", "type|str"):
        assert (BASE[k], NOW[k]) == ("AttributeError", "TypeError")
    assert sum(kinds.values()) == len(changed) and kinds["ok"] == 0       # nothing changed into "ok"


def test_the_two_registered_source_name_classes_the_owner_asked_about_are_refused_for_hand_built_metrics():
    for s in ("stripe", "ga4", "analytics_v2", "umami:site", "internal"):
        assert all(NOW[f"hand|source={s!r}|provider={p}"] == "ValueError" for p in P.PROVIDERS)

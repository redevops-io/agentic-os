"""SR-04 round 2: flag-off bit-identity against the BASE commit on ~2,800 observations, for every input shape.

`tests/golden/search_relevance_bitid_base.json` (159 digests) was produced by running
`tests/search_relevance_bitid_support.py` against the unmodified base commit d9210fb (before SR-04). Any behavioural
drift (list/tuple/generator/iterator inputs, duck-typed observation objects, detectors, aggregation, the mapper,
the queue, goal measures) changes a digest. The reviewer's independent harness was also run (see
docs/SEARCH_RELEVANCE.md, "Bit-identity evidence").
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import search_relevance_bitid_support as S  # noqa: E402

from agentic_os.content.search_signals import RELEVANCE_UNKNOWN_AWARE_ENV, SearchObservation, SignalType  # noqa: E402

GOLDEN = json.loads((Path(__file__).resolve().parent / "golden" / "search_relevance_bitid_base.json").read_text())

# every value the independent reviewer drove through the flag, plus unset
FLAG_VALUES = [None, "", "0", "false", "False", "off", "no", "FALSE", " ", "garbage", "1x", "2", "-1", "nope", "y",
               "t", "enabled", "\x01", "10"]


def test_golden_covers_a_large_fixture_and_every_input_shape():
    assert int(GOLDEN["n_rows"]) >= 2800 and len(GOLDEN) >= 150
    for shape in ("list", "tuple", "gen", "iter_tuple"):
        for kind in ("dc", "ns", "nt"):
            assert f"sig_{kind}_{shape}" in GOLDEN and f"cann_{kind}_{shape}" in GOLDEN
    assert int(GOLDEN["nonnull_detectors"]) > 5000                     # the fixture exercises real signals


@pytest.mark.parametrize("flag", FLAG_VALUES, ids=[repr(f) for f in FLAG_VALUES])
def test_flag_off_is_bit_identical_to_base_on_the_large_fixture(monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv(RELEVANCE_UNKNOWN_AWARE_ENV, raising=False)
    else:
        monkeypatch.setenv(RELEVANCE_UNKNOWN_AWARE_ENV, flag)
    got = S.compute()
    diff = sorted(k for k in set(GOLDEN) | set(got) if GOLDEN.get(k) != got.get(k))
    assert diff == []


def test_iterator_and_duck_typed_inputs_equal_the_dataclass_results():
    for shape in ("list", "tuple", "gen", "iter_tuple"):
        assert GOLDEN[f"sig_dc_{shape}"] == GOLDEN[f"sig_ns_{shape}"] == GOLDEN[f"sig_nt_{shape}"]
    assert GOLDEN["detectors_dc"] == GOLDEN["detectors_ns"] == GOLDEN["detectors_nt"]
    assert GOLDEN["sig_dc_list"] != GOLDEN["sig_dc_gen"]    # base quirk kept: a consumed iterator hides cannibalization


def test_deliberate_non_identities_are_exactly_the_documented_list():
    """What is NOT identical to base, on purpose (documented in docs/SEARCH_RELEVANCE.md)."""
    names = [f.name for f in dataclasses.fields(SearchObservation)]
    assert names[:9] == S.BASE_FIELDS and names[9:] == ["relevance_source", "relevance_detail"]   # repr/asdict gain 2
    assert [m.name for m in SignalType][-1] == "RELEVANCE_CONFLICT"            # new enum member, last
    assert [m.name for m in SignalType][:-1] == ["EXISTING_INTENT", "NEAR_WIN", "EMERGENT_INTENT", "MISSING_PAGE",
                                                 "CANNIBALIZATION", "CTR_OPPORTUNITY", "TOPIC_EXPANSION"]
